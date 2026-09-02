"""Конфигурация бота: читается из переменных окружения (в compose — из .env)."""

import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

MSK = ZoneInfo("Europe/Moscow")

# Коэффициенты активности для формулы Миффлина-Сан Жеора.
ACTIVITY_FACTORS = {
    "sedentary": (1.2, "Сидячий образ жизни, спорта нет"),
    "light": (1.375, "Лёгкая активность, 1-3 тренировки в неделю"),
    "moderate": (1.55, "Средняя активность, 3-5 тренировок в неделю"),
    "high": (1.725, "Высокая активность, 6-7 тренировок в неделю"),
    "athlete": (1.9, "Очень высокая: тяжёлая физическая работа или две тренировки в день"),
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
    # Ежедневная выгрузка в Google Sheets. Пустые значения выключают её.
    google_credentials_file: str
    google_sheet_id: str
    export_hour: int
    export_minute: int
    # Насколько можно отклониться от нормы, чтобы день всё ещё считался соблюдённым.
    tolerance: float
    # Белый список чатов для сводки. Пустой — шлём во все группы, где бот состоит.
    group_chat_ids: list[int]


def load_config() -> Config:
    db_password = _required("DB_PASSWORD")
    db_user = os.getenv("DB_USER", "fatkiller")
    db_name = os.getenv("DB_NAME", "fatkiller3000")
    db_host = os.getenv("DB_HOST", "host.docker.internal")
    db_port = os.getenv("DB_PORT", "5432")

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
        summary_day=os.getenv("SUMMARY_DAY", "sun"),
        summary_hour=int(os.getenv("SUMMARY_HOUR", "20")),
        summary_minute=int(os.getenv("SUMMARY_MINUTE", "0")),
        google_credentials_file=os.getenv(
            "GOOGLE_CREDENTIALS_FILE", "/app/secrets/google-service-account.json"
        ),
        google_sheet_id=os.getenv("GOOGLE_SHEET_ID", "").strip(),
        export_hour=int(os.getenv("EXPORT_HOUR", "21")),
        export_minute=int(os.getenv("EXPORT_MINUTE", "0")),
        tolerance=float(os.getenv("TOLERANCE", "0.10")),
        group_chat_ids=group_chat_ids,
    )
