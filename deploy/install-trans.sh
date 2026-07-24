#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────
# Transactional Mailer — full-auto installer + hardening
#
# Runs on a fresh Debian 12 / Ubuntu 22.04+ box, as root.
# End state: hardened non-root systemd service reachable via
# Caddy-terminated HTTPS on $DOMAIN, protected by UFW + fail2ban,
# with automatic security updates on. Idempotent — safe to re-run.
#
# Usage (edit the CONFIG block below, or pass via env):
#
#   ssh root@your-new-server
#   apt install -y curl
#   curl -fsSL https://raw.githubusercontent.com/badbodloves/mailer/claude/mass-email-sender-bkzIN/deploy/install-trans.sh \
#       | DOMAIN=trans.example.com bash
#
# Or clone first, edit, then run:
#   git clone https://github.com/badbodloves/mailer /tmp/mailer && \
#     bash /tmp/mailer/deploy/install-trans.sh
# ─────────────────────────────────────────────────────────────

set -euo pipefail

# ─── CONFIG ──────────────────────────────────────────────────
: "${DOMAIN:=trans.example.com}"                                  # → Let's Encrypt cert wird hierfür geholt
: "${REPO_URL:=https://github.com/badbodloves/mailer.git}"        # → public HTTPS clone
: "${BRANCH:=claude/mass-email-sender-bkzIN}"                     # → branch to check out
: "${APP_USER:=mailer}"                                           # → system user the app runs as
: "${APP_DIR:=/home/${APP_USER}/mailer}"                          # → checkout location
: "${APP_PORT:=8001}"                                             # → local uvicorn port
: "${HARDEN_SSH:=auto}"                                           # → auto | yes | no  (disables PW auth if keys exist)
: "${TIMEZONE:=Europe/Berlin}"

# ─── HELPERS ─────────────────────────────────────────────────
log()  { printf '\033[1;36m[%s]\033[0m %s\n' "$(date +%H:%M:%S)" "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[fatal]\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Muss als root laufen."
[ -r /etc/os-release ] || die "/etc/os-release fehlt — unbekanntes OS?"
. /etc/os-release
case "${ID:-}${ID_LIKE:-}" in
    *debian*|*ubuntu*) ;;
    *) die "Nur Debian/Ubuntu unterstützt (gefunden: ${ID:-?}).";;
esac

if [ "$DOMAIN" = "trans.example.com" ]; then
    die "DOMAIN ist noch der Platzhalter — bitte oben eintragen oder als env setzen (DOMAIN=... bash install-trans.sh)."
fi

export DEBIAN_FRONTEND=noninteractive

# ─── 1. System aktualisieren + Zeit setzen ───────────────────
log "1/10  System-Update + Zeitzone $TIMEZONE"
apt-get update -qq
apt-get -yqq -o Dpkg::Options::="--force-confdef" -o Dpkg::Options::="--force-confold" upgrade
timedatectl set-timezone "$TIMEZONE" || true

# ─── 2. Pakete ───────────────────────────────────────────────
log "2/10  Pakete installieren"
apt-get install -yqq \
    git curl ca-certificates gnupg \
    python3 python3-venv python3-pip \
    ufw fail2ban unattended-upgrades apt-listchanges \
    debian-keyring debian-archive-keyring apt-transport-https

# ─── 3. Caddy Repo + Install ─────────────────────────────────
if ! command -v caddy >/dev/null; then
    log "3/10  Caddy installieren"
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
        | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
        > /etc/apt/sources.list.d/caddy-stable.list
    apt-get update -qq
    apt-get install -yqq caddy
else
    log "3/10  Caddy schon installiert — skip"
fi

# ─── 4. App-User + Repo ──────────────────────────────────────
log "4/10  User '$APP_USER' + Repo"
if ! id -u "$APP_USER" >/dev/null 2>&1; then
    useradd --system --create-home --shell /bin/bash "$APP_USER"
fi

# Nur clonen wenn's noch nicht existiert; sonst fetch+reset auf $BRANCH
if [ ! -d "$APP_DIR/.git" ]; then
    sudo -u "$APP_USER" git clone --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
