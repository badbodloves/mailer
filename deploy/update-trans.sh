#!/usr/bin/env bash
# Regulärer Deploy: git pull + venv sync + Service-Restart.
# Als App-User laufen lassen — sudoers erlaubt den restart ohne PW.
#
#   sudo -u mailer bash /home/mailer/mailer/deploy/update-trans.sh
#
# Oder direkt aus einer mailer-SSH-Session:
#   bash ~/mailer/deploy/update-trans.sh

set -euo pipefail

: "${APP_DIR:=$HOME/mailer}"
: "${BRANCH:=claude/mass-email-sender-bkzIN}"

cd "$APP_DIR"

echo "→ git pull ($BRANCH)"
git fetch origin "$BRANCH"
git checkout "$BRANCH"
git reset --hard "origin/$BRANCH"

# requirements.txt oder deps hinzugekommen? venv sync
if [ -x venv/bin/pip ]; then
    echo "→ pip install -r requirements.txt (falls neu)"
    venv/bin/pip install -q --upgrade pip
    venv/bin/pip install -q -r requirements.txt
fi

echo "→ systemctl restart transmailer"
sudo -n systemctl restart transmailer

sleep 2
sudo -n systemctl status transmailer --no-pager | head -12

echo "✅ Deploy fertig."
