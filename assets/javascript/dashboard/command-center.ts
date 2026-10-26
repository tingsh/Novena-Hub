import { GridStack } from 'gridstack';
import 'gridstack/dist/gridstack.min.css';
import '../../styles/app/command-center.css';

type PanelGeometry = {
  id: string;
  x: number;
  y: number;
  w: number;
  h: number;
  hidden: boolean;
  mobile_order: number;
};

type PanelDefinition = {
  id: string;
  title: string;
  description: string;
  min: {w: number; h: number};
  default: {x: number; y: number; w: number; h: number};
  max: {w: number; h: number};
  hideable: boolean;
  warn_before_hide: boolean;
};

type CommandCenterConfig = {
  schema_version: number;
  layout: {schema_version: number; panels: PanelGeometry[]};
  source: string;
  personal_revision: number;
  team_default_revision: number;
  can_publish: boolean;
  catalog: PanelDefinition[];
  urls: {save: string; reset: string; publish: string; remove_default: string};
};

declare global {
  interface Window {
    Chart?: any;
    htmx?: any;
    NovenaCommandCenter?: CommandCenterController;
  }
}

const STORAGE_KEY = 'novena.commandCenter.refreshSeconds';
const ALLOWED_INTERVALS = [15, 30, 60];
const MOBILE_BREAKPOINT = 768;

function clonePanels(panels: Map<string, PanelGeometry>): Map<string, PanelGeometry> {
  return new Map(Array.from(panels.entries()).map(([id, panel]) => [id, {...panel}]));
}

function csrfToken(): string {
  const match = document.cookie.match(/(?:^|; )csrftoken=([^;]*)/);
  return match ? decodeURIComponent(match[1]) : '';
}

export class CommandCenterController {
  private grid: GridStack | null = null;
  private config: CommandCenterConfig | null = null;
  private panels = new Map<string, PanelGeometry>();
  private snapshot: Map<string, PanelGeometry> | null = null;
  private editing = false;
  private refreshTimer: number | null = null;
  private trendChart: any = null;
  private mobile = window.innerWidth < MOBILE_BREAKPOINT;
  private resizeTimer: number | null = null;
  private pendingConfirm: (() => void) | null = null;

  bindPage(): void {
    this.destroyVisuals();
    const configNode = document.getElementById('command-center-config');
    const gridElement = document.getElementById('command-center-grid');
    if (!configNode || !gridElement) {
      this.bindRefreshControls();
      return;
    }

    try {
      this.config = JSON.parse(configNode.textContent || '{}') as CommandCenterConfig;
    } catch (error) {
      console.warn('Command Center configuration is invalid', error);
      this.config = null;
    }
    this.panels = new Map((this.config?.layout.panels || []).map(panel => [panel.id, {...panel}]));
    this.mobile = window.innerWidth < MOBILE_BREAKPOINT;

    if (gridElement.dataset.customizationEnabled === 'true') {
      if (this.mobile) {
        gridElement.classList.add('command-center-mobile-grid');
        this.sortMobileDom();
      } else {
        this.grid = GridStack.init({
          column: 12,
          cellHeight: 72,
          margin: 20,
          animate: true,
          float: false,
          staticGrid: true,
          handle: '.command-center-drag-handle',
          resizable: {handles: 'n, e, s, w, ne, nw, se, sw'},
        }, gridElement);
        this.grid.on('change', (_event: Event, items: any[]) => {
          for (const item of items || []) this.captureNode(item);
          this.updateAllDensities();
        });
        this.grid.on('resizestop', (_event: Event, element: HTMLElement) => {
          this.captureElement(element);
          this.updateDensity(element);
          this.trendChart?.resize();
        });
        this.grid.on('dragstop', (_event: Event, element: HTMLElement) => this.captureElement(element));
      }
    }

    this.bindCustomizationControls();
    this.bindRefreshControls();
    this.updateAllDensities();
    this.updateHiddenUI();
    void this.initTrendChart();
  }