else
    log "     Repo existiert — fetch + reset auf $BRANCH"
    sudo -u "$APP_USER" git -C "$APP_DIR" fetch origin "$BRANCH"
    sudo -u "$APP_USER" git -C "$APP_DIR" checkout "$BRANCH"
    sudo -u "$APP_USER" git -C "$APP_DIR" reset --hard "origin/$BRANCH"
fi

# ─── 5. Python venv + Requirements ───────────────────────────
log "5/10  Python venv + Dependencies"
if [ ! -d "$APP_DIR/venv" ]; then
    sudo -u "$APP_USER" python3 -m venv "$APP_DIR/venv"
fi
sudo -u "$APP_USER" "$APP_DIR/venv/bin/pip" install --upgrade pip -q
# requirements.txt hat die Grund-Deps; die Web-Extras hängen wir dran
sudo -u "$APP_USER" "$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"
sudo -u "$APP_USER" "$APP_DIR/venv/bin/pip" install -q \
    fastapi 'uvicorn[standard]' jinja2 python-multipart boto3 pypdf

# Import-Smoke-Test
if ! sudo -u "$APP_USER" bash -c "cd '$APP_DIR' && '$APP_DIR/venv/bin/python' -c 'from transactional.web.main import app'"; then
    die "Import von transactional.web.main:app fehlgeschlagen — check dependencies."
fi

# ─── 6. Gehärteter systemd Service ──────────────────────────
log "6/10  systemd Unit (hardened, non-root)"
cat > /etc/systemd/system/transmailer.service <<EOF
[Unit]
Description=Transactional Mailer Web UI
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${APP_USER}
Group=${APP_USER}
WorkingDirectory=${APP_DIR}
ExecStart=${APP_DIR}/venv/bin/python -m uvicorn transactional.web.main:app --host 127.0.0.1 --port ${APP_PORT}
Restart=on-failure
RestartSec=5

Environment=PYTHONUNBUFFERED=1
Environment=TZ=${TIMEZONE}

# ── Hardening (systemd-analyze security transmailer sollte ≤ 3.0 sein) ──
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=${APP_DIR}
PrivateTmp=yes
PrivateDevices=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectKernelLogs=yes
ProtectControlGroups=yes
ProtectClock=yes
ProtectHostname=yes
ProtectProc=invisible
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
RestrictNamespaces=yes
RestrictRealtime=yes
RestrictSUIDSGID=yes
LockPersonality=yes
SystemCallArchitectures=native
CapabilityBoundingSet=
AmbientCapabilities=
UMask=0077

[Install]
WantedBy=multi-user.target
EOF

# ─── 7. Caddyfile ───────────────────────────────────────────
log "7/10  Caddyfile für ${DOMAIN}"
cat > /etc/caddy/Caddyfile <<EOF
{
    email admin@${DOMAIN#*.}
}

${DOMAIN} {
    encode gzip zstd

    header {
        Strict-Transport-Security "max-age=31536000; includeSubDomains"
        X-Content-Type-Options    "nosniff"
        X-Frame-Options           "SAMEORIGIN"
        Referrer-Policy           "strict-origin-when-cross-origin"
        Permissions-Policy        "geolocation=(), microphone=(), camera=()"
        -Server
    }

    reverse_proxy 127.0.0.1:${APP_PORT} {
        header_up X-Real-IP        {remote_host}
        header_up X-Forwarded-For  {remote_host}
        header_up X-Forwarded-Host {host}
    }

    log {
        output file /var/log/caddy/access.log {
            roll_size 10MiB
            roll_keep 5
        }
        format json
    }
}
EOF

# ─── 8. UFW Firewall ─────────────────────────────────────────
log "8/10  UFW Firewall (nur 22 / 80 / 443)"
ufw --force reset >/dev/null
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw allow 80/tcp   comment 'HTTP (Caddy)'
ufw allow 443/tcp  comment 'HTTPS (Caddy)'
yes | ufw enable >/dev/null

# ─── 9. fail2ban + unattended-upgrades ──────────────────────
log "9/10  fail2ban + unattended-upgrades"
cat > /etc/fail2ban/jail.d/sshd.conf <<'EOF'
[sshd]
enabled  = true
port     = ssh
backend  = systemd
maxretry = 5
findtime = 10m
bantime  = 1h
EOF
systemctl enable --now fail2ban
systemctl restart fail2ban

cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade   "1";
APT::Periodic::AutocleanInterval    "7";
EOF
cat > /etc/apt/apt.conf.d/50unattended-upgrades <<'EOF'
Unattended-Upgrade::Origins-Pattern {
    "origin=Debian,codename=${distro_codename},label=Debian-Security";
    "origin=Ubuntu,codename=${distro_codename}-security";
};
Unattended-Upgrade::Automatic-Reboot "false";
Unattended-Upgrade::Remove-Unused-Dependencies "true";
EOF
systemctl enable --now unattended-upgrades

# ─── 10. SSH-Hardening (nur wenn ein Key hinterlegt ist) ─────
log "10/10 SSH-Hardening ($HARDEN_SSH)"
should_harden=0
case "$HARDEN_SSH" in
    yes) should_harden=1 ;;
    no)  should_harden=0 ;;
    auto)
        if [ -s /root/.ssh/authorized_keys ] \
           || ([ -d "/home/$APP_USER/.ssh" ] && [ -s "/home/$APP_USER/.ssh/authorized_keys" ]); then
            should_harden=1
        else
            warn "Kein SSH-Key gefunden — SSH-Hardening übersprungen, sonst sperrst du dich aus."
            warn "Wenn du einen Key hast: install-trans.sh nochmal mit HARDEN_SSH=yes laufen lassen."
        fi
        ;;
