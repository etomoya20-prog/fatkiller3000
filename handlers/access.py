"""Доступ к боту в личке: только для участников одобренных групп.

Бот ведёт дневник для группы худеющих, а не для всех, кто его нашёл. Поэтому
анкета, отчёты о еде и взвешивания в личке открыты только тем, кто состоит
хотя бы в одной группе, куда бота пустил владелец (см. handlers/approval.py).
Владелец и админы проходят всегда — иначе служебные команды стали бы зависеть
от их участия в группе.

Проверка стоит внешним middleware на весь диспетчер, а не фильтром на роутерах:
так новый обработчик в личке не сможет случайно её обойти.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.enums import ChatType
from aiogram.types import CallbackQuery, Message, TelegramObject

import db

log = logging.getLogger(__name__)

DENIED = (
    "Я работаю только для участников групп, где меня разрешил владелец.\n\n"
    "Если вы состоите в такой группе, напишите в ней любое сообщение или "
    "команду /join, а потом возвращайтесь сюда."
)
DENIED_SHORT = "Бот доступен только участникам его групп"


async def _bind_from_deeplink(message: Message) -> None:
    """Привязывает к группе человека, пришедшего по кнопке «Написать боту в личку».

    Делать это нужно до проверки доступа: новичок, которого бот ещё не видел
    в чате, иначе получил бы отказ, хотя пришёл ровно из одобренной группы.
    Диплинк можно собрать руками с любым ID, поэтому привязываем только
    к одобренной группе.
    """
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[0].startswith("/start") or not parts[1].startswith("group"):
        return
    try:
        chat_id = int(parts[1].removeprefix("group"))
    except ValueError:
        log.warning("Не разобрал chat_id из диплинка: %r", parts[1])
        return
    if await db.is_chat_approved(chat_id):
        await db.add_group_member(chat_id, message.from_user.id)


class PrivateAccessMiddleware(BaseMiddleware):
    def __init__(self, allowed_ids: set[int]) -> None:
        self.allowed_ids = allowed_ids

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, Message):
            chat = event.chat
        elif isinstance(event, CallbackQuery) and event.message:
            chat = event.message.chat
        else:
            return await handler(event, data)

        user = event.from_user
        if chat.type != ChatType.PRIVATE or user is None or user.id in self.allowed_ids:
            return await handler(event, data)

        if isinstance(event, Message):
            await _bind_from_deeplink(event)
        if await db.is_in_any_group(user.id):
            return await handler(event, data)

        log.info("Отказ в доступе в личке: %s (%s)", user.id, user.full_name)
        if isinstance(event, Message):
            await event.answer(DENIED)
        else:
            await event.answer(DENIED_SHORT, show_alert=True)
        return None