  installGlobalListeners(): void {
    document.body.addEventListener('htmx:beforeRequest', event => {
      const detail = (event as CustomEvent).detail;
      if (detail?.elt?.id === 'dashboard-live-container') this.setRefreshBusy(true);
    });
    document.body.addEventListener('htmx:afterRequest', event => {
      const detail = (event as CustomEvent).detail;
      if (detail?.elt?.id === 'dashboard-live-container') {
        if (detail.successful) this.updateLastRefreshed();
        else this.setRefreshBusy(false);
      }
    });
    document.body.addEventListener('htmx:beforeSwap', event => {
      const detail = (event as CustomEvent).detail;
      if (detail?.target?.id === 'dashboard-live-container') this.destroyVisuals();
    });
    document.body.addEventListener('htmx:afterSettle', event => {
      const detail = (event as CustomEvent).detail;
      if (detail?.target?.id === 'dashboard-live-container') {
        this.bindPage();
        this.setRefreshBusy(false);
      }
    });
    window.addEventListener('resize', () => {
      if (this.resizeTimer) window.clearTimeout(this.resizeTimer);
      this.resizeTimer = window.setTimeout(() => {
        const nextMobile = window.innerWidth < MOBILE_BREAKPOINT;
        if (nextMobile !== this.mobile && document.querySelector('[data-command-center-page]')) this.bindPage();
      }, 180);
    });
  }

  private bindCustomizationControls(): void {
    const customize = document.getElementById('command-center-customize') as HTMLButtonElement | null;
    const cancel = document.getElementById('command-center-cancel') as HTMLButtonElement | null;
    const save = document.getElementById('command-center-save') as HTMLButtonElement | null;
    const addPanels = document.getElementById('command-center-add-panels') as HTMLButtonElement | null;
    if (customize) customize.onclick = () => this.enterEditMode();
    if (cancel) cancel.onclick = () => this.cancelEdit();
    if (save) save.onclick = () => void this.saveLayout();
    if (addPanels) addPanels.onclick = () => this.openDrawer();
    document.querySelectorAll<HTMLAnchorElement>('[data-command-center-kpi-link]').forEach(link => {
      link.onclick = event => {
        if (!this.editing) return;
        event.preventDefault();
        this.announce('Save or cancel dashboard editing before opening KPI details.');
      };
    });

    document.querySelectorAll<HTMLElement>('[data-open-panel-drawer]').forEach(button => {
      button.onclick = () => this.openDrawer();
    });
    document.querySelectorAll<HTMLElement>('[data-close-panel-drawer]').forEach(button => {
      button.onclick = () => this.closeDrawer();
    });
    document.querySelectorAll<HTMLButtonElement>('[data-restore-panel]').forEach(button => {
      button.onclick = () => this.restorePanel(button.dataset.restorePanel || '');
    });
    const restoreAll = document.getElementById('command-center-restore-all') as HTMLButtonElement | null;
    if (restoreAll) restoreAll.onclick = () => {
      for (const panel of Array.from(this.panels.values())) {
        if (panel.hidden) this.restorePanel(panel.id, false);
      }
      this.closeDrawer();
      this.announce('All panels restored.');
    };

    document.querySelectorAll<HTMLButtonElement>('[data-panel-action]').forEach(button => {
      button.onclick = () => {
        const element = button.closest<HTMLElement>('[data-panel-id]');
        if (!element) return;
        const action = button.dataset.panelAction;
        if (action === 'hide') this.requestHide(element.dataset.panelId || '');
        if (action === 'move-earlier') this.movePanel(element.dataset.panelId || '', -1);
        if (action === 'move-later') this.movePanel(element.dataset.panelId || '', 1);
        button.closest('details')?.removeAttribute('open');
      };
    });
    document.querySelectorAll<HTMLButtonElement>('[data-panel-size]').forEach(button => {
      button.onclick = () => {
        const element = button.closest<HTMLElement>('[data-panel-id]');
        if (element) this.applySize(element.dataset.panelId || '', button.dataset.panelSize || 'default');
        button.closest('details')?.removeAttribute('open');
      };
    });

    document.querySelectorAll<HTMLButtonElement>('[data-layout-command]').forEach(button => {
      button.onclick = () => {
        document.getElementById('command-center-actions')?.removeAttribute('open');
        const command = button.dataset.layoutCommand;
        if (command === 'reset') this.confirmAction(
          'Reset my layout?',
          'Your personal arrangement will be removed. You will inherit the team default, or the Novena default if no team default exists.',
          'Reset layout',
          () => void this.postLayoutCommand('reset'),
        );
        if (command === 'publish') this.confirmAction(
          'Publish team default?',
          'People without a personal layout will receive your saved arrangement. Existing personal layouts will not change.',
          'Publish default',
          () => void this.postLayoutCommand('publish'),
        );
        if (command === 'remove-default') this.confirmAction(
          'Remove team default?',
          'People without a personal layout will return to the Novena default.',
          'Remove default',
          () => void this.postLayoutCommand('remove_default'),
        );
      };
    });

    const confirm = document.getElementById('command-center-confirm-action') as HTMLButtonElement | null;
    if (confirm) confirm.onclick = () => {
      const action = this.pendingConfirm;
      this.pendingConfirm = null;
      (document.getElementById('command-center-confirm-dialog') as HTMLDialogElement | null)?.close();
      action?.();
    };
    document.querySelectorAll<HTMLElement>('[data-confirm-cancel]').forEach(button => {
      button.onclick = () => {
        this.pendingConfirm = null;
        (document.getElementById('command-center-confirm-dialog') as HTMLDialogElement | null)?.close();
        if (!this.editing) this.startRefreshTimer(this.readInterval());
      };
    });
  }