esac
if [ "$should_harden" = 1 ]; then
    cat > /etc/ssh/sshd_config.d/99-hardening.conf <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
ChallengeResponseAuthentication no
PermitRootLogin prohibit-password
X11Forwarding no
MaxAuthTries 3
LoginGraceTime 30
ClientAliveInterval 300
ClientAliveCountMax 2
Protocol 2
EOF
    if sshd -t 2>/dev/null; then
        systemctl reload ssh || systemctl reload sshd
        log "     SSH: Passwort-Auth aus, nur noch Key-Login."
    else
        warn "sshd config-Test fehlgeschlagen — Hardening-Datei entfernt."
        rm -f /etc/ssh/sshd_config.d/99-hardening.conf
    fi
fi

# ─── sudoers: NOPASSWD für den Restart, damit update-trans.sh ohne PW läuft ─
cat > /etc/sudoers.d/transmailer <<EOF
${APP_USER} ALL=(root) NOPASSWD: /bin/systemctl restart transmailer, /bin/systemctl status transmailer, /bin/systemctl reload transmailer
EOF
chmod 0440 /etc/sudoers.d/transmailer

# ─── Services scharfschalten ─────────────────────────────────
log "Services enablen + starten"
mkdir -p /var/log/caddy && chown caddy:caddy /var/log/caddy
systemctl daemon-reload
systemctl enable --now transmailer
systemctl reload caddy 2>/dev/null || systemctl restart caddy

# ─── Healthcheck ─────────────────────────────────────────────
sleep 3
HTTP_LOCAL=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:${APP_PORT}/" || echo "000")
[ "$HTTP_LOCAL" = "303" ] || [ "$HTTP_LOCAL" = "200" ] || warn "App antwortet nicht sauber (HTTP $HTTP_LOCAL) — check: journalctl -u transmailer -n 40"

cat <<EOF

┌──────────────────────────────────────────────────────────────
│  ✅  Deploy fertig.
│
│  Panel :  https://${DOMAIN}
│  Local :  http://127.0.0.1:${APP_PORT}  (HTTP $HTTP_LOCAL)
│  App   :  systemctl status transmailer
│  Logs  :  journalctl -u transmailer -f
│  Caddy :  journalctl -u caddy -f
│
│  DNS-Check: für automatisches Zertifikat muss ${DOMAIN}
│  bereits auf diesen Server zeigen (A-/AAAA-Record).
│  Beim ersten Aufruf holt Caddy Let's Encrypt automatisch.
│
│  Updates ab jetzt:
│      sudo -u ${APP_USER} bash ${APP_DIR}/deploy/update-trans.sh
│
│  security audit:
│      systemd-analyze security transmailer
└──────────────────────────────────────────────────────────────
EOF
