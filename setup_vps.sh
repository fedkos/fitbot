#!/bin/bash
# Одноразовая настройка автодеплоя на VPS. Запускать от root: bash setup_vps.sh
set -e
apt-get install -y -q git >/dev/null
cd /opt/fitbot
[ -f /root/.ssh/fitbot_deploy ] || ssh-keygen -t ed25519 -N "" -f /root/.ssh/fitbot_deploy -C fitbot-deploy -q
cat > /root/.ssh/config.d_fitbot <<'C'
Host github-fitbot
    HostName github.com
    User git
    IdentityFile /root/.ssh/fitbot_deploy
    IdentitiesOnly yes
C
grep -q github-fitbot /root/.ssh/config 2>/dev/null || cat /root/.ssh/config.d_fitbot >> /root/.ssh/config
ssh-keyscan -t ed25519 github.com >> /root/.ssh/known_hosts 2>/dev/null
mkdir -p backups
if [ ! -d .git ]; then
    git init -q && git remote add origin git@github-fitbot:fedkos/fitbot.git
fi
git config user.email deploy@fitbot && git config user.name fitbot-deploy
cat > /etc/systemd/system/fitbot-deploy.service <<'S'
[Unit]
Description=fitbot auto-deploy
[Service]
Type=oneshot
ExecStart=/opt/fitbot/deploy.sh
S
cat > /etc/systemd/system/fitbot-deploy.timer <<'T'
[Unit]
Description=fitbot auto-deploy every minute
[Timer]
OnBootSec=1min
OnUnitActiveSec=1min
[Install]
WantedBy=timers.target
T
systemctl daemon-reload
echo
echo "=== Добавь этот ключ в GitHub: репозиторий -> Settings -> Deploy keys (без права записи) ==="
cat /root/.ssh/fitbot_deploy.pub
echo
echo "После этого выполни: cd /opt/fitbot && git fetch origin main && git reset --hard origin/main && systemctl enable --now fitbot-deploy.timer"
