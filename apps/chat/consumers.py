import json
import logging

import litellm
from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from apps.chat.models import Chat, ChatMessage, ChatUsage, MessageTypes
from apps.chat.system_prompt import get_system_prompt
from apps.chat.tasks import set_chat_name
from apps.chat.tool_executor import NovenaToolExecutor
from apps.chat.tools import get_tools_definition
from apps.chat.utils import get_llm_kwargs
from apps.teams.models import Team

logger = logging.getLogger(__name__)


class ChatConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.user = self.scope["user"]
        if not self.user.is_authenticated:
            await self.close()
            return

        chat_id = self.scope["url_route"]["kwargs"].get("chat_id", None)

        # Resolve Team
        team_id = self.scope.get("session", {}).get("team")
        if team_id:
            self.team = await Team.objects.filter(id=team_id, members=self.user, status=Team.Status.ACTIVE).afirst()
        else:
            self.team = await Team.objects.filter(members=self.user, status=Team.Status.ACTIVE).afirst()

        if not self.team:
            await self.close()
            return

        from apps.teams.roles import visible_site_ids

        if await database_sync_to_async(visible_site_ids)(self.user, self.team) is not None:
            # The assistant tool executor operates on the whole team.
            await self.close()
            return

        if chat_id:
            try:
                self.chat = await Chat.objects.aget(user=self.user, id=chat_id)
                self.messages = [m.to_openai_dict() async for m in ChatMessage.objects.filter(chat=self.chat)]
            except Chat.DoesNotExist:
                await self.close()
                return
        else:
            self.chat = None
            self.messages = []

        await self.accept()

    async def disconnect(self, close_code):
        pass

    async def receive(self, text_data):
        text_data_json = json.loads(text_data)
        message_text = text_data_json["message"]

        if not message_text.strip():
            return

        if not self.chat:
            self.chat = await Chat.objects.acreate(user=self.user, team=self.team)
            set_chat_name.delay(self.chat.id, message_text)
            await self.send(text_data=json.dumps({"pushURL": reverse("chat:single_chat", args=[self.chat.id])}))

        # Increment Usage
        await database_sync_to_async(ChatUsage.increment_count_for_team)(self.team)

        # Save user message
        message = await self._save_message(message_text, MessageTypes.HUMAN)

        # Show user message
        user_message_html = render_to_string(
            "chat/websocket_components/user_message.html",
            {"message_text": message_text},
        )
        await self.send(text_data=user_message_html)

        # Show empty system message for streaming
        contents_div_id = f"message-response-{message.id}"
        system_message_html = render_to_string(
            "chat/websocket_components/system_message.html",
            {"contents_div_id": contents_div_id},
        )
        await self.send(text_data=system_message_html)

        # Full context with System Prompt
        context_messages = [{"role": "system", "content": get_system_prompt(self.team, self.user)}] + self.messages

        try:
            await self._process_completion(contents_div_id, context_messages)
        except Exception as e:
            logger.exception(e)
            error_html = render_to_string(
                "chat/websocket_components/final_system_message.html",
                {
                    "contents_div_id": contents_div_id,
                    "message": _("Sorry, there was an error processing your request."),
                },
            )
            await self.send(text_data=error_html)

    async def _process_completion(self, contents_div_id, context_messages):
        executor = NovenaToolExecutor(self.team)
        tools = get_tools_definition()

        # Tool-calling loop
        while True:
            response = await litellm.acompletion(
                messages=context_messages, tools=tools, tool_choice="auto", **get_llm_kwargs()
            )

            response_message = response.choices[0].message
            tool_calls = response_message.get("tool_calls")

            if tool_calls:
                # Add assistant message with tool calls to context
                context_messages.append(response_message)

                # Execute tools
                for tool_call in tool_calls:
                    function_name = tool_call.function.name
                    function_args = json.loads(tool_call.function.arguments)

                    # Update UI that we're searching data
                    query_label = function_name.replace("_", " ")
                    oob_html = (
                        f'<div hx-swap-oob="beforeend:#{contents_div_id}"><i>(Querying {query_label}...)</i><br></div>'
                    )
                    await self.send(text_data=oob_html)

                    result = await database_sync_to_async(executor.execute)(function_name, function_args)

                    context_messages.append(
                        {
                            "tool_call_id": tool_call.id,
                            "role": "tool",
                            "name": function_name,
                            "content": json.dumps(result),
                        }
                    )
                # Continue loop to get final answer
                continue
            else:
                # No tool calls, stream final response
                final_text = await self._stream_final_response(contents_div_id, context_messages)
                await self._save_message(final_text, MessageTypes.AI)
                break

    async def _stream_final_response(self, contents_div_id, context_messages):
        response = await litellm.acompletion(messages=context_messages, stream=True, **get_llm_kwargs())
        chunks = []
        async for chunk in response:
            content = chunk.choices[0].delta.content
            if content:
                chunks.append(content)
                html = f'<div hx-swap-oob="beforeend:#{contents_div_id}">{_format_token(content)}</div>'
                await self.send(text_data=html)

        full_text = "".join(chunks)
        # Final replacement for markdown rendering
        final_html = render_to_string(
            "chat/websocket_components/final_system_message.html",
            {"contents_div_id": contents_div_id, "message": full_text},
        )
        await self.send(text_data=final_html)
        return full_text

    async def _save_message(self, message_text, message_type):
        message = await ChatMessage.objects.acreate(
            chat=self.chat,
            message_type=message_type,
            content=message_text,
        )
        self.messages.append(message.to_openai_dict())
        return message


def _format_token(token: str) -> str:
    return token.replace("\n", "<br>")
