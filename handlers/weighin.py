"""Взвешивание: по субботам бот спрашивает вес, ведёт историю и пересчитывает норму.

Почему через кнопку, а не «просто пришли число». Личка у бота занята приёмом еды:
любой текст без команды уходит в разбор отчёта. Если после субботнего вопроса
молча ждать любое сообщение, первый же «овсянка 350» уедет в весы. Поэтому
намерение подтверждается явно — кнопкой или командой /weight, — и только после
этого следующее сообщение читается как вес.
"""

from __future__ import annotations

import datetime as dt
import logging

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

import calories
import db
from config import MSK
from . import intake

log = logging.getLogger(__name__)

router = Router(name="weighin")
router.message.filter(F.chat.type == ChatType.PRIVATE)

# Те же границы разумного, что и в анкете: всё за ними — опечатка, а не вес.
WEIGHT_LIMITS = (35.0, 400.0)

# Скачок больше этого между взвешиваниями за неделю физически невозможен —
# почти наверняка промахнулись мимо клавиши. Записываем, но переспрашиваем.
SUSPICIOUS_JUMP_KG = 7.0

WEIGH_KB = InlineKeyboardMarkup(inline_keyboard=[[
    InlineKeyboardButton(text="⚖️ Записать вес", callback_data="weigh:start")
]])

PROMPT = (
    "⚖️ <b>Суббота — день взвешивания.</b>\n\n"
    "Встань на весы натощак, до еды и без одежды — так цифра сравнима с прошлой "
    "неделей. Нажми кнопку и пришли вес числом.\n\n"
    "<i>Вес скачет на 1-2 кг из-за воды и соли, поэтому смотрим не на день, "
    "а на направление за месяц.</i>"
)

ASK_NUMBER = (
    "Напиши текущий вес числом в килограммах — например, <code>82.5</code>.\n"
    "Передумал взвешиваться — /cancel."
)


class WeighIn(StatesGroup):
    weight = State()


def today_msk() -> dt.date:
    return dt.datetime.now(MSK).date()


def parse_weight(text: str) -> float | None:
    """Вес из сообщения. Всё, кроме числа с единицами, — не вес."""
    cleaned = (text or "").strip().replace(",", ".").lower()
    for unit in ("кг", "kg", "килограмм", "килограммов", "кило"):
        cleaned = cleaned.replace(unit, "")
    cleaned = cleaned.strip()
    try:
        value = float(cleaned)
    except ValueError:
        return None
    low, high = WEIGHT_LIMITS
    return round(value, 1) if low <= value <= high else None


def _delta(value: float) -> str:
    """Изменение веса со знаком: плюс тоже надо показывать честно."""
    return f"{value:+.1f}".replace("+0.0", "0.0").replace("-0.0", "0.0")


async def record_weight(message: Message, weight_kg: float) -> None:
    """Сохраняет взвешивание, пересчитывает норму и отвечает динамикой."""
    tg_id = message.from_user.id
    profile = await db.get_user(tg_id)
    if not profile or not profile["onboarded_at"]:
        await message.answer("Сначала заполни анкету — /start. Без неё вес не с чем сравнивать.")
        return

    today = today_msk()
    previous = await db.previous_weight(tg_id, today)
    start = float(profile["start_weight_kg"]) if profile["start_weight_kg"] is not None else None
    target = float(profile["target_weight_kg"])
    old_norm = profile["kcal_norm"]

    # Норму пересчитываем от нового веса. Если человек уже на цели или ниже,
    # считать дефицит не от чего: calculate ждёт цель ниже текущего веса.
    goal_reached = weight_kg <= target
    norms = None
    if not goal_reached:
        norms = calories.calculate(
            gender=profile["gender"],
            age=profile["age"],
            height_cm=profile["height_cm"],
            weight_kg=weight_kg,
            target_weight_kg=target,
            activity=profile["activity"],
        )

    await db.save_weigh_in(tg_id, today, weight_kg, norms)
    log.info("Взвешивание %s: %.1f кг", tg_id, weight_kg)

    lines = [f"⚖️ Записал: <b>{weight_kg:.1f} кг</b> на {today.strftime('%d.%m')}."]

    if previous is not None:
        change = weight_kg - previous
        if abs(change) < 0.05:
            lines.append("С прошлого раза вес не изменился.")
        else:
            arrow = "🔻" if change < 0 else "🔺"
            lines.append(f"{arrow} С прошлого взвешивания: <b>{_delta(change)} кг</b>")

    if start is not None and abs(start - weight_kg) >= 0.05:
        total = weight_kg - start
        word = "сброшено" if total < 0 else "набрано"
        lines.append(f"С начала {word}: <b>{abs(total):.1f} кг</b> (старт {start:.1f})")

    if goal_reached:
        lines.append(
            f"\n🎉 Цель {target:.1f} кг достигнута! Норму оставил прежней — "
            f"если пора переходить на удержание или ставить новую цель, жми /again."
        )
    else:
        lines.append(f"До цели осталось: <b>{weight_kg - target:.1f} кг</b>")
        if norms and norms["kcal_norm"] != old_norm:
            lines.append(
                f"\n🔁 Пересчитал норму под новый вес: "
                f"<b>{old_norm} → {norms['kcal_norm']} ккал</b> "
                f"(Б {norms['protein_g']} / Ж {norms['fat_g']} / У {norms['carb_g']} г).\n"
                f"<i>Чем меньше вес, тем меньше тело тратит — норма идёт следом.</i>"
            )

    if previous is not None and abs(weight_kg - previous) > SUSPICIOUS_JUMP_KG:
        lines.append(
            f"\n⚠️ Разница с прошлым разом больше {SUSPICIOUS_JUMP_KG:.0f} кг — "
            f"похоже на опечатку. Если ошибся, пришли /weight ещё раз, я перезапишу."
        )

    await message.answer("\n".join(lines))


