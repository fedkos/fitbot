"""Программы тренировок: загрузка из папки, валидация, план на дату."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import progression as G


class ProgramError(ValueError):
    pass


@dataclass(frozen=True)
class DayPlan:
    day: int            # номер дня программы, с 1
    week: int           # индекс недели, с 0
    rest: bool
    test: bool
    variant: str        # утренний комплекс: "A", "B", ...
    pm_variant: str = ""  # вечерний набор


def validate_program(p: dict) -> list[str]:
    errs: list[str] = []
    for key in ("id", "order", "title", "days", "rounds", "intervals", "exercises", "am", "pm"):
        if key not in p:
            errs.append(f"нет поля {key}")
    if errs:
        return errs
    if not isinstance(p["days"], int) or p["days"] < 1:
        errs.append("days должно быть целым > 0")
    if not p["intervals"] or any(
        not isinstance(i.get("work"), int) or not isinstance(i.get("rest"), int) or i["work"] <= 0 or i["rest"] < 0
        for i in p["intervals"]
    ):
        errs.append("intervals: нужны целые work > 0 и rest >= 0")
    for d in p.get("rest_weekdays", []):
        if d not in range(7):
            errs.append(f"rest_weekdays: {d} вне 0..6")
    for d in p.get("test_days", []):
        if not 1 <= d <= p["days"]:
            errs.append(f"test_days: {d} вне 1..{p['days']}")
    ex = p["exercises"]
    for ex_id, e in ex.items():
        steps = e.get("steps") or []
        if not steps or any(not s.get("name") or not s.get("how") for s in steps):
            errs.append(f"{ex_id}: у каждой ступени нужны name и how")
        for s in e.get("schedule", [0]):
            if not isinstance(s, int) or not 0 <= s < max(len(steps), 1):
                errs.append(f"{ex_id}: schedule {s} вне лесенки")
    if not isinstance(p["am"], dict) or not p["am"]:
        errs.append("am: нужен хотя бы один комплекс")
        return errs
    for variant, ids in p["am"].items():
        if not ids:
            errs.append(f"am.{variant}: пусто")
        errs += [f"am.{variant}: неизвестное упражнение {i}" for i in ids if i not in ex]
    pm_sets = p["pm"] if isinstance(p["pm"], dict) else {"1": p["pm"]}
    if not pm_sets or any(not items for items in pm_sets.values()):
        errs.append("pm: пусто")
    for item in (i for items in pm_sets.values() for i in items):
        if item.get("ex") not in ex:
            errs.append(f"pm: неизвестное упражнение {item.get('ex')}")
        if not isinstance(item.get("sec"), int) or item["sec"] <= 0:
            errs.append(f"pm: sec у {item.get('ex')} должно быть целым > 0")
    return errs


def load_programs(folder: Path) -> dict[str, dict]:
    programs: dict[str, dict] = {}
    for path in sorted(Path(folder).glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise ProgramError(f"{path.name}: битый JSON: {e}") from e
        errs = validate_program(data)
        if errs:
            raise ProgramError(f"{path.name}: " + "; ".join(errs))
        if data["id"] in programs:
            raise ProgramError(f"{path.name}: повтор id {data['id']}")
        programs[data["id"]] = data
    if not programs:
        raise ProgramError(f"в {folder} нет ни одной программы")
    return programs


def ordered(programs: dict[str, dict]) -> list[dict]:
    return sorted(programs.values(), key=lambda p: (p["order"], p["id"]))


def first_program(programs: dict[str, dict]) -> dict:
    return ordered(programs)[0]


def next_program(programs: dict[str, dict], pid: str) -> dict | None:
    seq = ordered(programs)
    for i, p in enumerate(seq):
        if p["id"] == pid:
            return seq[i + 1] if i + 1 < len(seq) else None
    return None


def plan_for(program: dict, start: date, d: date) -> DayPlan | None:
    day = (d - start).days + 1
    if day < 1 or day > program["days"]:
        return None
    rest_days = program.get("rest_weekdays", [])
    # A/B чередуются по счёту тренировочных дней, а не календарных
    n_training = sum(
        1 for i in range(day) if (start + timedelta(days=i)).weekday() not in rest_days
    )
    return DayPlan(
        day=day,
        week=(day - 1) // 7,
        rest=d.weekday() in rest_days,
        test=day in program.get("test_days", []),
        variant=_rotate(sorted(program["am"]), n_training),
        pm_variant=_rotate(sorted(program["pm"]), n_training) if isinstance(program["pm"], dict) else "",
    )


def _rotate(keys: list[str], n_training: int) -> str:
    """Комплексы идут по кругу по счёту тренировочных дней."""
    return keys[(max(n_training, 1) - 1) % len(keys)]


def pm_items(program: dict, plan: DayPlan) -> list[dict]:
    pm = program["pm"]
    return list(pm[plan.pm_variant] if isinstance(pm, dict) else pm)


def slots_for(plan: DayPlan | None) -> list[str]:
    if plan is None:
        return []
    if plan.test:  # тест заменяет утро и перекрывает день отдыха
        return ["am"] if plan.rest else ["am", "pm"]
    return [] if plan.rest else ["am", "pm"]


def resolve(program: dict, ex_id: str, week: int, ladder: dict) -> tuple[int, dict, int]:
    """-> (индекс ступени, ступень, плановая ступень этой недели)."""
    ex = program["exercises"][ex_id]
    sched = ex.get("schedule", [0])
    base = sched[min(week, len(sched) - 1)]
    idx = G.ladder_step(base, ladder.get(ex_id, 0), len(ex["steps"]))
    return idx, ex["steps"][idx], base


def interval_for(program: dict, week: int, shift: int) -> dict:
    return program["intervals"][G.interval_level(week, shift, len(program["intervals"]))]


def am_ids(program: dict, plan: DayPlan) -> list[str]:
    return list(program["am"][plan.variant])


IMAGE_EXT = ("jpg", "jpeg", "png", "webp")


def base_step(program: dict, ex_id: str) -> int:
    """Ступень первой недели: её и показывает общая картинка упражнения."""
    return program["exercises"][ex_id].get("schedule", [0])[0]


def find_image(folder: Path, ex_id: str, step: int) -> tuple[Path, bool] | None:
    """Сначала картинка конкретной ступени (<id>_<step>.jpg), потом общая (<id>.jpg).
    -> (путь, точное ли совпадение со ступенью)."""
    for stem, exact in ((f"{ex_id}_{step}", True), (ex_id, False)):
        for ext in IMAGE_EXT:
            path = Path(folder) / f"{stem}.{ext}"
            if path.is_file():
                return path, exact
    return None
