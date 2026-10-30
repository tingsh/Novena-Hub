from django.urls import path

from . import views

app_name = "maintenance"

urlpatterns = [
    # Tickets
    path("tickets/", views.TicketListView.as_view(), name="ticket_list"),
    path("tickets/create/", views.TicketCreateView.as_view(), name="ticket_create"),
    path("tickets/<int:pk>/", views.TicketDetailView.as_view(), name="ticket_detail"),
    path("tickets/<int:pk>/edit/", views.TicketUpdateView.as_view(), name="ticket_edit"),
    path("tickets/<int:pk>/status/", views.update_ticket_status, name="ticket_status_update"),
    path(
        "tickets/<int:pk>/checklist/<int:item_index>/toggle/",
        views.toggle_checklist_item,
        name="ticket_checklist_toggle",
    ),
    path("tickets/<int:pk>/comment/", views.add_ticket_comment, name="ticket_comment"),
    path("tickets/<int:pk>/share/", views.generate_shared_link, name="generate_shared_link"),
    path("tickets/<int:pk>/share/<int:link_pk>/revoke/", views.revoke_shared_link, name="revoke_shared_link"),
    # Templates
    path("templates/", views.TemplateListView.as_view(), name="template_list"),
    path("templates/create/", views.TemplateCreateView.as_view(), name="template_create"),
    path("templates/<int:pk>/edit/", views.TemplateUpdateView.as_view(), name="template_edit"),
    # Preventive Schedules
    path("schedules/", views.ScheduleListView.as_view(), name="schedule_list"),
    path("schedules/create/", views.ScheduleCreateView.as_view(), name="schedule_create"),
    path("schedules/<int:pk>/edit/", views.ScheduleUpdateView.as_view(), name="schedule_edit"),
    path("schedules/<int:pk>/trigger/", views.trigger_preventive_schedule, name="schedule_trigger"),
]
