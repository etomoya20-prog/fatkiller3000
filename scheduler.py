"""Регулярные задачи: вечернее напоминание, ежедневная перекличка по анкетам,
субботний вопрос про вес, выгрузка в Google Sheets и недельная сводка в группу."""

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
import sheets
from config import MSK, Config
from handlers.group import invite_keyboard
from handlers.weighin import PROMPT as WEIGH_IN_PROMPT, WEIGH_KB

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


async def ask_weigh_in(bot: Bot) -> None:
    """Субботний вопрос про вес — в личку каждому, кто заполнил анкету.

    Групповое взвешивание никому не нужно: цифра личная, а в общий чат её всё
    равно никто не понесёт. Ответ бот ждёт не молча — человек подтверждает
    намерение кнопкой, иначе следующий отчёт о еде уехал бы в весы.
    """
    today = dt.datetime.now(MSK).date()
    targets = await db.users_for_weigh_in(today)
    log.info("Взвешивание: спрашиваю вес у %d человек", len(targets))

    for row in targets:
        sent = await _safe_send(bot, row["tg_id"], WEIGH_IN_PROMPT, reply_markup=WEIGH_KB)
        if sent:
            await db.mark_weight_asked(row["tg_id"], today)
        await asyncio.sleep(SEND_DELAY)


def _display_name(row) -> str:
    name = row["full_name"] or (f"@{row['username']}" if row["username"] else "Участник")
    return hlink(name, f"tg://user?id={row['tg_id']}")


async def _target_chats(cfg: Config) -> list[int]:
    """Куда шлём групповые сообщения: белый список из конфига или все живые чаты."""
    return cfg.group_chat_ids or await db.active_chats()


async def send_onboarding_nudge(bot: Bot, cfg: Config) -> None:
    """Раз в сутки перечисляет в группе тех, кто до сих пор не заполнил анкету.

    Личные напоминания таких людей не достают: без нормы считать нечего, и в
    users_without_entry они не попадают. Единственный способ их расшевелить —
    назвать вслух в группе. Если анкеты заполнили все, молчим: ежедневное
    сообщение «все молодцы» быстро превращается в шум.
    """
    chat_ids = await _target_chats(cfg)
    if not chat_ids:
        log.warning("Нет групп для переклички — бот ещё ни в одну не добавлен")
        return

    for chat_id in chat_ids:
        rows = await db.members_without_profile(chat_id)
        if not rows:
            log.info("Перекличка в %s: анкету заполнили все", chat_id)
            continue

        names = "\n".join(f"• {_display_name(row)}" for row in rows)
        text = (
            f"📋 <b>Ещё без анкеты: {len(rows)} чел.</b>\n\n"
            f"{names}\n\n"
            f"Без анкеты я не знаю вашей нормы — вы не попадаете ни в вечерние "
            f"напоминания, ни в воскресную сводку. Напишите мне в личку /start: "
            f"шесть вопросов, минута времени."
        )
        try:
            await bot.send_message(
                chat_id,
                text,
                reply_markup=await invite_keyboard(bot, chat_id),
                disable_web_page_preview=True,
            )
            log.info("Перекличка отправлена в %s (%d без анкеты)", chat_id, len(rows))
        except TelegramForbiddenError:
            log.info("Бота выгнали из чата %s", chat_id)
            await db.deactivate_chat(chat_id)
        except Exception:
            log.exception("Не удалось отправить перекличку в %s", chat_id)
        await asyncio.sleep(SEND_DELAY)


def _medal(index: int) -> str:
    return {0: "🥇", 1: "🥈", 2: "🥉"}.get(index, f"{index + 1}.")


