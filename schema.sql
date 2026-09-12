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

-- Когда владелец бота разрешил работу в группе. Пока NULL, бот в чате молчит,
-- никого не учитывает и никому из этой группы не пишет (см. handlers/approval.py).
-- Группы, где бот работал до появления одобрения, пропускаем сразу — но только
-- в момент добавления колонки: схема накатывается на каждом старте, и иначе
-- перезапуск бота молча одобрял бы всё, что ждёт решения.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_name = 'chats' AND column_name = 'approved_at'
    ) THEN
        ALTER TABLE chats ADD COLUMN approved_at TIMESTAMPTZ;
        UPDATE chats SET approved_at = now() WHERE is_active;
    END IF;
END $$;

-- Кто в какой группе состоит: сводка строится только по участникам группы.
-- Один человек может состоять сразу в нескольких группах — профиль и дневник
-- у него при этом общие, различается только то, в чью сводку он попадает.
CREATE TABLE IF NOT EXISTS group_members (
    chat_id    BIGINT      NOT NULL,
    tg_id      BIGINT      NOT NULL,
    joined_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    left_at    TIMESTAMPTZ,
    -- Когда мы поздоровались с человеком в этом чате. Нужно, чтобы не поприветствовать
    -- дважды: о вступлении Telegram сообщает и апдейтом chat_member, и сервисным
    -- сообщением, и оба приходят, когда бот администратор.
    greeted_at TIMESTAMPTZ,
    PRIMARY KEY (chat_id, tg_id)
);

ALTER TABLE group_members ADD COLUMN IF NOT EXISTS greeted_at TIMESTAMPTZ;

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

-- Полное предупреждение про оценку по фото — один раз в сутки на человека.
-- Дальше в тот же день хватает короткой строчки в самом отчёте: лекцию про
-- трекеры на каждое фото читать никто не станет.
CREATE TABLE IF NOT EXISTS photo_hints (
    tg_id     BIGINT      NOT NULL,
    hint_date DATE        NOT NULL,
    sent_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tg_id, hint_date)
);

-- Защита от повторной отправки напоминания в тот же день.
CREATE TABLE IF NOT EXISTS reminders (
    tg_id       BIGINT      NOT NULL,
    remind_date DATE        NOT NULL,
    sent_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tg_id, remind_date)
);

-- Стартовый вес — тот, с которым человек пришёл. Отдельная колонка нужна потому,
-- что weight_kg теперь меняется на каждом взвешивании и «сколько сброшено с начала»
-- по нему уже не посчитать.
ALTER TABLE users ADD COLUMN IF NOT EXISTS start_weight_kg NUMERIC(5, 1);
UPDATE users SET start_weight_kg = weight_kg
 WHERE start_weight_kg IS NULL AND weight_kg IS NOT NULL;

-- История взвешиваний: по субботам бот спрашивает текущий вес.
-- Одна запись на дату — второе взвешивание в тот же день перезаписывает первое.
CREATE TABLE IF NOT EXISTS weigh_ins (
    tg_id      BIGINT       NOT NULL REFERENCES users (tg_id) ON DELETE CASCADE,
    weigh_date DATE         NOT NULL,
    weight_kg  NUMERIC(5, 1) NOT NULL,
    created_at TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (tg_id, weigh_date)
);

CREATE INDEX IF NOT EXISTS weigh_ins_tg_date_idx ON weigh_ins (tg_id, weigh_date DESC);

-- Защита от повторного вопроса про вес в ту же субботу: планировщик может
-- сработать дважды после перезапуска бота (misfire_grace_time).
CREATE TABLE IF NOT EXISTS weight_prompts (
    tg_id    BIGINT      NOT NULL,
    ask_date DATE        NOT NULL,
    sent_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tg_id, ask_date)
);
