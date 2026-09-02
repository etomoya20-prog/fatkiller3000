"""Точка входа: поднимает БД, бота и планировщик."""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import BotCommand, Message

import db
import llm
import scheduler as scheduler_module
import sheets
from config import Config, load_config
from handlers import group, intake, onboarding

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("fatkiller3000")

# Кому можно дёргать рассылки руками — удобно для проверки после деплоя.
ADMIN_IDS = {
    int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x
}

admin_router = Router(name="admin")


@admin_router.message(Command("force_summary"))
async def force_summary(message: Message, bot: Bot, cfg: Config) -> None:
    if message.from_user.id not in ADMIN_IDS:
        return
    await scheduler_module.send_weekly_summary(bot, cfg)
    await message.answer("Сводка разослана.")


@admin_router.message(Command("force_reminder"))
async def force_reminder(message: Message, bot: Bot) -> None:
    if message.from_user.id not in ADMIN_IDS:
        return
    await scheduler_module.send_reminders(bot)
    await message.answer("Напоминания разосланы.")


@admin_router.message(Command("force_nudge"))
async def force_nudge(message: Message, bot: Bot, cfg: Config) -> None:
    if message.from_user.id not in ADMIN_IDS:
        return
    await scheduler_module.send_onboarding_nudge(bot, cfg)
    await message.answer("Перекличка по анкетам отправлена.")


@admin_router.message(Command("export"))
async def force_export(message: Message) -> None:
    if message.from_user.id not in ADMIN_IDS:
        return
    if not sheets.enabled():
        await message.answer(
            "Выгрузка не настроена: нет GOOGLE_SHEET_ID или ключа сервисного аккаунта."
        )
        return

    await message.answer("Обновляю таблицу…")
    try:
        url = await sheets.export()
    except Exception as exc:
        log.exception("Ручная выгрузка не удалась")
        await message.answer(f"Не получилось: {exc}")
        return
    await message.answer(f"Готово: {url}")


@admin_router.message(Command("chatid"))
async def chat_id(message: Message) -> None:
    if message.from_user.id not in ADMIN_IDS:
        return
    await message.answer(f"<code>{message.chat.id}</code>")


async def set_commands(bot: Bot) -> None:
    await bot.set_my_commands([
        BotCommand(command="start", description="Начать и заполнить анкету"),
        BotCommand(command="today", description="Итог за сегодня"),
        BotCommand(command="profile", description="Моя норма калорий и БЖУ"),
        BotCommand(command="reset", description="Стереть записи за сегодня"),
        BotCommand(command="again", description="Заполнить анкету заново"),
        BotCommand(command="help", description="Как пользоваться"),
    ])


async def main() -> None:
    cfg = load_config()

    await db.init(cfg.db_dsn)
    llm.init(cfg.openai_api_key, cfg.openai_model)
    sheets.init(cfg.google_credentials_file, cfg.google_sheet_id, cfg.tolerance)

    bot = Bot(
        token=cfg.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    dispatcher = Dispatcher()
    # cfg приезжает в хендлеры аргументом — так админские команды видят конфиг.
    dispatcher["cfg"] = cfg

    # Порядок важен: intake ловит любой текст, поэтому идёт последним.
    dispatcher.include_router(admin_router)
    dispatcher.include_router(group.router)
    dispatcher.include_router(onboarding.router)
    dispatcher.include_router(intake.router)

    scheduler = scheduler_module.build_scheduler(bot, cfg)
    scheduler.start()

    await set_commands(bot)

    me = await bot.get_me()
    log.info("Бот @%s запущен", me.username)

    try:
        await dispatcher.start_polling(
            bot,
            # chat_member приходит, только если запросить его явно и дать боту админку.
            allowed_updates=[
                "message",
                "callback_query",
                "chat_member",
                "my_chat_member",
            ],
        )
    finally:
        scheduler.shutdown(wait=False)
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Остановлен")
