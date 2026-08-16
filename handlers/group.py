"""Всё, что происходит в групповом чате: приход новых участников и приглашение в личку."""

from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.filters import (
    IS_MEMBER,
    IS_NOT_MEMBER,
    ChatMemberUpdatedFilter,
    Command,
)
from aiogram.types import (
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.markdown import hlink

import db

log = logging.getLogger(__name__)

router = Router(name="group")
router.message.filter(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))


async def _invite_keyboard(bot: Bot, chat_id: int) -> InlineKeyboardMarkup:
    """Кнопка-диплинк в личку: жать проще, чем вручную писать /start."""
    me = await bot.get_me()
    url = f"https://t.me/{me.username}?start=group{chat_id}"
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Написать боту в личку", url=url)]]
    )


def _mention(user) -> str:
    return hlink(user.full_name, f"tg://user?id={user.id}")


async def _greet_newcomer(bot: Bot, chat, user) -> None:
    """Регистрирует человека в группе и здоровается — ровно один раз на вступление.

    Вызывается из двух мест: апдейта chat_member и сервисного сообщения о входе.
    Когда бот администратор, Telegram присылает оба, поэтому право на приветствие
    разыгрывается в БД.
    """
    await db.upsert_chat(chat.id, chat.title)
    await db.upsert_user(user.id, user.username, user.full_name)
    await db.add_group_member(chat.id, user.id)

    if not await db.claim_greeting(chat.id, user.id):
        return

    profile = await db.get_user(user.id)
    if profile and profile["onboarded_at"]:
        # Человек уже вёл дневник в другой группе — анкету заново не гоняем.
        await bot.send_message(
            chat.id,
            f"{_mention(user)} снова с нами. Анкета уже заполнена, "
            f"норма на месте — продолжаем отчитываться в личке.",
        )
        return

    await bot.send_message(
        chat.id,
        f"{_mention(user)}, привет и добро пожаловать!\n\n"
        f"Чтобы я считал твои калории, напиши мне в личку команду /start — "
        f"задам шесть вопросов и рассчитаю дневную норму.",
        reply_markup=await _invite_keyboard(bot, chat.id),
    )


@router.chat_member(ChatMemberUpdatedFilter(IS_NOT_MEMBER >> IS_MEMBER))
async def on_user_joined(event: ChatMemberUpdated, bot: Bot) -> None:
    """Новый человек в группе — здороваемся и зовём в личку заполнять анкету."""
    user = event.new_chat_member.user
    if user.is_bot:
        return
    await _greet_newcomer(bot, event.chat, user)


@router.chat_member(ChatMemberUpdatedFilter(IS_MEMBER >> IS_NOT_MEMBER))
async def on_user_left(event: ChatMemberUpdated) -> None:
    """Человек вышел или его удалили — убираем из сводки, данные не трогаем."""
    user = event.new_chat_member.user
    if user.is_bot:
        return
    await db.remove_group_member(event.chat.id, user.id)


@router.my_chat_member()
async def on_bot_status_changed(event: ChatMemberUpdated, bot: Bot) -> None:
    """Бота добавили в группу или выгнали."""
    if event.chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
        return

    status = event.new_chat_member.status
    if status in {ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR}:
        await db.upsert_chat(event.chat.id, event.chat.title)
        note = ""
        if status != ChatMemberStatus.ADMINISTRATOR:
            note = (
                "\n\n⚠️ Сделайте меня администратором — без этого Telegram не сообщает "
                "мне о новых участниках, и я не смогу их встречать."
            )
        await bot.send_message(
            event.chat.id,
            "Я на месте. Буду встречать новичков, вести учёт калорий в личке "
            "и раз в неделю публиковать здесь сводку." + note,
        )
    elif status in {ChatMemberStatus.LEFT, ChatMemberStatus.KICKED}:
        await db.deactivate_chat(event.chat.id)


@router.message(F.new_chat_members)
async def on_new_chat_members_fallback(message: Message, bot: Bot) -> None:
    """Запасной путь: сервисное сообщение о входе приходит и без прав администратора
    на chat_member-апдейты."""
    for user in message.new_chat_members:
        if user.is_bot:
            continue
        await _greet_newcomer(bot, message.chat, user)


@router.message(F.left_chat_member)
async def on_left_chat_member_fallback(message: Message) -> None:
    user = message.left_chat_member
    if user and not user.is_bot:
        await db.remove_group_member(message.chat.id, user.id)


@router.message(Command("join"))
async def cmd_join(message: Message, bot: Bot) -> None:
    """Ручной вход для тех, кто был в группе до появления бота."""
    user = message.from_user
    await db.upsert_chat(message.chat.id, message.chat.title)
    await db.upsert_user(user.id, user.username, user.full_name)
    await db.add_group_member(message.chat.id, user.id)

    profile = await db.get_user(user.id)
    if profile and profile["onboarded_at"]:
        await message.answer(f"{_mention(user)}, записал тебя в участники. Анкета уже заполнена.")
        return

    await message.answer(
        f"{_mention(user)}, записал тебя в участники. "
        f"Теперь напиши мне в личку /start, чтобы заполнить анкету.",
        reply_markup=await _invite_keyboard(bot, message.chat.id),
    )


@router.message()
async def track_existing_member(message: Message) -> None:
    """Молча записывает автора сообщения в состав группы.

    О тех, кто состоял в группе до появления бота, Telegram задним числом не
    рассказывает и список участников выдать не даёт — единственный способ их
    узнать это увидеть их сообщение. Поэтому мы тихо отмечаем каждого пишущего:
    иначе старожилы не попадут ни в сводку, ни в напоминания, и им пришлось бы
    вручную звать /join. Ничего не отвечаем — это фоновая работа.
    """
    user = message.from_user
    if not user or user.is_bot:
        return
    await db.upsert_chat(message.chat.id, message.chat.title)
    await db.remember_user(user.id, user.username, user.full_name)
    await db.add_group_member(message.chat.id, user.id)
