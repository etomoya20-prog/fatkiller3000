"""Распознавание отчётов о еде через OpenAI: свободный текст, выгрузки из приложений,
скриншоты дневника питания и фотографии самой еды.

Модель здесь только читает цифры с экрана. Всю арифметику и выбор, какой цифре верить,
делает код ниже: на скриншотах приложений крупной цифрой почти всегда показан ОСТАТОК
до нормы, а не съеденное, и доверять модели решать это самой оказалось нельзя.
"""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass, field

from openai import AsyncOpenAI

log = logging.getLogger(__name__)

_client: AsyncOpenAI | None = None
_model = "gpt-4o-mini"

# Калорийность макронутриентов, ккал на грамм.
KCAL_PER_G_PROTEIN = 4
KCAL_PER_G_FAT = 9
KCAL_PER_G_CARB = 4

# Насколько сумма по БЖУ может расходиться с прочитанными калориями, прежде чем
# считать, что с экрана взяли не ту цифру. Ниже 200 ккал расхождение объясняется
# округлениями и клетчаткой, выше — почти всегда ошибка чтения.
MACRO_MISMATCH_RATIO = 0.25
MACRO_MISMATCH_FLOOR = 200.0


def init(api_key: str, model: str) -> None:
    global _client, _model
    _client = AsyncOpenAI(api_key=api_key)
    _model = model


SYSTEM_PROMPT = """Ты — парсер дневника питания в боте для худеющих.

На вход приходит одно из:
- свободный текст пользователя («съел 1800 ккал», «завтрак овсянка 350, обед 600»);
- выгрузка из приложения учёта калорий (FatSecret, MyFitnessPal, YAZIO, Lifesum
  и подобных) в виде текста из CSV/XLSX/PDF;
- скриншот экрана такого приложения;
- фотография самой еды: тарелка, упаковка продукта, меню, чек.

Твоя задача — АККУРАТНО СЧИТАТЬ ЦИФРЫ. Ничего не вычисляй и не складывай сам,
кроме случая, когда пользователь перечислил блюда текстом. Разложи увиденное
по полям, а арифметику сделает вызывающий код.

━━━ ГЛАВНОЕ ПРАВИЛО ДЛЯ СКРИНШОТОВ ━━━

Крупная цифра в кольце или в центре экрана — это, как правило, СКОЛЬКО ОСТАЛОСЬ
до дневной нормы, а НЕ сколько съедено. Рядом с ней стоит подпись «Осталось»,
«Remaining», «Left», «Ещё можно», «до цели», «kcal left».

Поэтому:
- Увидел подпись «осталось» рядом с числом — клади это число в kcal_remaining,
  а НЕ в kcal_consumed.
- Дневную цель («Цель», «Goal», «Норма», «Бюджет») клади в kcal_goal.
- В kcal_consumed клади ТОЛЬКО то, что прямо подписано как съеденное:
  «Съедено», «Потреблено», «Eaten», «Food», «Приёмы пищи».
- Не уверен, что означает число, — оставь kcal_consumed пустым и заполни
  kcal_goal с kcal_remaining. Пустое поле лучше неверного.

Пример: на экране «620» под кольцом, подпись «Осталось», сверху «Цель 2300».
Значит kcal_goal = 2300, kcal_remaining = 620, kcal_consumed = null.

━━━ ФОТОГРАФИЯ ЕДЫ, А НЕ ЭКРАНА ━━━

Если на картинке настоящая еда, а не интерфейс приложения — цифр там нет и
искать их не надо. Оцени калорийность и БЖУ блюда по своим знаниям:

- прикинь состав и порцию по виду (тарелка ≈ 25 см, кружка ≈ 250 мл);
- положи оценку в kcal_consumed и заполни protein_g, fat_g, carb_g;
- understood = true, is_full_day = false — это один приём пищи;
- в comment одной фразой скажи, что это оценка на глаз и что точнее будет,
  если прислать цифры из приложения.

Оценка с погрешностью полезнее отказа: человек ведёт дневник, и пропущенный
день стоит ему больше, чем неточные двести килокалорий. Отказывайся только
если на фото нет еды вообще.

━━━ БЕЛКИ, ЖИРЫ, УГЛЕВОДЫ ━━━

Обычно под калориями показаны съеденные граммы БЖУ — клади их в protein_g,
fat_g, carb_g. Но некоторые приложения показывают там ОСТАТОК по каждому
нутриенту («осталось 40 г белка»). Если это так, всё равно заполни поля,
но поставь macros_are_remaining = true.

━━━ ОСТАЛЬНОЕ ━━━

1. is_full_day различает итог за день и отдельный приём пищи. Это важно:
   итог за день ЗАМЕНЯЕТ записанное, отдельный приём ПРИБАВЛЯЕТСЯ.

   is_full_day = true, если данные охватывают весь день:
   - любой экран или выгрузка приложения, строка «Всего»/«Total»/«Итого»;
   - перечислены все приёмы пищи («завтрак 350, обед 600, ужин 250»);
   - фраза относится ко дню целиком: «сегодня 1800 ккал», «за день вышло 1800»,
     «всего съел 2000». Слова «сегодня», «за день», «всего», «итого» — признак итога.

   is_full_day = false только для одного приёма пищи: «съел бутерброд»,
   «обед 600 ккал», «перекусил яблоком».
2. Свободный текст без готовых цифр («овсянка 200 г, куриная грудка 150 г») —
   оцени калорийность по своим знаниям, положи в kcal_consumed и напиши
   в comment, что это оценка.
3. Данных о еде нет вовсе (вопрос, приветствие, картинка без еды и без цифр) —
   understood = false, а в comment коротко по-русски объясни, чего не хватает
   и что прислать вместо этого.
4. Числа — в килокалориях и граммах, без единиц измерения. Чего не видно — null.
5. comment — одна короткая фраза на русском для пользователя, без markdown.
   Не пересказывай в нём цифры, их подставит бот.

Отвечай строго в заданной JSON-схеме."""

RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "food_report",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "understood": {
                    "type": "boolean",
                    "description": "Удалось ли найти данные о еде",
                },
                "is_full_day": {
                    "type": "boolean",
                    "description": "Данные охватывают весь день, а не один приём пищи",
                },
                "kcal_consumed": {
                    "type": ["number", "null"],
                    "description": "Сколько СЪЕДЕНО — только если прямо подписано как съеденное",
                },
                "kcal_goal": {
                    "type": ["number", "null"],
                    "description": "Дневная цель или норма, если видна",
                },
                "kcal_remaining": {
                    "type": ["number", "null"],
                    "description": "Сколько ОСТАЛОСЬ до нормы, если видно",
                },
                "protein_g": {"type": ["number", "null"]},
                "fat_g": {"type": ["number", "null"]},
                "carb_g": {"type": ["number", "null"]},
                "macros_are_remaining": {
                    "type": "boolean",
                    "description": "БЖУ на экране показывают остаток, а не съеденное",
                },
                "comment": {"type": "string"},
            },
            "required": [
                "understood", "is_full_day",
                "kcal_consumed", "kcal_goal", "kcal_remaining",
                "protein_g", "fat_g", "carb_g",
                "macros_are_remaining", "comment",
            ],
        },
    },
}


@dataclass
class FoodReport:
    """Прочитанное с экрана плюс сведённый итог.

    Поля kcal и note заполняются в __post_init__: модель отдаёт сырые числа,
    а какое из них считать съеденным — решает _resolve().
    """

    understood: bool
    is_full_day: bool
    kcal_consumed: float | None
    kcal_goal: float | None
    kcal_remaining: float | None
    protein_g: float | None
    fat_g: float | None
    carb_g: float | None
    macros_are_remaining: bool
    comment: str

    kcal: float | None = field(init=False, default=None)
    note: str = field(init=False, default="")

    def __post_init__(self) -> None:
        self.kcal, self.note = self._resolve()

    @property
    def usable(self) -> bool:
        return self.understood and self.kcal is not None and self.kcal > 0

    @property
    def macros(self) -> tuple[float | None, float | None, float | None]:
        """Съеденные БЖУ. Если на экране был остаток, съеденное неизвестно."""
        if self.macros_are_remaining:
            return None, None, None
        return self.protein_g, self.fat_g, self.carb_g

    def _macro_kcal(self) -> float | None:
        """Калорийность, посчитанная из съеденных БЖУ — независимая проверка цифры."""
        protein, fat, carb = self.macros
        if protein is None or fat is None or carb is None:
            return None
        total = (
            protein * KCAL_PER_G_PROTEIN
            + fat * KCAL_PER_G_FAT
            + carb * KCAL_PER_G_CARB
        )
        return total if total > 0 else None

    def _resolve(self) -> tuple[float | None, str]:
        macro_kcal = self._macro_kcal()

        # 1. Цель и остаток вместе — это разметка приложения, вычитание однозначно
        #    и надёжнее любой отдельно прочитанной цифры.
        if self.kcal_goal and self.kcal_remaining is not None:
            derived = float(self.kcal_goal) - float(self.kcal_remaining)
            if derived > 0:
                note = (
                    f"Со скриншота: цель {self.kcal_goal:.0f}, "
                    f"осталось {self.kcal_remaining:.0f} — значит съедено {derived:.0f}."
                )
                return self._cross_check(derived, macro_kcal, trust_macros=False, note=note)

        # 2. Явно подписанное съеденное.
        if self.kcal_consumed and self.kcal_consumed > 0:
            return self._cross_check(
                float(self.kcal_consumed), macro_kcal, trust_macros=True, note=""
            )

        # 3. Калорий не видно, но есть БЖУ — считаем по ним.
        if macro_kcal is not None:
            return macro_kcal, "Калории посчитаны по белкам, жирам и углеводам."

        return None, ""

    def _cross_check(
        self, value: float, macro_kcal: float | None, trust_macros: bool, note: str
    ) -> tuple[float, str]:
        """Сверяет цифру с суммой по БЖУ.

        Три независимо прочитанных числа ошибаются согласованно крайне редко,
        поэтому при большом расхождении сумма по БЖУ — более надёжный источник,
        чем одно число, которое легко перепутать с остатком.
        """
        if macro_kcal is None:
            return value, note

        threshold = max(MACRO_MISMATCH_FLOOR, value * MACRO_MISMATCH_RATIO)
        if abs(macro_kcal - value) <= threshold:
            return value, note

        if trust_macros:
            return macro_kcal, (
                f"Судя по БЖУ, съедено около {macro_kcal:.0f} ккал, "
                f"а не {value:.0f} — взял значение по БЖУ."
            ).strip()

        return value, (
            f"{note} ⚠️ По БЖУ выходит {macro_kcal:.0f} ккал — "
            f"если верно это число, пришли данные заново после /reset."
        ).strip()


