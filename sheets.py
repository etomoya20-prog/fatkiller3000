"""Ежедневная выгрузка данных бота в Google Sheets.

Таблица переписывается целиком на каждом прогоне, а не дописывается. Так
задним числом подтягиваются исправления: отчёт, присланный после выгрузки,
попадёт в таблицу следующим вечером, а не потеряется навсегда. Побочный
плюс — выгрузка идемпотентна, её можно запускать руками сколько угодно раз.

Работа с Google синхронная и блокирующая, поэтому целиком уезжает в поток:
бот в это время должен продолжать отвечать людям.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
from decimal import Decimal
from pathlib import Path

import gspread

import db
from config import ACTIVITY_FACTORS, MSK

log = logging.getLogger(__name__)

SHEET_PARTICIPANTS = "Участники"
SHEET_DIARY = "Дневник"
SHEET_MATRIX = "По дням"

_credentials_file: str | None = None
_sheet_id: str | None = None
_tolerance = 0.10

GENDERS = {"male": "муж", "female": "жен"}


def init(credentials_file: str | None, sheet_id: str | None, tolerance: float) -> None:
    """Включает выгрузку, если заданы и ключ, и таблица."""
    global _credentials_file, _sheet_id, _tolerance
    _tolerance = tolerance

    if not credentials_file or not sheet_id:
        log.info("Выгрузка в Google Sheets выключена: не заданы ключ или ID таблицы")
        return

    # Ключ читаем сразу и целиком: неверный путь, чужие права или битый JSON
    # должны выключить выгрузку, а не уронить бота на старте. is_file() тут не
    # хватает — на закрытом каталоге он сам бросает PermissionError.
    try:
        json.loads(Path(credentials_file).read_text(encoding="utf-8"))
    except OSError as exc:
        log.error("Ключ сервисного аккаунта не прочитан (%s) — выгрузка выключена", exc)
        return
    except ValueError as exc:
        log.error("Ключ сервисного аккаунта не разобран (%s) — выгрузка выключена", exc)
        return

    _credentials_file = credentials_file
    _sheet_id = sheet_id
    log.info("Выгрузка в Google Sheets включена, таблица %s", sheet_id)


def enabled() -> bool:
    return _credentials_file is not None and _sheet_id is not None


def _num(value) -> float | str:
    """Числа отдаём числами, пустоту — пустой строкой, а не нулём.

    Ноль калорий и отсутствие отчёта — разные вещи: первое означает голодание,
    второе прогул. В таблице их нельзя смешивать, иначе средние поедут.
    """
    if value is None:
        return ""
    if isinstance(value, Decimal):
        value = float(value)
    return round(value, 1) if isinstance(value, float) else value


def _day(value: dt.datetime | None) -> str:
    """Дата в ISO: Google разбирает такой формат независимо от локали таблицы."""
    if value is None:
        return ""
    return value.astimezone(MSK).date().isoformat()


def _name(row) -> str:
    return row["full_name"] or (f"@{row['username']}" if row["username"] else str(row["tg_id"]))


def build_participants(rows) -> list[list]:
    header = [
        "Имя", "Username", "Telegram ID", "Пол", "Возраст", "Рост, см",
        "Вес, кг", "Цель, кг", "Осталось, кг", "Активность", "Норма, ккал",
        "Белки, г", "Жиры, г", "Углеводы, г", "Анкета заполнена", "В группе с",
        "Статус",
    ]
    values = [header]
    for row in sorted(rows, key=_name):
        weight = row["weight_kg"]
        target = row["target_weight_kg"]
        to_go = float(weight) - float(target) if weight is not None and target is not None else None
        activity = ACTIVITY_FACTORS.get(row["activity"] or "", (None, ""))[1]

        if not row["is_active"]:
            status = "заблокировал бота"
        elif row["onboarded_at"] is None:
            status = "нет анкеты"
        else:
            status = "активен"

        values.append([
            _name(row),
            f"@{row['username']}" if row["username"] else "",
            row["tg_id"],
            GENDERS.get(row["gender"] or "", ""),
            _num(row["age"]),
            _num(row["height_cm"]),
            _num(weight),
            _num(target),
            _num(to_go),
            activity,
            _num(row["kcal_norm"]),
            _num(row["protein_g"]),
            _num(row["fat_g"]),
            _num(row["carb_g"]),
            _day(row["onboarded_at"]),
            _day(row["joined_at"]),
            status,
        ])
    return values


def build_diary(rows, tolerance: float) -> list[list]:
    header = [
        "Дата", "Участник", "Ккал", "Норма, ккал", "% от нормы", "В норме",
        "Белки, г", "Жиры, г", "Углеводы, г", "Записей",
    ]
    values = [header]
    for row in rows:
        kcal = row["kcal"]
        norm = row["kcal_norm"]

        if kcal is None:
            percent: float | str = ""
            verdict = "нет отчёта"
        else:
            ratio = float(kcal) / norm if norm else 0
            percent = round(ratio * 100)
            verdict = "да" if 1 - tolerance <= ratio <= 1 + tolerance else "нет"

        values.append([
            row["log_date"].isoformat(),
            _name(row),
            _num(kcal),
            _num(norm),
            percent,
            verdict,
            _num(row["protein_g"]),
            _num(row["fat_g"]),
            _num(row["carb_g"]),
            row["entries"],
        ])
    return values


def build_matrix(rows) -> list[list]:
    """Сводная матрица «участник × дата» с калориями в ячейках.

    Собирается из тех же строк дневника, отдельного запроса не требует.

    Колонки — объединение дат по всем участникам, поэтому у новичка слева
    оказываются дни, когда его в проекте ещё не было. Прогул от «не участвовал»
    надо отличать: пропущенный день помечается прочерком, а чужой остаётся
    пустым. Иначе у вступившего вчера вся история выглядит сплошным прогулом.
    """
    dates: list[dt.date] = []
    seen: set[dt.date] = set()
    people: dict[int, str] = {}
    norms: dict[int, int | None] = {}
    cells: dict[tuple[int, dt.date], float] = {}
    tracked: set[tuple[int, dt.date]] = set()

    for row in rows:
        log_date = row["log_date"]
        tg_id = row["tg_id"]
        if log_date not in seen:
            seen.add(log_date)
            dates.append(log_date)
        people.setdefault(tg_id, _name(row))
        norms.setdefault(tg_id, row["kcal_norm"])
        tracked.add((tg_id, log_date))
        if row["kcal"] is not None:
            cells[(tg_id, log_date)] = float(row["kcal"])

    dates.sort()
    header = ["Участник", "Норма"] + [d.strftime("%d.%m") for d in dates]

    values = [header]
    for tg_id, name in sorted(people.items(), key=lambda item: item[1]):
        line: list = [name, _num(norms.get(tg_id))]
        for day in dates:
            kcal = cells.get((tg_id, day))
            if kcal is not None:
                line.append(_num(kcal))
            else:
                line.append("—" if (tg_id, day) in tracked else "")
        values.append(line)
    return values


def _write_sheet(book, title: str, values: list[list]) -> None:
    """Переписывает лист целиком, подгоняя размер сетки под данные."""
    rows = max(len(values), 2)
    cols = max(max(len(r) for r in values), 1)

    try:
        worksheet = book.worksheet(title)
    except gspread.WorksheetNotFound:
        worksheet = book.add_worksheet(title=title, rows=rows, cols=cols)

    # Сетку сначала расширяем, потом чистим: update за пределы листа падает,
    # а лишние старые строки иначе остались бы висеть хвостом под данными.
    worksheet.resize(rows=rows, cols=cols)
    worksheet.clear()
    # USER_ENTERED — чтобы даты и числа легли в ячейки датами и числами,
    # а не текстом, иначе в таблице по ним не построить ни график, ни сводную.
    worksheet.update(values=values, range_name="A1", value_input_option="USER_ENTERED")
    worksheet.freeze(rows=1)


def _push(participants: list[list], diary: list[list], matrix: list[list]) -> str:
    client = gspread.service_account(filename=_credentials_file)
    book = client.open_by_key(_sheet_id)
    _write_sheet(book, SHEET_PARTICIPANTS, participants)
    _write_sheet(book, SHEET_DIARY, diary)
    _write_sheet(book, SHEET_MATRIX, matrix)
    return book.url


async def export() -> str | None:
    """Собирает три листа и перезаливает таблицу. Возвращает ссылку на неё."""
    if not enabled():
        log.info("Выгрузка пропущена: не настроена")
        return None

    today = dt.datetime.now(MSK).date()
    participant_rows = await db.export_participants()
    diary_rows = await db.export_diary(today)

    participants = build_participants(participant_rows)
    diary = build_diary(diary_rows, _tolerance)
    matrix = build_matrix(diary_rows)

    url = await asyncio.to_thread(_push, participants, diary, matrix)
    log.info(
        "Выгрузка готова: %d участников, %d строк дневника",
        len(participants) - 1, len(diary) - 1,
    )
    return url