def build_summary(rows, start: dt.date, end: dt.date, total_days: int) -> str:
    """Собирает текст недельной сводки."""
    # Блоки склеиваются через "\n\n", поэтому свои переводы строк на концах не ставим.
    header = (
        f"📊 <b>Итоги недели</b>\n"
        f"<i>{start.strftime('%d.%m')} — {end.strftime('%d.%m')}</i>"
    )

    if not rows:
        return header + "\n\nЗа эту неделю данных нет. Заполните анкету у бота в личке — /start"

    lines = [header]
    has_newcomers = False
    for index, row in enumerate(rows):
        on_track = row["on_track_days"]
        missed = row["missed_days"]
        avg = row["avg_kcal"]
        # У вступивших среди недели знаменатель меньше: считаем со дня вступления.
        tracked = row["tracked_days"]
        if tracked < total_days:
            has_newcomers = True

        parts = [f"{_medal(index)} {_display_name(row)}"]
        suffix = " <i>(с момента вступления)</i>" if tracked < total_days else ""
        parts.append(f"    В норме: <b>{on_track}</b> из {tracked} дн.{suffix}")
        if avg is not None:
            parts.append(f"    Средние калории: {int(avg)} при норме {row['kcal_norm']}")
        if missed:
            parts.append(f"    ⚠️ Не отчитался: <b>{missed}</b> дн.")
        lines.append("\n".join(parts))

    perfect = [
        r for r in rows
        if r["missed_days"] == 0
        and r["tracked_days"] == total_days
        and r["on_track_days"] == total_days
    ]
    if perfect:
        names = ", ".join(r["full_name"] or "участник" for r in perfect)
        lines.append(f"🏆 Идеальная неделя: {names}. Так держать!")

    silent = [r for r in rows if r["reported_days"] == 0]
    if silent:
        lines.append(
            f"😴 Всю неделю молчали: {len(silent)} чел. "
            f"Ребята, без данных я не могу помочь."
        )

    footer = "<i>День засчитан, если калории уложились в ±10% от нормы."
    if has_newcomers:
        footer += " У недавно вступивших счёт идёт со дня прихода в группу."
    lines.append(footer + "</i>")
    return "\n\n".join(lines)


async def send_weekly_summary(bot: Bot, cfg: Config) -> None:
    """Публикует сводку за последние 7 дней во все известные группы."""
    end = dt.datetime.now(MSK).date()
    start = end - dt.timedelta(days=6)
    total_days = 7

    chat_ids = await _target_chats(cfg)
    if not chat_ids:
        log.warning("Нет групп для сводки — бот ещё ни в одну не добавлен")
        return
    log.info("Рассылаю сводку в %d групп(ы)", len(chat_ids))

    for chat_id in chat_ids:
        rows = await db.weekly_stats(
            chat_id, start, end, cfg.tolerance, cfg.history_start
        )
        # Неделя, в которой не отчитался вообще никто, — это не итоги, а список
        # из восьми нулей. Такое бывает после обнуления статистики или на старте
        # группы; молчим по той же причине, по которой молчит перекличка.
        # Пустой список значит, что считать пока не по кому: анкет нет или неделя
        # целиком лежит до history_start. Звать в анкету — работа переклички,
        # она делает это каждый вечер и адресно.
        if not rows or all(row["reported_days"] == 0 for row in rows):
            log.info("Сводка в %s пропущена: за неделю нет ни одной записи", chat_id)
            continue

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


async def run_export() -> None:
    """Перезаливает Google-таблицу. Ошибку глушим: упавший экспорт не повод
    ронять планировщик, а таблица догонит данные на следующем прогоне."""
    try:
        await sheets.export()
    except Exception:
        log.exception("Не удалось обновить Google-таблицу")


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
        send_onboarding_nudge,
        CronTrigger(hour=cfg.nudge_hour, minute=cfg.nudge_minute, timezone=MSK),
        args=[bot, cfg],
        id="onboarding_nudge",
        misfire_grace_time=3600,
        coalesce=True,
        replace_existing=True,
    )

    if sheets.enabled():
        # Отчёты приходят вечером, особенно после напоминания в 21:00, поэтому
        # таблицу обновляем каждые полчаса с 20:00. Грейс короче интервала:
        # опоздавший прогон всё равно перекроет следующий.
        scheduler.add_job(
            run_export,
            CronTrigger(
                hour=f"{cfg.export_from_hour}-23",
                minute=f"*/{cfg.export_every_minutes}",
                timezone=MSK,
            ),
            id="sheets_export",
            misfire_grace_time=cfg.export_every_minutes * 60,
            coalesce=True,
            replace_existing=True,
        )
        # Последний прогон перед полуночью, чтобы в таблицу попали и отчёты,
        # присланные после 23:30, а не ждали следующего вечера.
        scheduler.add_job(
            run_export,
            CronTrigger(hour=23, minute=55, timezone=MSK),
            id="sheets_export_final",
            misfire_grace_time=3600,
            coalesce=True,
            replace_existing=True,
        )

    scheduler.add_job(
        ask_weigh_in,
        CronTrigger(
            day_of_week=cfg.weighin_day,
            hour=cfg.weighin_hour,
            minute=cfg.weighin_minute,
            timezone=MSK,
        ),
        args=[bot],
        id="weigh_in_prompt",
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
