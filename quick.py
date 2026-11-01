"""Разовые тренировки (AMRAP 10 минут): база, подбор, адаптация уровня. Чистая логика."""
from __future__ import annotations

import json
import random
from pathlib import Path

import progression as G

LEVEL_MIN, LEVEL_MAX = 1, 5
GROUPS = {"legs", "push", "core", "cardio", "back", "mobility"}
SLOTS = ("am", "day", "pm")


class QuickError(ValueError):
    pass


def validate(cfg: dict) -> list[str]:
    errs: list[str] = []
    for key in ("duration_sec", "target_rounds", "levels", "templates", "exercises"):
        if key not in cfg:
            errs.append(f"нет поля {key}")
    if errs:
        return errs
    if set(cfg["templates"]) != set(SLOTS):
        errs.append(f"templates: нужны {', '.join(SLOTS)}")
    ids = set()
    for e in cfg["exercises"]:
        eid = e.get("id", "?")
        if eid in ids:
            errs.append(f"{eid}: повтор id")
        ids.add(eid)
        if e.get("group") not in GROUPS:
            errs.append(f"{eid}: группа {e.get('group')} вне {sorted(GROUPS)}")
        if not set(e.get("slots", SLOTS)) <= set(SLOTS):
            errs.append(f"{eid}: slots вне {SLOTS}")
        steps = e.get("steps") or []
        if not steps:
            errs.append(f"{eid}: нет ступеней")
        for s in steps:
            if not s.get("name") or not s.get("how") or not (s.get("reps") or s.get("sec")):
                errs.append(f"{eid}: ступени нужны name, how и reps или sec")
        sched = e.get("schedule", [0] * 5)
        if len(sched) != 5 or any(not isinstance(x, int) or not 0 <= x < max(len(steps), 1) for x in sched):
            errs.append(f"{eid}: schedule должен быть списком из 5 ступеней")
    for slot, groups in cfg["templates"].items():
        for g in groups:
            opts = g if isinstance(g, list) else [g]
            if not set(opts) <= GROUPS:
                errs.append(f"templates.{slot}: неизвестная группа {g}")
            elif not any(e["group"] in opts and slot in e.get("slots", SLOTS) for e in cfg["exercises"]):
                errs.append(f"templates.{slot}: нет упражнений для {g}")
    return errs


def load(path: Path) -> dict:
    try:
        cfg = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise QuickError(f"{path}: файла нет")
    except json.JSONDecodeError as e:
        raise QuickError(f"{path.name}: битый JSON: {e}") from e
    errs = validate(cfg)
    if errs:
        raise QuickError(f"{path.name}: " + "; ".join(errs))
    cfg["by_id"] = {e["id"]: e for e in cfg["exercises"]}
    return cfg


def step_for(ex: dict, level: int, offset: int = 0) -> tuple[int, dict]:
    base = ex.get("schedule", [0] * 5)[G.clamp(level, LEVEL_MIN, LEVEL_MAX) - 1]
    idx = G.ladder_step(base, offset, len(ex["steps"]))
    return idx, ex["steps"][idx]


def dose(step: dict) -> str:
    return f"{step['sec']} с" if step.get("sec") else f"{step['reps']} повт."


def pick(cfg: dict, slot: str, recent: list[str], rng: random.Random, exclude: set[str] = frozenset()) -> list[str]:
    """По одному упражнению на группу шаблона. Недавние и исключённые не берём,
    если есть из чего выбирать."""
    chosen: list[str] = []
    for g in cfg["templates"][slot]:
        opts = g if isinstance(g, list) else [g]
        pool = [e["id"] for e in cfg["exercises"]
                if e["group"] in opts and slot in e.get("slots", SLOTS) and e["id"] not in chosen]
        fresh = [i for i in pool if i not in recent and i not in exclude]
        chosen.append(rng.choice(fresh or [i for i in pool if i not in exclude] or pool))
    return chosen


def next_level(level: int, streak_easy: int, rounds: int, rpe: int, target: int) -> tuple[int, int, str]:
    """-> (новый уровень, счётчик лёгких подряд, пояснение).
    5/5 или сильно меньше цели -> уровень вниз. Дважды подряд легко и цель выполнена -> вверх."""
    if rpe >= 5 or rounds <= target - 2:
        return max(LEVEL_MIN, level - 1), 0, "down" if level > LEVEL_MIN else ""
    if rpe <= 2 and rounds >= target:
        if streak_easy + 1 >= 2 and level < LEVEL_MAX:
            return level + 1, 0, "up"
        return level, streak_easy + 1, ""
    return level, 0, ""


def suggest_level(q: dict) -> int:
    return G.clamp(int(q.get("level", 2)), LEVEL_MIN, LEVEL_MAX)


def recent_ids(q: dict, n: int) -> list[str]:
    out: list[str] = []
    for w in reversed(q.get("log", [])[-n:]):
        out += w.get("exercises", [])
    return out
