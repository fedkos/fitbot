#!/bin/bash
# Автодеплой: новый коммит -> pull -> validate + тесты -> restart. При сбое откат на прошлый коммит.
set -u
export HOME=/root
cd /opt/fitbot
PY=venv/bin/python
LOG="logger -t fitbot-deploy"

git fetch -q origin main || { $LOG "fetch failed"; exit 0; }
LOCAL=$(git rev-parse HEAD); REMOTE=$(git rev-parse origin/main)
[ "$LOCAL" = "$REMOTE" ] && exit 0

$LOG "deploying $REMOTE"
git reset -q --hard "$REMOTE"
if ! cmp -s requirements.txt .requirements.installed; then
    venv/bin/pip install -q -r requirements.txt && cp requirements.txt .requirements.installed
fi

check() { $PY validate.py >/dev/null && $PY tests/test_logic.py >/dev/null 2>&1; }
rollback() {
    $LOG "FAILED at $REMOTE ($1), rolling back to $LOCAL"
    git reset -q --hard "$LOCAL"
    chown -R fitbot:fitbot /opt/fitbot
    systemctl restart fitbot
    [ -n "${NOTIFY_CHAT:-}" ] && curl -s -o /dev/null "https://api.telegram.org/bot$BOT_TOKEN/sendMessage" \
        -d chat_id="$NOTIFY_CHAT" --data-urlencode text="⚠️ Деплой $REMOTE откатился: $1. Бот работает на старой версии."
    exit 1
}
set -a; . ./.env; set +a
cp -f progress.json "backups/progress.$(date +%Y%m%d%H%M%S).json" 2>/dev/null
ls -t backups/progress.*.json 2>/dev/null | tail -n +30 | xargs -r rm

check || rollback "validate/tests"
chown -R fitbot:fitbot /opt/fitbot
systemctl restart fitbot
sleep 8
systemctl is-active --quiet fitbot || rollback "service down"
$LOG "deployed $REMOTE ok"
[ -n "${NOTIFY_CHAT:-}" ] && curl -s -o /dev/null "https://api.telegram.org/bot$BOT_TOKEN/sendMessage" \
    -d chat_id="$NOTIFY_CHAT" --data-urlencode text="✅ Обновление установлено: $(git log -1 --pretty=%s)"
exit 0
