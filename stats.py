"""Серия, вес, недельный отчёт, график. Про Telegram ничего не знает."""
from __future__ import annotations

import io
from datetime import date, timedelta
from typing import Callable

OK = {"done", "partial"}


def slot_status(log: dict, d: date, slot: str) -> str | None:
    return log.get(d.isoformat(), {}).get(slot, {}).get("status")


def day_ok(log: dict, d: date) -> bool:
    return any(s.get("status") in OK for s in log.get(d.isoformat(), {}).values())


def streak(log: dict, today: date, start: date, is_rest: Callable[[date], bool]) -> int:
    """Дней подряд с хотя бы одной тренировкой. День отдыха и ещё не закрытое
    «сегодня» серию не рвут и не увеличивают."""
    n, d = 0, today
    while d >= start:
        if day_ok(log, d):
            n += 1
        elif not (is_rest(d) or d == today):
            break
        d -= timedelta(days=1)
    return n


def ma_at(weights: dict, d: date, window: int = 7) -> float | None:
    vals = [
        w for k, w in weights.items()
        if d - timedelta(days=window - 1) <= date.fromisoformat(k) <= d
    ]
    return sum(vals) / len(vals) if vals else None


def weight_series(weights: dict, window: int = 7) -> list[tuple[date, float, float]]:
    return [
        (date.fromisoformat(k), weights[k], ma_at(weights, date.fromisoformat(k), window))
        for k in sorted(weights)
    ]


def weekly(log: dict, today: date, slots_on: Callable[[date], list[str]]) -> dict:
    """Выполнение за 7 дней, заканчивая today."""
    res = {"planned": 0, "done": 0, "partial": 0, "missed": 0,
           "from": today - timedelta(days=6), "to": today}
    for i in range(7):
        d = today - timedelta(days=i)
        for slot in slots_on(d):
            res["planned"] += 1
            st = slot_status(log, d, slot)
            if st == "done":
                res["done"] += 1
            elif st == "partial":
                res["partial"] += 1
            else:
                res["missed"] += 1
    res["pct"] = round(100 * (res["done"] + 0.5 * res["partial"]) / res["planned"]) if res["planned"] else None
    return res


def weight_chart(weights: dict) -> bytes:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    series = weight_series(weights)
    xs = [s[0] for s in series]
    fig, ax = plt.subplots(figsize=(8, 4.5), dpi=130)
    ax.plot(xs, [s[1] for s in series], "o", color="#9aa5b1", markersize=4, label="Daily")
    ax.plot(xs, [s[2] for s in series], "-", color="#1f77b4", linewidth=2.2, label="7-day average")
    ax.set_title("Weight progress")
    ax.set_ylabel("Weight, kg")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))
    ax.grid(alpha=0.3)
    ax.legend()
    fig.autofmt_xdate()
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return buf.getvalue()