@router.callback_query(F.data == "weigh:start")
async def on_weigh_button(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(WeighIn.weight)
    await callback.message.answer(ASK_NUMBER)
    await callback.answer()


@router.message(Command("weight"))
async def cmd_weight(message: Message, command: CommandObject, state: FSMContext) -> None:
    """Взвеситься вручную в любой день: /weight или /weight 82.5."""
    if command.args:
        weight = parse_weight(command.args)
        if weight is None:
            low, high = WEIGHT_LIMITS
            await message.answer(
                f"Вес в килограммах, число от {low:.0f} до {high:.0f}. Например: /weight 82.5"
            )
            return
        await state.clear()
        await record_weight(message, weight)
        return

    await state.set_state(WeighIn.weight)
    await message.answer(ASK_NUMBER)


@router.message(Command("cancel"), WeighIn.weight)
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Хорошо, взвешивание пропускаем. Записать позже: /weight")


@router.message(WeighIn.weight, F.text & ~F.text.startswith("/"))
async def got_weight(message: Message, state: FSMContext, bot: Bot) -> None:
    weight = parse_weight(message.text)
    if weight is None:
        # Человек забыл про весы и прислал еду. Молча отдаём сообщение приёмке
        # отчётов: переспрашивать про вес поверх завтрака — верный способ
        # потерять и завтрак, и взвешивание.
        await state.clear()
        await intake.on_text(message, bot)
        return

    await state.clear()
    await record_weight(message, weight)


@router.message(Command("weights"))
async def cmd_weights(message: Message) -> None:
    """История взвешиваний — чтобы видеть направление, а не одну цифру."""
    rows = await db.weigh_in_history(message.from_user.id)
    if not rows:
        await message.answer(
            "Взвешиваний пока нет. Встань на весы и пришли цифру: /weight"
        )
        return

    lines = ["<b>Твои взвешивания</b>"]
    # Пришли от свежих к старым, а читать историю удобнее сверху вниз.
    ordered = list(reversed(rows))
    for index, row in enumerate(ordered):
        weight = float(row["weight_kg"])
        mark = ""
        if index:
            change = weight - float(ordered[index - 1]["weight_kg"])
            if abs(change) >= 0.05:
                mark = f"  ({_delta(change)})"
        lines.append(f"{row['weigh_date'].strftime('%d.%m')} — {weight:.1f} кг{mark}")

    profile = await db.get_user(message.from_user.id)
    if profile and profile["target_weight_kg"] is not None:
        last = float(ordered[-1]["weight_kg"])
        to_go = last - float(profile["target_weight_kg"])
        lines.append(
            f"\nДо цели: <b>{to_go:.1f} кг</b>" if to_go > 0 else "\n🎉 Цель достигнута!"
        )
    await message.answer("\n".join(lines))
