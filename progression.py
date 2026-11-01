"""Прогрессия нагрузки. Чистые функции, про Telegram и JSON ничего не знают."""
from __future__ import annotations

SHIFT_MIN, SHIFT_MAX = -3, 3


def clamp(x: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, x))


def interval_level(week: int, shift: int, n_levels: int) -> int:
    """Уровень интервалов: неделя программы + смещение от оценок сложности."""
    return clamp(week + shift, 0, n_levels - 1)


def ladder_step(base: int, offset: int, n_steps: int) -> int:
    """Ступень лесенки упражнения: плановая + личное смещение."""
    return clamp(base + offset, 0, n_steps - 1)


def adjust_offset(base: int, offset: int, delta: int, n_steps: int) -> int:
    """Новое смещение после «легче/сложнее». Не выходит за края лесенки."""
    return ladder_step(base, offset + delta, n_steps) - base


def next_shift(shift: int, window: list[int], rpe: int) -> tuple[int, list[int]]:
    """Оценка 5 -> шаг назад сразу. Три подряд оценки <= 2 -> шаг вперёд.
    После изменения окно обнуляется, чтобы одно и то же не срабатывало дважды."""
    window = list(window) + [rpe]
    if rpe >= 5:
        return max(SHIFT_MIN, shift - 1), []
    if len(window) >= 3 and all(r <= 2 for r in window[-3:]):
        return min(SHIFT_MAX, shift + 1), []
    return shift, window[-5:]
