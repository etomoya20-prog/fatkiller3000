"""Допуск бота в группы: работать в чате он начинает только с согласия владельца.

Добавить бота в группу Telegram разрешает кому угодно. Поэтому в неодобренном
чате бот молчит и никого не учитывает (фильтр на роутере в handlers/group.py
и условие approved_at в запросах db.py), а владельцу в личку уходит запрос
с кнопками «Разрешить» и «Отклонить».
"""

from __future__ import annotations

import html
import logging

from aiogram import Bot, F, Router
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.markdown import hlink

import db
from config import Config

log = logging.getLogger(__name__)

router = Router(name="approval")

GROUP_TYPES = {ChatType.GROUP, ChatType.SUPERGROUP}
PRESENT = {ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR}
GONE = {ChatMemberStatus.LEFT, ChatMemberStatus.KICKED}


async def is_approved_chat(event: Message | ChatMemberUpdated) -> bool:
    """Фильтр для роутера группы: пропускает события только из одобренных чатов."""
    return await db.is_chat_approved(event.chat.id)


async def _announce(bot: Bot, chat_id: int, is_admin: bool) -> None:
    note = ""
    if not is_admin:
        note = (
            "\n\n⚠️ Сделайте меня администратором — без этого Telegram не сообщает "
            "мне о новых участниках, и я не смогу их встречать."
        )
    await bot.send_message(
        chat_id,
        "Я на месте. Буду встречать новичков, вести учёт калорий в личке "
        "и раз в неделю публиковать здесь сводку." + note,
    )


async def _request_approval(bot: Bot, cfg: Config, event: ChatMemberUpdated) -> None:
    if cfg.owner_id is None:
        log.warning(
            "Бота добавили в %s (%s), но OWNER_ID не задан — одобрить некому",
            event.chat.id, event.chat.title,
        )
        return

    who = event.from_user
    added_by = hlink(who.full_name, f"tg://user?id={who.id}")
    if who.username:
        added_by += f" (@{who.username})"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Разрешить", callback_data=f"chat:ok:{event.chat.id}"),
        InlineKeyboardButton(text="🚫 Отклонить", callback_data=f"chat:no:{event.chat.id}"),
    ]])
    try:
        await bot.send_message(
            cfg.owner_id,
            f"🔐 <b>Меня добавили в группу</b>\n\n"
            f"Группа: {html.escape(event.chat.title or 'без названия')}\n"
            f"ID: <code>{event.chat.id}</code>\n"
            f"Добавил: {added_by}\n\n"
            f"Пока вы не ответите, я там молчу и никого не учитываю.",
            reply_markup=keyboard,
        )
    except TelegramAPIError:
        # Чаще всего владелец ещё ни разу не писал боту, и Telegram не даёт начать диалог.
        log.exception("Не удалось отправить владельцу запрос на группу %s", event.chat.id)


@router.my_chat_member(F.chat.type.in_(GROUP_TYPES))
async def on_bot_status_changed(event: ChatMemberUpdated, bot: Bot, cfg: Config) -> None:
    """Бота добавили в группу, повысили или выгнали."""
    status = event.new_chat_member.status
    if status in GONE:
        await db.deactivate_chat(event.chat.id)
        return
    if status not in PRESENT:
        return

    await db.upsert_chat(event.chat.id, event.chat.title)
    # Владелец, сам добавивший бота, своё согласие уже дал.
    if event.from_user.id == cfg.owner_id:
        await db.approve_chat(event.chat.id)

    if await db.is_chat_approved(event.chat.id):
        await _announce(bot, event.chat.id, status == ChatMemberStatus.ADMINISTRATOR)
        return

    # Повышение до администратора в ожидающей группе — не повод спрашивать второй раз.
    if event.old_chat_member.status in PRESENT:
        return

    await bot.send_message(
        event.chat.id,
        "Меня добавили в группу, но работать здесь я начну только после "
        "подтверждения владельца бота. Запрос ему уже отправлен.",
    )
    await _request_approval(bot, cfg, event)


@router.callback_query(F.data.startswith("chat:"))
async def on_decision(query: CallbackQuery, bot: Bot, cfg: Config) -> None:
    if query.from_user.id != cfg.owner_id:
        await query.answer("Решать может только владелец бота", show_alert=True)
        return

    _, action, raw_chat_id = query.data.split(":")
    chat_id = int(raw_chat_id)

    if action == "ok":
        await db.approve_chat(chat_id)
        try:
            me = await bot.get_chat_member(chat_id, bot.id)
            await _announce(bot, chat_id, me.status == ChatMemberStatus.ADMINISTRATOR)
            result = "✅ Разрешено. Я поздоровался в группе и начинаю работу."
        except TelegramAPIError:
            log.warning("Группа %s одобрена, но написать в неё не получилось", chat_id)
            result = "✅ Разрешено, но написать в группу не получилось — возможно, меня оттуда уже убрали."
        log.info("Владелец одобрил группу %s", chat_id)
    else:
        try:
            await bot.leave_chat(chat_id)
        except TelegramAPIError:
            log.warning("Не удалось выйти из группы %s", chat_id)
        await db.deactivate_chat(chat_id)
        result = "🚫 Отклонено, я вышел из группы."
        log.info("Владелец отклонил группу %s", chat_id)

    await query.message.edit_text(f"{query.message.html_text}\n\n{result}", reply_markup=None)
    await query.answer()


@router.message(F.migrate_from_chat_id)
async def on_migrated_here(message: Message) -> None:
    """Группа стала супергруппой: сообщение приходит уже в новый чат."""
    await db.transfer_approval(message.migrate_from_chat_id, message.chat.id, message.chat.title)


@router.message(F.migrate_to_chat_id)
async def on_migrated_away(message: Message) -> None:
    """То же событие со стороны старого чата — какое из двух придёт первым, неизвестно."""
    await db.transfer_approval(message.chat.id, message.migrate_to_chat_id, None)
