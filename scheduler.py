"""Регулярные задачи: вечернее напоминание и недельная сводка в группу."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.utils.markdown import hlink
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

import db
from config import MSK, Config

log = logging.getLogger(__name__)

# Пауза между сообщениями, чтобы не упереться в лимиты Telegram на рассылку.
SEND_DELAY = 0.05


async def _safe_send(bot: Bot, chat_id: int, text: str, **kwargs) -> bool:
    """Отправка с обработкой блокировки бота и троттлинга."""
    try:
        await bot.send_message(chat_id, text, **kwargs)
        return True
    except TelegramForbiddenError:
        log.info("Пользователь %s заблокировал бота", chat_id)
        await db.deactivate_user(chat_id)
        return False
    except TelegramRetryAfter as exc:
        log.warning("Троттлинг на %s секунд", exc.retry_after)
        await asyncio.sleep(exc.retry_after)
        return await _safe_send(bot, chat_id, text, **kwargs)
    except Exception:
        log.exception("Не удалось отправить сообщение в %s", chat_id)
        return False


async def send_reminders(bot: Bot) -> None:
    """Пинает тех, кто за сегодня не прислал ни одной записи."""
    today = dt.datetime.now(MSK).date()
    targets = await db.users_without_entry(today)
    log.info("Напоминание: %d пользователей без записей за %s", len(targets), today)

    for row in targets:
        sent = await _safe_send(
            bot,
            row["tg_id"],
            "🔔 Привет! Ты сегодня ещё не отчитался о еде.\n\n"
            "Напиши, что съел за день — текстом, файлом из приложения или скриншотом. "
            "Займёт минуту, а неделя не будет испорчена пропуском.",
        )
        if sent:
            await db.mark_reminded(row["tg_id"], today)
        await asyncio.sleep(SEND_DELAY)


def _display_name(row) -> str:
    name = row["full_name"] or (f"@{row['username']}" if row["username"] else "Участник")
    return hlink(name, f"tg://user?id={row['tg_id']}")


def _medal(index: int) -> str:
    return {0: "🥇", 1: "🥈", 2: "🥉"}.get(index, f"{index + 1}.")


def build_summary(rows, start: dt.date, end: dt.date, total_days: int) -> str:
    """Собирает текст недельной сводки."""
    header = (
        f"📊 <b>Итоги недели</b>\n"
        f"<i>{start.strftime('%d.%m')} — {end.strftime('%d.%m')}</i>\n\n"
    )

    if not rows:
        return header + "За эту неделю данных нет. Заполните анкету у бота в личке — /start"

    lines = [header]
    for index, row in enumerate(rows):
        on_track = row["on_track_days"]
        missed = row["missed_days"]
        avg = row["avg_kcal"]

        parts = [f"{_medal(index)} {_display_name(row)}"]
        parts.append(f"    В норме: <b>{on_track}</b> из {total_days} дн.")
        if avg is not None:
            parts.append(f"    Средние калории: {int(avg)} при норме {row['kcal_norm']}")
        if missed:
            parts.append(f"    ⚠️ Не отчитался: <b>{missed}</b> дн.")
        lines.append("\n".join(parts))

    perfect = [r for r in rows if r["missed_days"] == 0 and r["on_track_days"] == total_days]
    if perfect:
        names = ", ".join(r["full_name"] or "участник" for r in perfect)
        lines.append(f"\n🏆 Идеальная неделя: {names}. Так держать!")

    silent = [r for r in rows if r["reported_days"] == 0]
    if silent:
        lines.append(
            f"\n😴 Всю неделю молчали: {len(silent)} чел. "
            f"Ребята, без данных я не могу помочь."
        )

    lines.append("\n<i>День засчитан, если калории уложились в ±10% от нормы.</i>")
    return "\n\n".join(lines)


async def send_weekly_summary(bot: Bot, cfg: Config) -> None:
    """Публикует сводку за последние 7 дней во все известные группы."""
    end = dt.datetime.now(MSK).date()
    start = end - dt.timedelta(days=6)
    total_days = 7

    chat_ids = cfg.group_chat_ids or await db.active_chats()
    if not chat_ids:
        log.warning("Нет групп для сводки — бот ещё ни в одну не добавлен")
        return
    log.info("Рассылаю сводку в %d групп(ы)", len(chat_ids))

    for chat_id in chat_ids:
        rows = await db.weekly_stats(chat_id, start, end, cfg.tolerance)
        text = build_summary(rows, start, end, total_days)
        try:
            await bot.send_message(chat_id, text, disable_web_page_preview=True)
            log.info("Сводка отправлена в %s (%d участников)", chat_id, len(rows))
        except TelegramForbiddenError:
            log.info("Бота выгнали из чата %s", chat_id)
            await db.deactivate_chat(chat_id)
        except Exception:
            log.exception("Не удалось отправить сводку в %s", chat_id)
        await asyncio.sleep(SEND_DELAY)


def build_scheduler(bot: Bot, cfg: Config) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=MSK)

    scheduler.add_job(
        send_reminders,
        CronTrigger(hour=cfg.reminder_hour, minute=cfg.reminder_minute, timezone=MSK),
        args=[bot],
        id="daily_reminder",
        # Если бот лежал во время срабатывания — досылаем в течение часа, но одним разом.
        misfire_grace_time=3600,
        coalesce=True,
        replace_existing=True,
    )

    scheduler.add_job(
        send_weekly_summary,
        CronTrigger(
            day_of_week=cfg.summary_day,
            hour=cfg.summary_hour,
            minute=cfg.summary_minute,
            timezone=MSK,
        ),
        args=[bot, cfg],
        id="weekly_summary",
        misfire_grace_time=3600,
        coalesce=True,
        replace_existing=True,
    )

    return scheduler
