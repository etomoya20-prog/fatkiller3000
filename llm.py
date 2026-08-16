"""Распознавание отчётов о еде через OpenAI: свободный текст, выгрузки из приложений,
скриншоты дневника питания."""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass

from openai import AsyncOpenAI

log = logging.getLogger(__name__)

_client: AsyncOpenAI | None = None
_model = "gpt-4o-mini"


def init(api_key: str, model: str) -> None:
    global _client, _model
    _client = AsyncOpenAI(api_key=api_key)
    _model = model


SYSTEM_PROMPT = """Ты — парсер дневника питания в боте для худеющих.

На вход приходит одно из:
- свободный текст пользователя («съел 1800 ккал», «завтрак овсянка 350, обед 600»);
- выгрузка из приложения учёта калорий (FatSecret, MyFitnessPal, YAZIO, Lifesum и подобных)
  в виде текста из CSV/XLSX/PDF;
- текст, распознанный со скриншота дневника питания.

Твоя задача — извлечь калории и БЖУ.

Правила:
1. Если в данных есть готовый итог за день («Всего», «Total», «Итого») — бери его
   и ставь is_full_day = true.
2. Если пользователь перечислил приёмы пищи за весь день — сложи их, is_full_day = true.
3. Если это один приём пищи («съел бутерброд», «обед 600 ккал») — is_full_day = false.
4. Если калории прямо не указаны, но перечислены блюда с порциями — оцени калорийность
   по своим знаниям о продуктах и напиши в comment, что это оценка.
5. Если данных о еде нет вообще (пользователь спрашивает что-то, здоровается, шлёт мусор) —
   understood = false, а в comment коротко и по-русски объясни, чего не хватает.
6. Числа возвращай в граммах и килокалориях, без единиц измерения.
   Если БЖУ не определяется — null, но kcal старайся определить всегда.
7. comment — одна короткая фраза на русском для пользователя. Без markdown.

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
                    "description": "Это итог за весь день, а не отдельный приём пищи",
                },
                "kcal": {"type": ["number", "null"]},
                "protein_g": {"type": ["number", "null"]},
                "fat_g": {"type": ["number", "null"]},
                "carb_g": {"type": ["number", "null"]},
                "comment": {"type": "string"},
            },
            "required": [
                "understood", "is_full_day", "kcal",
                "protein_g", "fat_g", "carb_g", "comment",
            ],
        },
    },
}


@dataclass
class FoodReport:
    understood: bool
    is_full_day: bool
    kcal: float | None
    protein_g: float | None
    fat_g: float | None
    carb_g: float | None
    comment: str

    @property
    def usable(self) -> bool:
        return self.understood and self.kcal is not None and self.kcal > 0


def _client_or_raise() -> AsyncOpenAI:
    if _client is None:
        raise RuntimeError("LLM не инициализирован — вызовите llm.init()")
    return _client


async def _ask(content: list[dict] | str) -> FoodReport:
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
    return FoodReport(**data)


async def parse_text(text: str) -> FoodReport:
    return await _ask(f"Сообщение пользователя:\n\n{text}")


async def parse_document(filename: str, extracted_text: str) -> FoodReport:
    # Огромные выгрузки режем: итог за день всегда в начале или в конце файла.
    if len(extracted_text) > 12_000:
        extracted_text = (
            extracted_text[:6_000]
            + "\n\n[...середина файла пропущена...]\n\n"
            + extracted_text[-6_000:]
        )
    return await _ask(
        f"Выгрузка из приложения учёта калорий, файл «{filename}»:\n\n{extracted_text}"
    )


async def parse_image(image_bytes: bytes, mime: str = "image/jpeg") -> FoodReport:
    encoded = base64.b64encode(image_bytes).decode()
    return await _ask([
        {
            "type": "text",
            "text": "Скриншот или фото дневника питания. Извлеки калории и БЖУ.",
        },
        {
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{encoded}"},
        },
    ])
