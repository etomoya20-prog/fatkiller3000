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

if [ ! -d "$SRV_DIR/.git" ]; then
  mkdir -p "$SRV_DIR"
  git clone "$REPO_SSH" "$SRV_DIR"
else
  cd "$SRV_DIR"
  git fetch --all --prune
  TARGET_BRANCH="$(git symbolic-ref --short -q refs/remotes/origin/HEAD | sed 's|^origin/||' || echo main)"
  git reset --hard "origin/${TARGET_BRANCH}"
fi

cd "$SRV_DIR"

if [ ! -f .env ]; then
  echo "ОШИБКА: нет $SRV_DIR/.env — скопируйте .env.example и заполните" >&2
  exit 1
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
