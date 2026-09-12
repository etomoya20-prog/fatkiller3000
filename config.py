"""Конфигурация бота: читается из переменных окружения (в compose — из .env)."""

import datetime as dt
import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

MSK = ZoneInfo("Europe/Moscow")

# Коэффициенты активности для формулы Миффлина-Сан Жеора.
# Подписи не пересекаются по числу тренировок: раньше «лёгкая» была 1-3, а
# «средняя» 3-5, и тренирующийся трижды в неделю видел себя сразу в двух
# кнопках — жал верхнюю и получал норму на 200-300 ккал ниже своей.
ACTIVITY_FACTORS = {
    "sedentary": (1.2, "Сидячий образ жизни, тренировок нет"),
    "light": (1.375, "Лёгкая: 1-2 тренировки в неделю"),
    "moderate": (1.55, "Средняя: 3-5 тренировок в неделю"),
    "high": (1.725, "Высокая: 6-7 тренировок в неделю"),
    "athlete": (1.9, "Очень высокая: тяжёлая работа или две тренировки в день"),
}


def _required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Не задана обязательная переменная окружения {name}")
    return value


@dataclass(frozen=True)
class Config:
    bot_token: str
    openai_api_key: str
    openai_model: str
    db_dsn: str
    # Во сколько по МСК напоминать тем, кто не отчитался за день.
    reminder_hour: int
    reminder_minute: int
    # Когда перечислять в группе тех, кто не заполнил анкету.
    nudge_hour: int
    nudge_minute: int
    # Когда публиковать недельную сводку (day_of_week в терминах APScheduler).
    summary_day: str
    summary_hour: int
    summary_minute: int
    # Когда спрашивать в личке текущий вес.
    weighin_day: str
    weighin_hour: int
    weighin_minute: int
    # Ежедневная выгрузка в Google Sheets. Пустые значения выключают её.
    google_credentials_file: str
    google_sheet_id: str
    # Вечером таблица обновляется каждые export_every_minutes минут, начиная
    # с export_from_hour и до полуночи, плюс финальный прогон в 23:55.
    export_from_hour: int
    export_every_minutes: int
    # Статья про дневник питания: бот прикладывает её к оценкам по фото.
    # Пустая строка убирает ссылку из ответа.
    guide_url: str
    # Насколько можно отклониться от нормы, чтобы день всё ещё считался соблюдённым.
    tolerance: float
    # С какой даты вести историю. Дни до неё не считаются прогулами и не попадают
    # в выгрузку: после обнуления статистики записей за них нет и быть не должно.
    # None — считать с самого начала, как было до первого обнуления.
    history_start: dt.date | None
    # Белый список чатов для сводки. Пустой — шлём во все группы, где бот состоит.
    group_chat_ids: list[int]


def load_config() -> Config:
    db_password = _required("DB_PASSWORD")
    db_user = os.getenv("DB_USER", "fatkiller")
    db_name = os.getenv("DB_NAME", "fatkiller3000")
    db_host = os.getenv("DB_HOST", "host.docker.internal")
    db_port = os.getenv("DB_PORT", "5432")

    # Пустое значение снимает ограничение: это нормальное состояние до первого обнуления.
    raw_history_start = os.getenv("HISTORY_START", "").strip()
    history_start = dt.date.fromisoformat(raw_history_start) if raw_history_start else None

    # Допускаем несколько ID через запятую: бот может работать в нескольких группах.
    raw_chat_ids = os.getenv("GROUP_CHAT_ID", "").replace(" ", "")
    group_chat_ids = [int(x) for x in raw_chat_ids.split(",") if x]

    return Config(
        bot_token=_required("BOT_TOKEN"),
        openai_api_key=_required("OPENAI_API_KEY"),
        openai_model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        db_dsn=f"postgresql://{db_user}:{db_password}@{db_host}:{db_port}/{db_name}",
        reminder_hour=int(os.getenv("REMINDER_HOUR", "21")),
        reminder_minute=int(os.getenv("REMINDER_MINUTE", "0")),
        nudge_hour=int(os.getenv("NUDGE_HOUR", "19")),
        nudge_minute=int(os.getenv("NUDGE_MINUTE", "0")),
        summary_day=os.getenv("SUMMARY_DAY", "mon"),
        summary_hour=int(os.getenv("SUMMARY_HOUR", "10")),
        summary_minute=int(os.getenv("SUMMARY_MINUTE", "0")),
        weighin_day=os.getenv("WEIGHIN_DAY", "sat"),
        weighin_hour=int(os.getenv("WEIGHIN_HOUR", "10")),
        weighin_minute=int(os.getenv("WEIGHIN_MINUTE", "0")),
        google_credentials_file=os.getenv(
            "GOOGLE_CREDENTIALS_FILE", "/app/secrets/google-service-account.json"
        ),
        google_sheet_id=os.getenv("GOOGLE_SHEET_ID", "").strip(),
        export_from_hour=int(os.getenv("EXPORT_FROM_HOUR", "20")),
        export_every_minutes=int(os.getenv("EXPORT_EVERY_MINUTES", "30")),
        guide_url=os.getenv(
            "GUIDE_URL",
            "https://medvisor.ru/articles/dieta-i-zdorovoe-pitanie/dnevnik-pitaniya/",
        ).strip(),
        tolerance=float(os.getenv("TOLERANCE", "0.10")),
        history_start=history_start,
        group_chat_ids=group_chat_ids,
    )