  private enterEditMode(): void {
    if (this.editing) return;
    this.editing = true;
    this.snapshot = clonePanels(this.panels);
    document.querySelector('[data-command-center-page]')?.classList.add('command-center-is-editing');
    document.getElementById('command-center-edit-toolbar')?.classList.replace('hidden', 'flex');
    document.getElementById('command-center-customize')?.classList.add('hidden');
    document.getElementById('command-center-actions')?.classList.add('hidden');
    this.grid?.setStatic(false);
    this.pauseRefresh();
    this.setRefreshBusy(false);
    this.announce('Dashboard editing enabled.');
  }

  private cancelEdit(): void {
    if (!this.snapshot) return;
    this.applySnapshot(this.snapshot);
    this.exitEditMode();
    this.announce('Dashboard changes cancelled.');
    this.requestRefresh();
  }

  private exitEditMode(): void {
    this.editing = false;
    this.snapshot = null;
    document.querySelector('[data-command-center-page]')?.classList.remove('command-center-is-editing');
    document.getElementById('command-center-edit-toolbar')?.classList.replace('flex', 'hidden');
    document.getElementById('command-center-customize')?.classList.remove('hidden');
    document.getElementById('command-center-actions')?.classList.remove('hidden');
    this.grid?.setStatic(true);
    this.setRefreshBusy(false);
    this.startRefreshTimer(this.readInterval());
  }

  private captureNode(node: any): void {
    const element = node?.el as HTMLElement | undefined;
    const id = element?.dataset.panelId;
    const panel = id ? this.panels.get(id) : null;
    if (!panel) return;
    panel.x = Number(node.x ?? panel.x);
    panel.y = Number(node.y ?? panel.y);
    panel.w = Number(node.w ?? panel.w);
    panel.h = Number(node.h ?? panel.h);
  }

  private captureElement(element: HTMLElement): void {
    this.captureNode((element as any).gridstackNode);
  }

  private requestHide(panelId: string): void {
    const definition = this.definition(panelId);
    if (!definition?.hideable) return;
    if (definition.warn_before_hide) {
      this.confirmAction(
        'Hide Needs Attention?',
        'The detailed exception queue will be hidden. Fleet Health, Gateways, Active Alerts, and Maintenance summaries will remain visible.',
        'Hide panel',
        () => this.hidePanel(panelId),
      );
      return;
    }
    this.hidePanel(panelId);
  }

  private hidePanel(panelId: string): void {
    const panel = this.panels.get(panelId);
    const element = document.querySelector<HTMLElement>(`[data-panel-id="${panelId}"]`);
    const stash = document.getElementById('command-center-panel-stash');
    if (!panel || !element || !stash) return;
    this.captureElement(element);
    if (this.grid) this.grid.removeWidget(element, false);
    stash.appendChild(element);
    element.hidden = true;
    panel.hidden = true;
    this.updateHiddenUI();
    this.announce(`${this.definition(panelId)?.title || 'Panel'} hidden.`);
  }

  private restorePanel(panelId: string, closeDrawer = true): void {
    const panel = this.panels.get(panelId);
    const element = document.querySelector<HTMLElement>(`[data-panel-id="${panelId}"]`);
    const gridElement = document.getElementById('command-center-grid');
    if (!panel || !element || !gridElement || !panel.hidden) return;
    panel.hidden = false;
    element.hidden = false;
    gridElement.appendChild(element);
    if (this.grid) {
      this.grid.makeWidget(element);
      this.grid.update(element, {x: panel.x, y: panel.y, w: panel.w, h: panel.h});
    } else if (this.mobile) {
      this.sortMobileDom();
    }
    this.updateDensity(element);
    this.updateHiddenUI();
    if (closeDrawer) this.closeDrawer();
    this.announce(`${this.definition(panelId)?.title || 'Panel'} restored.`);
  }

