"""Все настройки и пути. .env читается вручную, без зависимостей."""
import os
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = Path(__file__).resolve().parent


def _load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()  # strip() снимает и \r от Windows
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_env(BASE / ".env")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
# Пусто = бот отвечает всем. Иначе список tg id через запятую.
ALLOWED_IDS = {int(x) for x in os.environ.get("ALLOWED_IDS", "").replace(" ", "").split(",") if x}

TZ = ZoneInfo(os.environ.get("TZ_NAME", "Asia/Nicosia"))
DATA_FILE = Path(os.environ.get("DATA_FILE", BASE / "progress.json"))
PROGRAM_DIR = Path(os.environ.get("PROGRAM_DIR", BASE / "program"))
IMAGE_DIR = Path(os.environ.get("IMAGE_DIR", BASE / "images"))
QUICK_FILE = Path(os.environ.get("QUICK_FILE", BASE / "quick" / "exercises.json"))

FLUSH_INTERVAL = 3        # сек, сброс progress.json по флагу dirty
REMINDER_TICK = 30        # сек, период проверки напоминаний
REMINDER_GRACE_MIN = 180  # если бот лежал дольше, напоминание не досылается
BACKFILL_DAYS = 1         # на сколько дней назад можно отметить тренировку
DEFAULT_AM = "07:30"
DEFAULT_PM = "21:00"
REPORT_WEEKDAY = 6        # воскресенье
