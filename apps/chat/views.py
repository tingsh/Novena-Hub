from django.contrib.auth.decorators import login_required
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404
from django.template.response import TemplateResponse

from apps.chat.models import Chat
from apps.teams.roles import visible_site_ids
from apps.web.meta import websocket_absolute_url, websocket_reverse


@login_required
def chat_home(request):
    if request.team and visible_site_ids(request.user, request.team) is not None:
        return HttpResponseForbidden()
    chats = request.user.chats.order_by("-updated_at")
    return TemplateResponse(
        request,
        "chat/chat_home.html",
        {
            "active_tab": "ai-chat",
            "chats": chats,
        },
    )


@login_required
def new_chat_streaming(request):
    if request.team and visible_site_ids(request.user, request.team) is not None:
        return HttpResponseForbidden()
    websocket_url = websocket_absolute_url(websocket_reverse("ws_openai_new_chat"))
    return TemplateResponse(
        request,
        "chat/single_chat_streaming.html",
        {
            "active_tab": "ai-chat",
            "websocket_url": websocket_url,
        },
    )


@login_required
def single_chat_streaming(request, chat_id: int):
    if request.team and visible_site_ids(request.user, request.team) is not None:
        return HttpResponseForbidden()
    chat = get_object_or_404(Chat, user=request.user, id=chat_id)
    websocket_url = websocket_absolute_url(websocket_reverse("ws_openai_continue_chat", args=[chat_id]))
    from apps.chat.models import ChatUsage

    usage_count = ChatUsage.get_count_for_team(request.team)
    return TemplateResponse(
        request,
        "chat/single_chat_streaming.html",
        {
            "active_tab": "ai-chat",
            "websocket_url": websocket_url,
            "chat": chat,
            "usage_count": usage_count,
        },
    )
