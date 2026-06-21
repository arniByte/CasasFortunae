#!/bin/bash
# Casas Fortunae — SessionStart hook (Claude Code on the web).
# Ставит зависимости игры, чтобы server.py и simulate.py работали сразу.
set -euo pipefail

# Только в облачном окружении Claude Code on the web — на машине пользователя не трогаем.
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "$CLAUDE_PROJECT_DIR"

# Flask + flask-sock + qrcode.
# --ignore-installed blinker: системный Debian-blinker нельзя деинсталлировать
# (RECORD-файл отсутствует), а Flask тянет более новую версию — обходим конфликт.
pip install --quiet --ignore-installed blinker -r requirements.txt >/dev/null 2>&1 \
  || pip install --quiet --ignore-installed blinker flask flask-sock qrcode >/dev/null 2>&1

# Дым-проверка: движок импортируется без ошибок.
python3 -c "import server" && echo "Casas Fortunae: зависимости установлены, движок импортируется."
