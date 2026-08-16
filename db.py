"""Доступ к PostgreSQL. БД живёт на хосте, контейнер ходит через host.docker.internal."""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path
from typing import Any

import asyncpg

log = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None


async def init(dsn: str) -> None:
    """Поднимает пул и накатывает схему."""
    global _pool
    _pool = await asyncpg.create_pool(dsn, min_size=1, max_size=5, command_timeout=30)
    schema = (Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")
    async with _pool.acquire() as conn:
        await conn.execute(schema)
    log.info("БД готова")


async def close() -> None:
    if _pool is not None:
        await _pool.close()


def pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Пул БД не инициализирован — вызовите db.init() до работы с БД")
    return _pool


# --------------------------------------------------------------------------
# Пользователи
# --------------------------------------------------------------------------

async def upsert_user(tg_id: int, username: str | None, full_name: str | None) -> None:
    await pool().execute(
        """
        INSERT INTO users (tg_id, username, full_name)
        VALUES ($1, $2, $3)
        ON CONFLICT (tg_id) DO UPDATE
            SET username   = EXCLUDED.username,
                full_name  = EXCLUDED.full_name,
                is_active  = TRUE,
                updated_at = now()
        """,
        tg_id, username, full_name,
    )


async def get_user(tg_id: int) -> asyncpg.Record | None:
    return await pool().fetchrow("SELECT * FROM users WHERE tg_id = $1", tg_id)


async def save_profile(tg_id: int, profile: dict[str, Any]) -> None:
    """Сохраняет анкету и рассчитанные нормы, помечая анкету завершённой."""
    await pool().execute(
        """
        UPDATE users
           SET gender           = $2,
               age              = $3,
               height_cm        = $4,
               weight_kg        = $5,
               target_weight_kg = $6,
               activity         = $7,
               kcal_norm        = $8,
               protein_g        = $9,
               fat_g            = $10,
               carb_g           = $11,
               onboarded_at     = now(),
               updated_at       = now()
         WHERE tg_id = $1
        """,
        tg_id,
        profile["gender"], profile["age"], profile["height_cm"],
        profile["weight_kg"], profile["target_weight_kg"], profile["activity"],
        profile["kcal_norm"], profile["protein_g"], profile["fat_g"], profile["carb_g"],
    )


async def deactivate_user(tg_id: int) -> None:
    """Пользователь заблокировал бота — больше не пишем ему."""
    await pool().execute(
        "UPDATE users SET is_active = FALSE, updated_at = now() WHERE tg_id = $1", tg_id
    )


# --------------------------------------------------------------------------
# Группы
# --------------------------------------------------------------------------

async def upsert_chat(chat_id: int, title: str | None) -> None:
    await pool().execute(
        """
        INSERT INTO chats (chat_id, title)
        VALUES ($1, $2)
        ON CONFLICT (chat_id) DO UPDATE
            SET title = EXCLUDED.title, is_active = TRUE
        """,
        chat_id, title,
    )


async def deactivate_chat(chat_id: int) -> None:
    await pool().execute("UPDATE chats SET is_active = FALSE WHERE chat_id = $1", chat_id)


async def active_chats() -> list[int]:
    rows = await pool().fetch("SELECT chat_id FROM chats WHERE is_active ORDER BY chat_id")
    return [r["chat_id"] for r in rows]


async def add_group_member(chat_id: int, tg_id: int) -> None:
    await pool().execute(
        """
        INSERT INTO group_members (chat_id, tg_id)
        VALUES ($1, $2)
        ON CONFLICT (chat_id, tg_id) DO UPDATE
            SET left_at = NULL, joined_at = now()
        """,
        chat_id, tg_id,
    )


async def remove_group_member(chat_id: int, tg_id: int) -> None:
    await pool().execute(
        "UPDATE group_members SET left_at = now() WHERE chat_id = $1 AND tg_id = $2",
        chat_id, tg_id,
    )


async def claim_greeting(chat_id: int, tg_id: int) -> bool:
    """Резервирует право поприветствовать человека в этом чате.

    О вступлении Telegram сообщает дважды — апдейтом chat_member и сервисным
    сообщением, — поэтому обработчики гонятся за одно и то же приветствие.
    Побеждает тот, чей UPDATE вернул строку; остальные получают False.
    Час — окно, после которого повторный вход считается новым событием.
    """
    row = await pool().fetchrow(
        """
        UPDATE group_members
           SET greeted_at = now()
         WHERE chat_id = $1
           AND tg_id = $2
           AND (greeted_at IS NULL OR greeted_at < now() - interval '1 hour')
        RETURNING tg_id
        """,
        chat_id, tg_id,
    )
    return row is not None


async def is_in_any_group(tg_id: int) -> bool:
    row = await pool().fetchrow(
        """
        SELECT 1
          FROM group_members gm
          JOIN chats c ON c.chat_id = gm.chat_id
         WHERE gm.tg_id = $1 AND gm.left_at IS NULL AND c.is_active
         LIMIT 1
        """,
        tg_id,
    )
    return row is not None


# --------------------------------------------------------------------------
# Записи о еде
# --------------------------------------------------------------------------

async def add_entry(
    tg_id: int,
    log_date: dt.date,
    kcal: float,
    protein_g: float | None,
    fat_g: float | None,
    carb_g: float | None,
    is_full_day: bool,
    source: str,
    raw_input: str | None,
    llm_note: str | None,
) -> None:
    """Полный дневной отчёт вытесняет накопленные за день записи, приём пищи — добавляется."""
    async with pool().acquire() as conn:
        async with conn.transaction():
            if is_full_day:
                await conn.execute(
                    "DELETE FROM entries WHERE tg_id = $1 AND log_date = $2", tg_id, log_date
                )
            await conn.execute(
                """
                INSERT INTO entries (tg_id, log_date, kcal, protein_g, fat_g, carb_g,
                                     is_full_day, source, raw_input, llm_note)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                """,
                tg_id, log_date, kcal, protein_g, fat_g, carb_g,
                is_full_day, source, raw_input, llm_note,
            )


async def day_totals(tg_id: int, log_date: dt.date) -> asyncpg.Record:
    return await pool().fetchrow(
        """
        SELECT COALESCE(SUM(kcal), 0)      AS kcal,
               COALESCE(SUM(protein_g), 0) AS protein_g,
               COALESCE(SUM(fat_g), 0)     AS fat_g,
               COALESCE(SUM(carb_g), 0)    AS carb_g,
               COUNT(*)                    AS entries
          FROM entries
         WHERE tg_id = $1 AND log_date = $2
        """,
        tg_id, log_date,
    )


async def clear_day(tg_id: int, log_date: dt.date) -> int:
    result = await pool().execute(
        "DELETE FROM entries WHERE tg_id = $1 AND log_date = $2", tg_id, log_date
    )
    return int(result.split()[-1])


# --------------------------------------------------------------------------
# Напоминания
# --------------------------------------------------------------------------

async def users_without_entry(log_date: dt.date) -> list[asyncpg.Record]:
    """Прошедшие анкету активные пользователи, от которых за дату нет ни одной записи
    и которым сегодня ещё не напоминали.

    Напоминание — одно на человека в день, даже если он состоит в нескольких
    группах: дневник у него общий. Вышедшим из всех групп не пишем."""
    return await pool().fetch(
        """
        SELECT u.tg_id, u.full_name
          FROM users u
         WHERE u.is_active
           AND u.onboarded_at IS NOT NULL
           AND EXISTS (SELECT 1 FROM group_members gm
                         JOIN chats c ON c.chat_id = gm.chat_id
                        WHERE gm.tg_id = u.tg_id
                          AND gm.left_at IS NULL
                          AND c.is_active)
           AND NOT EXISTS (SELECT 1 FROM entries e
                            WHERE e.tg_id = u.tg_id AND e.log_date = $1)
           AND NOT EXISTS (SELECT 1 FROM reminders r
                            WHERE r.tg_id = u.tg_id AND r.remind_date = $1)
        """,
        log_date,
    )


async def mark_reminded(tg_id: int, remind_date: dt.date) -> None:
    await pool().execute(
        """
        INSERT INTO reminders (tg_id, remind_date) VALUES ($1, $2)
        ON CONFLICT DO NOTHING
        """,
        tg_id, remind_date,
    )


# --------------------------------------------------------------------------
# Недельная сводка
# --------------------------------------------------------------------------

async def weekly_stats(
    chat_id: int, start: dt.date, end: dt.date, tolerance: float
) -> list[asyncpg.Record]:
    """По каждому участнику группы за период [start, end]:
    сколько дней уложился в коридор нормы, сколько дней вообще не отчитался,
    и среднее по калориям за дни с данными."""
    return await pool().fetch(
        """
        WITH members AS (
            SELECT u.tg_id, u.full_name, u.username, u.kcal_norm
              FROM group_members gm
              JOIN users u ON u.tg_id = gm.tg_id
             WHERE gm.chat_id = $1
               AND gm.left_at IS NULL
               AND u.onboarded_at IS NOT NULL
               AND u.kcal_norm IS NOT NULL
        ),
        days AS (
            SELECT m.tg_id, d::date AS log_date
              FROM members m
              CROSS JOIN generate_series($2::date, $3::date, interval '1 day') AS d
        ),
        totals AS (
            SELECT d.tg_id,
                   d.log_date,
                   SUM(e.kcal) AS kcal
              FROM days d
              LEFT JOIN entries e ON e.tg_id = d.tg_id AND e.log_date = d.log_date
             GROUP BY d.tg_id, d.log_date
        )
        SELECT m.tg_id,
               m.full_name,
               m.username,
               m.kcal_norm,
               COUNT(*) FILTER (WHERE t.kcal IS NULL)                       AS missed_days,
               COUNT(*) FILTER (
                   WHERE t.kcal IS NOT NULL
                     AND t.kcal BETWEEN m.kcal_norm * (1 - $4::numeric)
                                    AND m.kcal_norm * (1 + $4::numeric)
               )                                                            AS on_track_days,
               COUNT(*) FILTER (WHERE t.kcal IS NOT NULL)                   AS reported_days,
               ROUND(AVG(t.kcal) FILTER (WHERE t.kcal IS NOT NULL))         AS avg_kcal
          FROM members m
          JOIN totals t ON t.tg_id = m.tg_id
         GROUP BY m.tg_id, m.full_name, m.username, m.kcal_norm
         ORDER BY on_track_days DESC, missed_days ASC, m.full_name
        """,
        chat_id, start, end, tolerance,
    )
