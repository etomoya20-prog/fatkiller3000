#!/usr/bin/env bash
set -euo pipefail

# Деплой fatkiller3000: коммит + пуш локально, затем pull и пересборка на сервере.

SERVER="root@150.251.155.201"
SRV_DIR="/srv/fatkiller3000"
REPO_SSH="${REPO_SSH:-git@github.com:etomoya20-prog/fatkiller3000.git}"

BRANCH="$(git rev-parse --abbrev-ref HEAD)"

echo "==> git add -A"
git add -A

if ! git diff --cached --quiet; then
  MSG="deploy: $(date '+%Y-%m-%d %H:%M:%S')"
  echo "==> git commit -m '$MSG'"
  git commit -m "$MSG"
else
  echo "==> нечего коммитить"
fi

echo "==> git push origin ${BRANCH}"
git push origin "${BRANCH}"

echo "==> подключаюсь к ${SERVER}"
ssh "${SERVER}" REPO_SSH="${REPO_SSH}" SRV_DIR="${SRV_DIR}" bash -se <<'SH'
set -euo pipefail

install -d -m 700 ~/.ssh
touch ~/.ssh/known_hosts
chmod 600 ~/.ssh/known_hosts
if ! ssh-keygen -F github.com >/dev/null; then
  ssh-keyscan -H github.com >> ~/.ssh/known_hosts 2>/dev/null
fi

# Репозиторий поднимаем НА МЕСТЕ, а не через clone в чистый каталог: в $SRV_DIR
# лежит .env с секретами, он не под git, и снести каталог значит потерять его.
# git reset --hard игнорируемые файлы не трогает, поэтому .env переживает деплой.
if [ ! -d "$SRV_DIR/.git" ]; then
  echo "==> первый деплой: инициализирую репозиторий в $SRV_DIR"
  mkdir -p "$SRV_DIR"
  # Файлы могли приехать сюда с чужим uid (например, через rsync с ноутбука) —
  # git на такой каталог ругается «dubious ownership» и отказывается работать.
  chown -R "$(id -u):$(id -g)" "$SRV_DIR"
  cd "$SRV_DIR"
  git init -q
  git remote add origin "$REPO_SSH"
fi

cd "$SRV_DIR"
# Идемпотентно: каталог мог остаться после прерванного деплоя — с .git, но без remote.
git remote set-url origin "$REPO_SSH" 2>/dev/null || git remote add origin "$REPO_SSH"
git fetch --all --prune
git remote set-head origin -a >/dev/null 2>&1 || true

TARGET_BRANCH="$(git symbolic-ref --short -q refs/remotes/origin/HEAD | sed 's|^origin/||' || true)"
[ -n "$TARGET_BRANCH" ] || TARGET_BRANCH=main
echo "==> подтягиваю origin/${TARGET_BRANCH}"
git reset --hard "origin/${TARGET_BRANCH}"

if [ ! -f .env ]; then
  echo "ОШИБКА: нет $SRV_DIR/.env — скопируйте .env.example и заполните" >&2
  exit 1
fi

# Пустой обязательный ключ уронит контейнер в цикл перезапусков уже после сборки —
# дешевле остановиться здесь.
MISSING=""
for KEY in BOT_TOKEN OPENAI_API_KEY DB_PASSWORD; do
  VALUE="$(grep -E "^${KEY}=" .env | head -1 | cut -d= -f2- | tr -d '[:space:]')"
  [ -n "$VALUE" ] || MISSING="$MISSING $KEY"
done
if [ -n "$MISSING" ]; then
  echo "ОШИБКА: в $SRV_DIR/.env не заполнено:$MISSING" >&2
  exit 1
fi

# Каталог с ключом сервисного аккаунта Google под git не ходит, а compose
# монтирует его томом. Создаём заранее, чтобы docker не сделал это за нас от root.
# Владелец — uid 1000: под ним (appuser из Dockerfile) работает контейнер, и от
# root каталог 700 ему просто не открыть. На хосте uid 1000 не занят, так что
# ключ остаётся закрытым для всех, кроме root и самого бота.
mkdir -p secrets
chown -R 1000:1000 secrets
chmod 700 secrets
find secrets -type f -exec chmod 600 {} +

if [ -n "$(grep -E '^GOOGLE_SHEET_ID=' .env | cut -d= -f2- | tr -d '[:space:]')" ] \
   && [ ! -f secrets/google-service-account.json ]; then
  echo "ВНИМАНИЕ: GOOGLE_SHEET_ID задан, но нет secrets/google-service-account.json" >&2
  echo "          выгрузка в таблицу останется выключенной" >&2
fi

echo "==> docker compose build --pull"
docker compose build --pull

echo "==> docker compose up -d"
docker compose up -d

docker compose ps

echo "==> чищу старые образы и кэш сборки"
docker image prune -af --filter "until=24h" || true
docker builder prune -af --filter "until=168h" || true

echo "==> логи:"
docker compose logs --tail=60
SH

echo "==> готово"