def _client_or_raise() -> AsyncOpenAI:
    if _client is None:
        raise RuntimeError("LLM не инициализирован — вызовите llm.init()")
    return _client


async def _ask(content: list[dict] | str, source: str) -> FoodReport:
    response = await _client_or_raise().chat.completions.create(
        model=_model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": content},
        ],
        response_format=RESPONSE_FORMAT,
        temperature=0,
    )
    data = json.loads(response.choices[0].message.content)
    report = FoodReport(**data)
    log.info(
        "LLM: съедено=%s цель=%s осталось=%s БЖУ=%s/%s/%s остаток_бжу=%s -> %s ккал",
        data["kcal_consumed"], data["kcal_goal"], data["kcal_remaining"],
        data["protein_g"], data["fat_g"], data["carb_g"],
        data["macros_are_remaining"], report.kcal,
    )
    if not report.usable:
        # Неразобранный отчёт нигде не сохраняется, и без этой строки потом
        # нечего смотреть: непонятно даже, что человек прислал. Пишем WARNING,
        # чтобы такие случаи выцеплялись из логов одним grep.
        log.warning(
            "LLM не разобрал (%s): понял=%s комментарий=%r ← %s",
            source, report.understood, report.comment, _describe(content),
        )
    return report


def _describe(content: list[dict] | str) -> str:
    """Короткое описание входа для лога: текст обрезаем, картинку не пишем вовсе."""
    if isinstance(content, str):
        return repr(content[:600])
    parts = []
    for item in content:
        if item.get("type") == "text":
            parts.append(repr(item["text"][:300]))
        else:
            # В логе от base64 картинки пользы нет, а места он займёт мегабайт.
            parts.append(f"<изображение, {len(item['image_url']['url']) // 1400} КБ>")
    return " + ".join(parts)


async def parse_text(text: str) -> FoodReport:
    return await _ask(f"Сообщение пользователя:\n\n{text}", "текст")


async def parse_document(filename: str, extracted_text: str) -> FoodReport:
    # Огромные выгрузки режем: итог за день всегда в начале или в конце файла.
    if len(extracted_text) > 12_000:
        extracted_text = (
            extracted_text[:6_000]
            + "\n\n[...середина файла пропущена...]\n\n"
            + extracted_text[-6_000:]
        )
    return await _ask(
        f"Выгрузка из приложения учёта калорий, файл «{filename}»:\n\n{extracted_text}",
        f"файл {filename}",
    )


async def parse_image(image_bytes: bytes, mime: str = "image/jpeg") -> FoodReport:
    encoded = base64.b64encode(image_bytes).decode()
    return await _ask([
        {
            "type": "text",
            "text": (
                "Картинка от пользователя. Это либо экран приложения учёта калорий, "
                "либо фотография самой еды — определи, что именно, и действуй по "
                "нужному правилу. Если это экран, внимательно смотри на подписи у "
                "чисел: крупная цифра обычно означает остаток до нормы, а не "
                "съеденное. Если это еда, оцени её калорийность сам."
            ),
        },
        {
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{encoded}"},
        },
    ], "картинка")
