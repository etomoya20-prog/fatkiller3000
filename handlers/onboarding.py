"""Анкета в личке: шесть вопросов, на выходе — дневная норма калорий и БЖУ."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandObject, CommandStart
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
from config import ACTIVITY_FACTORS

log = logging.getLogger(__name__)

router = Router(name="onboarding")
router.message.filter(F.chat.type == ChatType.PRIVATE)

# Границы разумного. Всё, что за ними, — почти наверняка опечатка.
LIMITS = {
    "age": (14, 100),
    "height_cm": (120, 250),
    "weight_kg": (35, 400),
    "target_weight_kg": (35, 400),
}


class Survey(StatesGroup):
    gender = State()
    age = State()
    height = State()
    weight = State()
    target = State()
    activity = State()


GENDER_KB = InlineKeyboardMarkup(inline_keyboard=[[
    InlineKeyboardButton(text="Мужской", callback_data="gender:male"),
    InlineKeyboardButton(text="Женский", callback_data="gender:female"),
]])

ACTIVITY_KB = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text=description, callback_data=f"activity:{key}")]
    for key, (_, description) in ACTIVITY_FACTORS.items()
])


def _parse_number(text: str) -> float | None:
    cleaned = text.strip().replace(",", ".")
    # Отрезаем единицы измерения: «75 кг», «175 см».
    cleaned = "".join(ch for ch in cleaned if ch.isdigit() or ch == ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def format_profile(user: dict) -> str:
    gender_ru = "мужской" if user["gender"] == "male" else "женский"
    activity_ru = ACTIVITY_FACTORS[user["activity"]][1]
    return (
        f"<b>Твой профиль</b>\n"
        f"Пол: {gender_ru}, возраст: {user['age']}\n"
        f"Рост: {user['height_cm']} см, вес: {user['weight_kg']} кг\n"
        f"Цель: {user['target_weight_kg']} кг\n"
        f"Активность: {activity_ru}\n\n"
        f"<b>Дневная норма</b>\n"
        f"🔥 Калории: <b>{user['kcal_norm']} ккал</b>\n"
        f"🥩 Белки: {user['protein_g']} г\n"
        f"🥑 Жиры: {user['fat_g']} г\n"
        f"🍞 Углеводы: {user['carb_g']} г"
    )


@router.message(CommandStart(deep_link=True))
async def start_with_payload(message: Message, command: CommandObject, state: FSMContext) -> None:
    """Переход по кнопке из группы: запоминаем, из какого чата пришёл человек."""
    payload = command.args or ""
    if payload.startswith("group"):
        try:
            chat_id = int(payload.removeprefix("group"))
        except ValueError:
            log.warning("Не разобрал chat_id из диплинка: %r", payload)
        else:
            await db.upsert_chat(chat_id, None)
            await db.add_group_member(chat_id, message.from_user.id)
    await start(message, state)


@router.message(CommandStart())
async def start(message: Message, state: FSMContext) -> None:
    user = message.from_user
    await db.upsert_user(user.id, user.username, user.full_name)

    profile = await db.get_user(user.id)
    if profile and profile["onboarded_at"]:
        await state.clear()
        await message.answer(
            format_profile(dict(profile))
            + "\n\nАнкета уже заполнена. Присылай, что ел за день — я посчитаю.\n"
              "Пересчитать норму: /again"
        )
        return

    await state.set_state(Survey.gender)
    await message.answer(
        "Привет! Я помогу держать калории под контролем.\n\n"
        "Задам шесть вопросов и рассчитаю твою дневную норму по формуле "
        "Миффлина-Сан Жеора — это то, чем пользуются диетологи.\n\n"
        "<b>1/6. Какой у тебя пол?</b>",
        reply_markup=GENDER_KB,
    )


@router.message(Command("again"))
async def restart_survey(message: Message, state: FSMContext) -> None:
    await state.set_state(Survey.gender)
    await message.answer(
        "Заполняем анкету заново.\n\n<b>1/6. Какой у тебя пол?</b>",
        reply_markup=GENDER_KB,
    )


@router.callback_query(Survey.gender, F.data.startswith("gender:"))
async def got_gender(callback: CallbackQuery, state: FSMContext) -> None:
    gender = callback.data.split(":", 1)[1]
    await state.update_data(gender=gender)
    await state.set_state(Survey.age)
    await callback.message.edit_text(
        f"Пол: {'мужской' if gender == 'male' else 'женский'} ✓\n\n"
        f"<b>2/6. Сколько тебе лет?</b>\nНапиши числом, например: 34"
    )
    await callback.answer()


@router.message(Survey.age)
async def got_age(message: Message, state: FSMContext) -> None:
    value = _parse_number(message.text or "")
    low, high = LIMITS["age"]
    if value is None or not low <= value <= high:
        await message.answer(f"Нужно число от {low} до {high}. Например: 34")
        return

    await state.update_data(age=int(value))
    await state.set_state(Survey.height)
    await message.answer("<b>3/6. Какой у тебя рост в сантиметрах?</b>\nНапример: 175")


@router.message(Survey.height)
async def got_height(message: Message, state: FSMContext) -> None:
    value = _parse_number(message.text or "")
    low, high = LIMITS["height_cm"]
    if value is None or not low <= value <= high:
        await message.answer(f"Рост в сантиметрах, число от {low} до {high}. Например: 175")
        return

    await state.update_data(height_cm=int(value))
    await state.set_state(Survey.weight)
    await message.answer("<b>4/6. Сколько ты весишь сейчас, в килограммах?</b>\nНапример: 82.5")


@router.message(Survey.weight)
async def got_weight(message: Message, state: FSMContext) -> None:
    value = _parse_number(message.text or "")
    low, high = LIMITS["weight_kg"]
    if value is None or not low <= value <= high:
        await message.answer(f"Вес в килограммах, число от {low} до {high}. Например: 82.5")
        return

    await state.update_data(weight_kg=round(value, 1))
    await state.set_state(Survey.target)
    await message.answer(
        "<b>5/6. До какого веса хочешь дойти?</b>\n"
        "Напиши целевой вес в килограммах, например: 75"
    )


@router.message(Survey.target)
async def got_target(message: Message, state: FSMContext) -> None:
    value = _parse_number(message.text or "")
    low, high = LIMITS["target_weight_kg"]
    if value is None or not low <= value <= high:
        await message.answer(f"Целевой вес в килограммах, число от {low} до {high}. Например: 75")
        return

    data = await state.get_data()
    current = data["weight_kg"]
    if value >= current:
        await message.answer(
            f"Целевой вес должен быть меньше текущего ({current} кг). "
            f"Сколько хочешь весить?"
        )
        return

    # Проверяем, не ведёт ли цель в недовес: ИМТ ниже 18.5 — это уже дефицит массы.
    height_m = data["height_cm"] / 100
    target_bmi = value / (height_m ** 2)
    warning = ""
    if target_bmi < 18.5:
        warning = (
            f"\n\n⚠️ При росте {data['height_cm']} см вес {value} кг — это ИМТ "
            f"{target_bmi:.1f}, ниже нормы. Норму рассчитаю, но такую цель стоит "
            f"обсудить с врачом."
        )

    await state.update_data(target_weight_kg=round(value, 1))
    await state.set_state(Survey.activity)
    await message.answer(
        f"<b>6/6. Насколько ты активен?</b>\n"
        f"Выбери вариант, который ближе всего к твоей обычной неделе.{warning}",
        reply_markup=ACTIVITY_KB,
    )


@router.callback_query(Survey.activity, F.data.startswith("activity:"))
async def got_activity(callback: CallbackQuery, state: FSMContext) -> None:
    activity = callback.data.split(":", 1)[1]
    data = await state.get_data()
    data["activity"] = activity

    result = calories.calculate(
        gender=data["gender"],
        age=data["age"],
        height_cm=data["height_cm"],
        weight_kg=data["weight_kg"],
        target_weight_kg=data["target_weight_kg"],
        activity=activity,
    )
    profile = {**data, **result}
    await db.save_profile(callback.from_user.id, profile)
    await state.clear()

    weeks = calories.weeks_to_goal(
        data["weight_kg"], data["target_weight_kg"],
        result["kcal_norm"], result["maintenance_kcal"],
    )
    forecast = ""
    if weeks:
        forecast = (
            f"\n\nПри таком дефиците до цели примерно <b>{weeks} нед.</b> — "
            f"это оценка, реальный темп зависит от тела и дисциплины."
        )

    await callback.message.edit_text(
        f"Готово, вот что получилось.\n\n"
        f"{format_profile(profile)}\n\n"
        f"Без дефицита ты бы тратил около {result['maintenance_kcal']} ккал в день — "
        f"норма ниже на 20%, это безопасный темп снижения веса.{forecast}\n\n"
        f"━━━━━━━━━━━━━━\n"
        f"<b>Что дальше.</b> Каждый день присылай мне, что съел: текстом, "
        f"файлом-выгрузкой из приложения или скриншотом дневника. Разберусь в любом формате.\n"
        f"Если до 21:00 по Москве от тебя ничего не будет — напомню.\n\n"
        f"Команды: /today — итог дня, /profile — норма, /reset — стереть сегодняшний день."
        + await _no_group_warning(callback.from_user.id)
    )
    await callback.answer("Норма рассчитана")


async def _no_group_warning(tg_id: int) -> str:
    """Человек, не привязанный ни к одной группе, выпадает и из сводки, и из
    напоминаний — молчать об этом нельзя, он решит, что бот сломался."""
    if await db.is_in_any_group(tg_id):
        return ""
    return (
        "\n\n⚠️ Я пока не вижу тебя ни в одной группе. Значит, ты не попадёшь "
        "в общую сводку и не будешь получать напоминания. Напиши в групповом чате "
        "<code>/join</code> или просто что-нибудь — и я тебя запишу."
    )


@router.message(Command("profile"))
async def cmd_profile(message: Message) -> None:
    profile = await db.get_user(message.from_user.id)
    if not profile or not profile["onboarded_at"]:
        await message.answer("Анкета ещё не заполнена. Начнём? Жми /start")
        return
    await message.answer(
        format_profile(dict(profile))
        + "\n\nПересчитать: /again"
        + await _no_group_warning(message.from_user.id)
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        "<b>Как со мной работать</b>\n\n"
        "Просто пиши, что съел — обычным текстом:\n"
        "• «завтрак овсянка 350, обед суп и котлета 600, ужин творог 250»\n"
        "• «сегодня 1800 ккал, белка 120»\n\n"
        "Или пришли файл-выгрузку из приложения учёта калорий "
        "(CSV, XLSX, PDF, TXT) либо скриншот дневника — распознаю сам.\n\n"
        "<b>Команды</b>\n"
        "/today — что уже засчитано за сегодня\n"
        "/profile — твоя норма калорий и БЖУ\n"
        "/again — заполнить анкету заново\n"
        "/reset — стереть записи за сегодня и начать день заново"
    )
