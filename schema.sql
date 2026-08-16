-- Схема БД fatkiller3000. Применяется идемпотентно при каждом старте бота.

CREATE TABLE IF NOT EXISTS users (
    tg_id            BIGINT PRIMARY KEY,
    username         TEXT,
    full_name        TEXT,
    gender           TEXT CHECK (gender IN ('male', 'female')),
    age              INT,
    height_cm        INT,
    weight_kg        NUMERIC(5, 1),
    target_weight_kg NUMERIC(5, 1),
    activity         TEXT,
    kcal_norm        INT,
    protein_g        INT,
    fat_g            INT,
    carb_g           INT,
    onboarded_at     TIMESTAMPTZ,
    is_active        BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Группы, куда бота добавили. Недельная сводка уходит во все активные.
CREATE TABLE IF NOT EXISTS chats (
    chat_id   BIGINT PRIMARY KEY,
    title     TEXT,
    is_active BOOLEAN     NOT NULL DEFAULT TRUE,
    added_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Кто в какой группе состоит: сводка строится только по участникам группы.
CREATE TABLE IF NOT EXISTS group_members (
    chat_id   BIGINT      NOT NULL,
    tg_id     BIGINT      NOT NULL,
    joined_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    left_at   TIMESTAMPTZ,
    PRIMARY KEY (chat_id, tg_id)
);

-- Каждое распознанное сообщение о еде — отдельная запись.
-- Дневной итог считается суммой записей за дату (см. is_full_day в intake.py).
CREATE TABLE IF NOT EXISTS entries (
    id          BIGSERIAL PRIMARY KEY,
    tg_id       BIGINT       NOT NULL REFERENCES users (tg_id) ON DELETE CASCADE,
    log_date    DATE         NOT NULL,
    kcal        NUMERIC(7, 1) NOT NULL DEFAULT 0,
    protein_g   NUMERIC(6, 1),
    fat_g       NUMERIC(6, 1),
    carb_g      NUMERIC(6, 1),
    is_full_day BOOLEAN      NOT NULL DEFAULT FALSE,
    source      TEXT         NOT NULL,
    raw_input   TEXT,
    llm_note    TEXT,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS entries_tg_date_idx ON entries (tg_id, log_date);
CREATE INDEX IF NOT EXISTS entries_date_idx ON entries (log_date);

-- Защита от повторной отправки напоминания в тот же день.
CREATE TABLE IF NOT EXISTS reminders (
    tg_id       BIGINT      NOT NULL,
    remind_date DATE        NOT NULL,
    sent_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tg_id, remind_date)
);
