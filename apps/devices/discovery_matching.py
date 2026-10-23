from __future__ import annotations

from dataclasses import dataclass

from django.db.models import Q

from .models import DeviceTemplate
from .services import visible_templates_for_team


@dataclass
class TemplateMatch:
    template: DeviceTemplate
    score: int
    reasons: list[str]


def _norm(value) -> str:
    return str(value or "").strip().lower()


def _hint_values(template: DeviceTemplate, key: str) -> list[str]:
    hints = template.discovery_hints or {}
    value = hints.get(key, [])
    if isinstance(value, str):
        return [_norm(value)]
    if isinstance(value, list):
        return [_norm(item) for item in value if item]
    return []


def rank_templates_for_discovery(discovery: dict, limit: int = 5, team=None) -> list[dict]:
    """Return ranked candidate templates for one discovered field device."""
    identification = discovery.get("identification") or {}
    vendor = _norm(identification.get("vendor") or discovery.get("vendor"))
    model = _norm(identification.get("model") or discovery.get("model"))
    signature = _norm(discovery.get("signature"))
    protocol = _norm(discovery.get("protocol") or discovery.get("connection")).replace("modbus_", "modbus_")

    qs = DeviceTemplate.objects.filter(is_current=True).exclude(evidence_status="retired")
    if team is not None:
        qs = visible_templates_for_team(team)
    else:
        qs = qs.filter(created_by_team__isnull=True, source="curated")
    query = Q()
    if vendor:
        query |= Q(manufacturer__icontains=vendor)
    if model:
        query |= Q(model_number__icontains=model) | Q(name__icontains=model)
    if signature:
        query |= Q(name__icontains=signature) | Q(manufacturer__icontains=signature)
    if query:
        qs = qs.filter(query).distinct()

    matches: list[TemplateMatch] = []
    for template in qs[:100]:
        score = 0
        reasons = []
        manufacturer = _norm(template.manufacturer)
        model_number = _norm(template.model_number)
        name = _norm(template.name)

        if protocol and template.protocol == protocol:
            score += 20
            reasons.append("protocol")
        if vendor and manufacturer == vendor:
            score += 35
            reasons.append("manufacturer")
        elif vendor and (vendor in manufacturer or vendor in _hint_values(template, "manufacturer_aliases")):
            score += 20
            reasons.append("manufacturer hint")
        if model and model_number == model:
            score += 40
            reasons.append("model")
        elif model and (model in name or model in _hint_values(template, "model_aliases")):
            score += 25
            reasons.append("model hint")
        if signature and (signature in name or signature in manufacturer):
            score += 15
            reasons.append("signature")
        for alias in _hint_values(template, "signatures"):
            if alias and alias in signature:
                score += 20
                reasons.append("signature hint")

        if score:
            matches.append(TemplateMatch(template=template, score=score, reasons=reasons))

    matches.sort(key=lambda item: item.score, reverse=True)
    return [
        {
            "template_id": match.template.id,
            "template_name": match.template.name,
            "score": match.score,
            "reasons": match.reasons,
        }
        for match in matches[:limit]
    ]


def enrich_discovered_device(discovery: dict, team=None) -> dict:
    enriched = dict(discovery)
    stable_key_parts = [
        enriched.get("connection") or enriched.get("protocol") or "unknown",
        enriched.get("interface") or enriched.get("port") or "",
        str(enriched.get("slave_id") or ""),
    ]
    enriched["stable_key"] = "|".join(str(part) for part in stable_key_parts)
    candidates = rank_templates_for_discovery(enriched, team=team)
    enriched["template_candidates"] = candidates
    if candidates:
        best = candidates[0]
        enriched["matched_template_id"] = best["template_id"]
        enriched["matched_template_name"] = best["template_name"]
        enriched["matched_template_score"] = best["score"]
        enriched["matched_template_reasons"] = best["reasons"]
    return enriched