  private movePanel(panelId: string, direction: -1 | 1): void {
    const panel = this.panels.get(panelId);
    if (!panel) return;
    if (this.mobile) {
      const ordered = Array.from(this.panels.values()).sort((a, b) => a.mobile_order - b.mobile_order);
      const index = ordered.findIndex(item => item.id === panelId);
      const next = ordered[index + direction];
      if (!next) return;
      [panel.mobile_order, next.mobile_order] = [next.mobile_order, panel.mobile_order];
      this.sortMobileDom();
    } else {
      const element = document.querySelector<HTMLElement>(`[data-panel-id="${panelId}"]`);
      if (!element || !this.grid) return;
      this.grid.update(element, {y: Math.max(0, panel.y + direction * Math.max(1, panel.h))});
      this.captureElement(element);
    }
    this.announce(`${this.definition(panelId)?.title || 'Panel'} moved ${direction < 0 ? 'earlier' : 'later'}.`);
  }

  private applySize(panelId: string, size: string): void {
    const definition = this.definition(panelId);
    const panel = this.panels.get(panelId);
    const element = document.querySelector<HTMLElement>(`[data-panel-id="${panelId}"]`);
    if (!definition || !panel || !element || this.mobile || !this.grid) return;
    const geometry = size === 'compact' ? definition.min : size === 'expanded' ? definition.max : definition.default;
    this.grid.update(element, {w: geometry.w, h: geometry.h});
    this.captureElement(element);
    this.updateDensity(element);
    this.trendChart?.resize();
    this.announce(`${definition.title} set to ${size === 'default' ? 'standard' : size} size.`);
  }

  private applySnapshot(snapshot: Map<string, PanelGeometry>): void {
    for (const [id, target] of Array.from(snapshot.entries())) {
      const current = this.panels.get(id);
      if (!current) continue;
      if (current.hidden && !target.hidden) this.restorePanel(id, false);
      if (!current.hidden && target.hidden) this.hidePanel(id);
      Object.assign(current, target);
      const element = document.querySelector<HTMLElement>(`[data-panel-id="${id}"]`);
      if (element && !target.hidden && this.grid) {
        this.grid.update(element, {x: target.x, y: target.y, w: target.w, h: target.h});
      }
    }
    if (this.mobile) this.sortMobileDom();
    this.updateAllDensities();
    this.updateHiddenUI();
  }

  private async saveLayout(): Promise<void> {
    if (!this.config) return;
    const button = document.getElementById('command-center-save') as HTMLButtonElement | null;
    if (button) {
      button.disabled = true;
      button.innerHTML = '<span class="loading loading-spinner loading-xs"></span> Saving';
    }
    this.normalizeMobileOrder();
    try {
      const response = await this.postJson(this.config.urls.save, {
        schema_version: this.config.schema_version,
        base_revision: this.config.personal_revision,
        panels: Array.from(this.panels.values()),
      });
      this.config.personal_revision = response.personal_revision;
      this.config.team_default_revision = response.team_default_revision;
      this.exitEditMode();
      this.notify('Dashboard layout saved.');
      this.requestRefresh();
    } catch (error) {
      this.notify(error instanceof Error ? error.message : 'The dashboard layout could not be saved.', true);
    } finally {
      if (button) {
        button.disabled = false;
        button.innerHTML = '<i class="fa fa-check mr-2"></i>Save';
      }
    }
  }

  private async postLayoutCommand(command: 'reset' | 'publish' | 'remove_default'): Promise<void> {
    if (!this.config) return;
    const url = command === 'reset' ? this.config.urls.reset : command === 'publish' ? this.config.urls.publish : this.config.urls.remove_default;
    const baseRevision = command === 'reset' ? this.config.personal_revision : this.config.team_default_revision;
    try {
      const response = await this.postJson(url, {base_revision: baseRevision});
      this.config.personal_revision = response.personal_revision;
      this.config.team_default_revision = response.team_default_revision;
      this.notify(command === 'reset' ? 'Personal layout reset.' : command === 'publish' ? 'Team default published.' : 'Team default removed.');
      this.requestRefresh();
    } catch (error) {
      this.notify(error instanceof Error ? error.message : 'The dashboard change could not be completed.', true);
    } finally {
      if (!this.editing) this.startRefreshTimer(this.readInterval());
    }
  }

