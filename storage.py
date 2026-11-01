"""progress.json: атомарная запись + дебаунс."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
from pathlib import Path

log = logging.getLogger(__name__)


class Storage:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.data: dict = {"users": {}}
        self._dirty = False

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not isinstance(data.get("users"), dict):
                raise ValueError("unexpected structure")
            self.data = data
        except Exception:
            broken = self.path.with_name(f"{self.path.name}.broken.{int(time.time())}")
            os.replace(self.path, broken)
            log.exception("progress file is broken, moved to %s", broken)
            self.data = {"users": {}}

    def user(self, uid: int | str) -> dict:
        """Профиль читается форвард-совместимо: новые поля через setdefault."""
        u = self.data["users"].setdefault(str(uid), {})
        u.setdefault("profile", {})
        u.setdefault("log", {})        # {"YYYY-MM-DD": {"am": {"status", "rpe"}, "pm": {...}}}
        u.setdefault("weights", {})    # {"YYYY-MM-DD": kg}
        u.setdefault("tests", [])
        u.setdefault("ladder", {})     # {ex_id: смещение ступени относительно плана}
        u.setdefault("shift", 0)       # смещение уровня интервалов от оценок сложности
        u.setdefault("rpe_window", [])
        u.setdefault("sent", {})       # {"YYYY-MM-DD": ["am", "pm", "report"]}
        u.setdefault("quick", {})      # разовые: level, easy, ladder, log
        u["quick"].setdefault("level", 2)
        u["quick"].setdefault("easy", 0)
        u["quick"].setdefault("ladder", {})
        u["quick"].setdefault("log", [])
        return u

    def mark_dirty(self) -> None:
        self._dirty = True

    def flush(self) -> None:
        if not self._dirty:
            return
        self._dirty = False
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=self.path.name, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=1)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except Exception:
            self._dirty = True
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    async def flush_loop(self, interval: float) -> None:
        while True:
            await asyncio.sleep(interval)
            try:
                self.flush()
            except Exception:
                log.exception("flush failed")
