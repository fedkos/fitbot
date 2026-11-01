"""Что пора отправить. Чистая функция, отправкой занимается bot.py."""
from __future__ import annotations

import re
from datetime import datetime, time, timedelta

_HHMM = re.compile(r"^(\d{1,2})[:.](\d{2})$")


def parse_hhmm(s: str) -> time | None:
    m = _HHMM.match(s.strip())
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    return time(h, mi) if h < 24 and mi < 60 else None


def due_events(profile: dict, sent_today: list[str], now: datetime, slots: list[str],
               report_day: bool, grace_min: int) -> list[str]:
    """Слот «созрел», если его время уже наступило, но не раньше чем grace_min назад,
    и сегодня он ещё не отправлялся."""
    out = []

    def ripe(hhmm: str) -> bool:
        t = parse_hhmm(hhmm)
        if t is None:
            return False
        sched = now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
        return sched <= now <= sched + timedelta(minutes=grace_min)

    for slot in slots:
        if slot not in sent_today and ripe(profile.get(slot, "")):
            out.append(slot)
    if report_day and "report" not in sent_today and ripe(profile.get("pm", "")):
        out.append("report")
    return out