  private async postJson(url: string, payload: object): Promise<any> {
    const response = await fetch(url, {
      method: 'POST',
      credentials: 'same-origin',
      headers: {'Content-Type': 'application/json', 'X-CSRFToken': csrfToken()},
      body: JSON.stringify(payload),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.message || 'The dashboard change could not be completed.');
    return data;
  }

  private definition(panelId: string): PanelDefinition | undefined {
    return this.config?.catalog.find(panel => panel.id === panelId);
  }

  private updateAllDensities(): void {
    document.querySelectorAll<HTMLElement>('[data-panel-id]').forEach(element => this.updateDensity(element));
  }

  private updateDensity(element: HTMLElement): void {
    const id = element.dataset.panelId || '';
    const panel = this.panels.get(id);
    const definition = this.definition(id);
    if (!panel || !definition) return;
    let density = 'standard';
    if (panel.w <= definition.min.w && panel.h <= definition.min.h) density = 'compact';
    const expanded = id === 'business_impact'
      ? panel.h >= 3
      : id === 'operations_trend'
        ? panel.w >= 10 || panel.h >= 5
        : panel.w >= 6 || panel.h >= 6;
    if (expanded) density = 'expanded';
    element.dataset.density = density;
  }

  private normalizeMobileOrder(): void {
    if (!this.mobile) {
      const visible = Array.from(this.panels.values()).filter(panel => !panel.hidden).sort((a, b) => a.y - b.y || a.x - b.x);
      const hidden = Array.from(this.panels.values()).filter(panel => panel.hidden).sort((a, b) => a.mobile_order - b.mobile_order);
      [...visible, ...hidden].forEach((panel, index) => panel.mobile_order = index);
    } else {
      Array.from(this.panels.values()).sort((a, b) => a.mobile_order - b.mobile_order).forEach((panel, index) => panel.mobile_order = index);
    }
  }

  private sortMobileDom(): void {
    const gridElement = document.getElementById('command-center-grid');
    if (!gridElement) return;
    Array.from(this.panels.values())
      .filter(panel => !panel.hidden)
      .sort((a, b) => a.mobile_order - b.mobile_order)
      .forEach(panel => {
        const element = document.querySelector<HTMLElement>(`[data-panel-id="${panel.id}"]`);
        if (element) gridElement.appendChild(element);
      });
  }

  private updateHiddenUI(): void {
    const hidden = Array.from(this.panels.values()).filter(panel => panel.hidden);
    const count = document.getElementById('command-center-hidden-count');
    if (count) count.textContent = String(hidden.length);
    document.querySelectorAll<HTMLElement>('[data-panel-catalog-id]').forEach(item => {
      item.hidden = !this.panels.get(item.dataset.panelCatalogId || '')?.hidden;
    });
    const restoreAll = document.getElementById('command-center-restore-all') as HTMLButtonElement | null;
    if (restoreAll) restoreAll.disabled = hidden.length === 0;
    document.getElementById('command-center-no-hidden-panels')?.classList.toggle('hidden', hidden.length !== 0);
    const addPanels = document.getElementById('command-center-add-panels') as HTMLButtonElement | null;
    if (addPanels) {
      addPanels.disabled = hidden.length === 0;
      addPanels.title = hidden.length === 0 ? 'All available panels are visible' : 'Restore hidden panels';
    }
    document.getElementById('command-center-empty')?.classList.toggle('hidden', hidden.length !== this.panels.size);
  }

  private openDrawer(): void {
    this.updateHiddenUI();
    (document.getElementById('command-center-panel-drawer') as HTMLDialogElement | null)?.showModal();
  }

  private closeDrawer(): void {
    (document.getElementById('command-center-panel-drawer') as HTMLDialogElement | null)?.close();
  }

  private confirmAction(title: string, message: string, label: string, action: () => void): void {
    const dialog = document.getElementById('command-center-confirm-dialog') as HTMLDialogElement | null;
    if (!dialog) return;
    const titleElement = document.getElementById('command-center-confirm-title');
    const messageElement = document.getElementById('command-center-confirm-message');
    const actionButton = document.getElementById('command-center-confirm-action');
    if (titleElement) titleElement.textContent = title;
    if (messageElement) messageElement.textContent = message;
    if (actionButton) actionButton.textContent = label;
    this.pendingConfirm = action;
    this.pauseRefresh();
    dialog.showModal();
  }

  private bindRefreshControls(): void {
    const selector = document.getElementById('fleet-refresh-interval') as HTMLSelectElement | null;
    const button = document.getElementById('fleet-refresh-button') as HTMLButtonElement | null;
    if (!selector || !button) {
      this.pauseRefresh();
      return;
    }
    const interval = this.readInterval();
    selector.value = String(interval);
    selector.onchange = () => this.applyInterval(Number(selector.value));
    button.onclick = () => this.requestRefresh();
    this.applyInterval(interval);
    this.updateLastRefreshed();
  }

  private readInterval(): number {
    try {
      const stored = Number(window.localStorage.getItem(STORAGE_KEY));
      return ALLOWED_INTERVALS.includes(stored) ? stored : 60;
    } catch (_error) {
      return 60;
    }
  }

  private applyInterval(seconds: number): void {
    const interval = ALLOWED_INTERVALS.includes(seconds) ? seconds : 60;
    try { window.localStorage.setItem(STORAGE_KEY, String(interval)); } catch (_error) { /* Use default. */ }
    const copy = document.getElementById('fleet-refresh-copy');
    if (copy) copy.textContent = `Fleet summary updates every ${interval}s`;
    if (!this.editing) this.startRefreshTimer(interval);
  }

  private startRefreshTimer(seconds: number): void {
    this.pauseRefresh();
    this.refreshTimer = window.setInterval(() => this.requestRefresh(), seconds * 1000);
  }

  private pauseRefresh(): void {
    if (this.refreshTimer) window.clearInterval(this.refreshTimer);
    this.refreshTimer = null;
  }

  private requestRefresh(): void {
    if (this.editing || !document.getElementById('dashboard-live-container')) return;
    window.htmx?.trigger(document.body, 'fleetSummaryRefresh');
  }

  private updateLastRefreshed(): void {
    const element = document.getElementById('fleet-last-refreshed') as HTMLTimeElement | null;
    if (!element) return;
    const now = new Date();
    element.dateTime = now.toISOString();
    element.textContent = now.toLocaleTimeString();
  }

  private setRefreshBusy(busy: boolean): void {
    const button = document.getElementById('fleet-refresh-button') as HTMLButtonElement | null;
    if (button) {
      button.disabled = busy || this.editing;
      button.innerHTML = busy ? '<span class="loading loading-spinner loading-xs"></span>' : '<i class="fa fa-refresh"></i>';
    }
    const customize = document.getElementById('command-center-customize') as HTMLButtonElement | null;
    if (customize) customize.disabled = busy;
    const actions = document.getElementById('command-center-actions');
    if (actions) {
      actions.toggleAttribute('inert', busy);
      actions.setAttribute('aria-disabled', String(busy));
    }
  }

  private async initTrendChart(): Promise<void> {
    const canvas = document.getElementById('operations-trend-chart') as HTMLCanvasElement | null;
    const labels = document.getElementById('operations-trend-labels');
    const values = document.getElementById('operations-trend-values');
    if (!canvas || !labels || !values || !window.Chart) return;
    this.trendChart = new window.Chart(canvas.getContext('2d'), {
      type: 'line',
      data: {labels: JSON.parse(labels.textContent || '[]'), datasets: [{data: JSON.parse(values.textContent || '[]'), borderColor: '#2563eb', backgroundColor: 'rgba(37,99,235,.08)', borderWidth: 2, fill: true, tension: .35, pointRadius: 2, spanGaps: false}]},
      options: {responsive: true, maintainAspectRatio: false, plugins: {legend: {display: false}}, scales: {x: {grid: {display: false}, ticks: {maxTicksLimit: 8, color: '#94a3b8', font: {size: 10}}}, y: {beginAtZero: false, grid: {color: 'rgba(148,163,184,.18)'}, ticks: {color: '#94a3b8', font: {size: 10}}}}},
    });
  }

  private destroyVisuals(): void {
    this.trendChart?.destroy();
    this.trendChart = null;
    this.grid?.destroy(false);
    this.grid = null;
  }

  private announce(message: string): void {
    const announcer = document.getElementById('command-center-announcer');
    if (announcer) announcer.textContent = message;
  }

  private notify(message: string, error = false): void {
    this.announce(message);
    const toast = document.createElement('div');
    toast.className = `command-center-toast ${error ? 'command-center-toast-error' : ''}`;
    toast.textContent = message;
    document.body.appendChild(toast);
    window.setTimeout(() => toast.remove(), 4000);
  }
}

export function installCommandCenter(): void {
  if (window.NovenaCommandCenter) {
    window.NovenaCommandCenter.bindPage();
    return;
  }
  const controller = new CommandCenterController();
  window.NovenaCommandCenter = controller;
  controller.installGlobalListeners();
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', () => controller.bindPage(), {once: true});
  else controller.bindPage();
}
