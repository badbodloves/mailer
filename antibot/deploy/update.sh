#!/usr/bin/env bash
# Regulärer Deploy: git pull + venv sync + restart.
# Als antibot-User laufen lassen — sudoers erlaubt den restart ohne PW.
#
#   bash ~/antibot/deploy/update.sh

set -euo pipefail

: "${APP_DIR:=$HOME/antibot}"
: "${BRANCH:=main}"

cd "$APP_DIR"

echo "→ git pull ($BRANCH)"
git fetch origin "$BRANCH"
git checkout "$BRANCH"
git reset --hard "origin/$BRANCH"

if [ -x venv/bin/pip ]; then
    echo "→ pip install -r requirements.txt (falls neu)"
    venv/bin/pip install -q --upgrade pip
    venv/bin/pip install -q -r requirements.txt
fi

echo "→ systemctl restart antibot"
sudo -n systemctl restart antibot

sleep 2
sudo -n systemctl status antibot --no-pager | head -8
echo "✅ Deploy fertig."
