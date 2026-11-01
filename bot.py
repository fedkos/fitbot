"""Слой представления: хендлеры, рендер, клавиатуры, запуск."""
from __future__ import annotations

import asyncio
import html
import logging
import random
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.filters.callback_data import CallbackData
from aiogram.types import (BotCommand, BufferedInputFile, CallbackQuery, FSInputFile,
                           InlineKeyboardButton, InlineKeyboardMarkup, Message)

import config
import program as P
import progression as G
import quick as Q
import reminders as R
import stats as S
from storage import Storage

log = logging.getLogger("fitbot")
router = Router()
storage = Storage(config.DATA_FILE)
programs: dict[str, dict] = {}
quick_cfg: dict = {}

SLOT_NAME = {"am": "☀️ Утро", "pm": "🌙 Вечер"}
STATUS_NAME = {"done": "✅ сделано", "partial": "◐ частично", "skip": "✖ пропуск"}
TESTS = [
    ("pushups", "Отжимания", "Максимум отжиманий за один подход, тем вариантом, который сейчас в программе. Введи число:", ""),
    ("squats60", "Приседания за 60 с", "Приседания за 60 секунд. Введи число:", ""),
    ("plank", "Планка", "Планка на предплечьях до отказа. Введи секунды:", " с"),
]


# ---------- колбэки: непересекающиеся префиксы, в данных только дата/слот/индекс ----------

class WCb(CallbackData, prefix="w"):   # карточка тренировки
    d: str
    slot: str
    act: str    # show / start / done / partial / skip / test


class SCb(CallbackData, prefix="s"):   # шаг внутри тренировки
    d: str
    slot: str
    idx: int
    act: str    # next / easier / harder / img


class RCb(CallbackData, prefix="r"):   # оценка сложности
    d: str
    slot: str
    val: int


class MCb(CallbackData, prefix="m"):   # меню и онбординг
    act: str


class QCb(CallbackData, prefix="q"):   # разовая тренировка
    sid: int    # номер сессии, старые кнопки игнорируются
    act: str    # slot / lvl / reroll / start / finish / rounds / rpe / easier / harder / cancel
    val: int = 0


@dataclass
class QuickSession:
    sid: int
    slot: str = ""
    level: int = 2
    items: list[str] = field(default_factory=list)
    stage: str = "slot"     # slot -> preview -> running -> rounds -> rpe
    rounds: int = 0
    started: str = ""


quick_sessions: dict[int, QuickSession] = {}
quick_counter = 0


@dataclass
class Session:
    d: str
    slot: str
    idx: int
    cards: list[tuple[str, int]]   # (ex_id, номер круга или секунды)
    photos: list[int] = field(default_factory=list)   # id сообщений с картинками, убираем при переходе
    photo_for: tuple[int, int] | None = None          # (idx, ступень), для которых картинка уже показана


sessions: dict[int, Session] = {}
file_ids: dict[str, str] = {}      # кэш Telegram file_id, чтобы не заливать картинку повторно
awaiting: dict[int, dict] = {}     # ожидаемый свободный ввод


# ---------- помощники ----------

def now_local() -> datetime:
    return datetime.now(config.TZ)


def today() -> date:
    return now_local().date()


def esc(s: str) -> str:
    return html.escape(str(s))


def btn(text: str, data: CallbackData) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data.pack())


def kb(*rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows if r])


def menu_kb() -> InlineKeyboardMarkup:
    return kb(
        [btn("🏋️ Сегодня", MCb(act="today")), btn("⚖️ Вес", MCb(act="weight"))],
        [btn("📊 Прогресс", MCb(act="stats")), btn("🧪 Тест", MCb(act="test"))],
        [btn("↩️ Вчера", MCb(act="yesterday")), btn("⚙️ Настройки", MCb(act="settings"))],
        [btn("⚡ Разовая тренировка", MCb(act="quick"))],
    )


async def safe_edit(msg: Message, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    try:
        await msg.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as e:
        if "not modified" not in str(e):
            await msg.answer(text, reply_markup=markup)


def ready(user: dict) -> bool:
    p = user["profile"]
    return bool(p.get("am") and p.get("pm") and user.get("start_date")
                and user.get("program_id") in programs)


def rollover(user: dict, d: date) -> None:
    """Программа закончилась и в папке есть следующая: переключаемся на неё."""
    while ready(user):
        prog = programs[user["program_id"]]
        end = date.fromisoformat(user["start_date"]) + timedelta(days=prog["days"] - 1)
        nxt = P.next_program(programs, prog["id"])
        if d <= end or nxt is None:
            return
        user["program_id"] = nxt["id"]
        user["start_date"] = (end + timedelta(days=1)).isoformat()
        storage.mark_dirty()


def ctx(user: dict, d: date) -> tuple[dict, P.DayPlan | None]:
    prog = programs[user["program_id"]]
    return prog, P.plan_for(prog, date.fromisoformat(user["start_date"]), d)


def joined(user: dict) -> date:
    return date.fromisoformat(user.get("joined") or user["start_date"])


def slots_on(user: dict, d: date) -> list[str]:
    prog, plan = ctx(user, d)
    if plan:
        return P.slots_for(plan)
    # дни прошлой программы после переключения
    if joined(user) <= d < date.fromisoformat(user["start_date"]):
        return [] if d.weekday() in prog.get("rest_weekdays", []) else ["am", "pm"]
    return []


def streak(user: dict) -> int:
    return S.streak(user["log"], today(), joined(user), lambda d: not slots_on(user, d))


def editable(d: date) -> bool:
    return today() - timedelta(days=config.BACKFILL_DAYS) <= d <= today()


def apply_status(user: dict, d: date, slot: str, status: str) -> bool:
    entry = user["log"].setdefault(d.isoformat(), {}).setdefault(slot, {})
    if entry.get("status") == status:
        return False
    entry["status"] = status
    entry["ts"] = now_local().isoformat(timespec="seconds")
    if status == "skip":
        entry.pop("rpe", None)
    storage.mark_dirty()
    return True


def current_slot(user: dict, d: date) -> str:
    pm = R.parse_hhmm(user["profile"]["pm"])
    if S.slot_status(user["log"], d, "am") is None and now_local().time() < pm:
        return "am"
    return "pm"


# ---------- рендер ----------

def mmss(sec: int) -> str:
    return f"{sec // 60}:{sec % 60:02d}"


def render_slot(user: dict, d: date, slot: str) -> tuple[str, InlineKeyboardMarkup]:
    prog, plan = ctx(user, d)
    start = date.fromisoformat(user["start_date"])
    if plan is None:
        if d < start:
            return f"Программа стартует {start:%d.%m}. До этого отдыхай и гуляй.", menu_kb()
        return ("🏁 Программа пройдена. Положи следующий файл в папку program/ "
                "и перезапусти бота, он подхватит его сам."), menu_kb()
    slots = P.slots_for(plan)
    if not slots:
        return (f"🌿 День {plan.day}/{prog['days']}: отдых.\n"
                "Только прогулка 30-40 минут в спокойном темпе."), menu_kb()
    if slot not in slots:
        slot = slots[0]
    iso = d.isoformat()
    when = "" if d == today() else f" · {d:%d.%m}"
    lines = [f"<b>{SLOT_NAME[slot]}{when}</b> · день {plan.day}/{prog['days']} · неделя {plan.week + 1}", ""]
    status = S.slot_status(user["log"], d, slot)
    switch = [btn(SLOT_NAME[s], WCb(d=iso, slot=s, act="show")) for s in slots if s != slot]

    if slot == "am" and plan.test:
        lines += ["🧪 Сегодня контрольный тест вместо утренней тренировки:",
                  "отжимания на максимум, приседания за 60 секунд, планка до отказа.",
                  "Перед тестом разомнись 2 минуты."]
        action = [btn("🧪 Пройти тест", WCb(d=iso, slot=slot, act="test")),
                  btn("✖ Пропустил", WCb(d=iso, slot=slot, act="skip"))]
        first_row = None
    else:
        if slot == "am":
            iv = P.interval_for(prog, plan.week, user["shift"])
            ids = P.am_ids(prog, plan)
            unit = iv["work"] + iv["rest"]
            rounds = prog["rounds"]
            lines += [f"Комплекс {plan.variant} · всего {mmss(unit * len(ids) * rounds)}",
                      f"Кругов: {rounds}, в каждом {len(ids)} упражнений подряд.",
                      f"На каждое упражнение {iv['work']} с работа + {iv['rest']} с отдых.",
                      "",
                      "Старт упражнения (" + " / ".join(f"круг {r + 1}" for r in range(rounds)) + "):"]
            for i, ex_id in enumerate(ids):
                starts = " / ".join(mmss((r * len(ids) + i) * unit) for r in range(rounds))
                lines.append(f"{starts}  {esc(P.resolve(prog, ex_id, plan.week, user['ladder'])[1]['name'])}")
            lines += ["", f"Таймер на телефоне: интервалы {iv['work']} с / {iv['rest']} с, "
                          f"повторов {len(ids) * rounds}."]
            if d == today() and iso not in user["weights"]:
                lines += ["", "⚖️ Не забудь взвеситься до завтрака."]
        else:
            total = sum(item["sec"] for item in P.pm_items(prog, plan))
            lines += [f"Кор и мобильность" + (f", набор {plan.pm_variant}" if plan.pm_variant else "") + f" · всего {mmss(total)}",
                      "Один проход, без кругов и без пауз между упражнениями.", ""]
            t = 0
            for item in P.pm_items(prog, plan):
                name = P.resolve(prog, item["ex"], plan.week, user["ladder"])[1]["name"]
                lines.append(f"{mmss(t)}–{mmss(t + item['sec'])}  {esc(name)}")
                t += item["sec"]
        first_row = [btn("▶ Начать", WCb(d=iso, slot=slot, act="start"))]
        action = [btn("✅ Сделал", WCb(d=iso, slot=slot, act="done")),
                  btn("◐ Частично", WCb(d=iso, slot=slot, act="partial")),
                  btn("✖ Пропустил", WCb(d=iso, slot=slot, act="skip"))]

    if status:
        rpe = user["log"][iso][slot].get("rpe")
        lines += ["", f"Статус: {STATUS_NAME[status]}" + (f", сложность {rpe}/5" if rpe else "")]
    return "\n".join(lines), kb(first_row, action, switch)


def build_session(user: dict, d: date, slot: str) -> Session | None:
    prog, plan = ctx(user, d)
    if plan is None or slot not in P.slots_for(plan) or (slot == "am" and plan.test):
        return None
    if slot == "am":
        cards = [(ex_id, r) for r in range(1, prog["rounds"] + 1) for ex_id in P.am_ids(prog, plan)]
    else:
        cards = [(item["ex"], item["sec"]) for item in P.pm_items(prog, plan)]
    return Session(d=d.isoformat(), slot=slot, idx=0, cards=cards)


def render_card(user: dict, sess: Session) -> tuple[str, InlineKeyboardMarkup]:
    d = date.fromisoformat(sess.d)
    prog, plan = ctx(user, d)
    ex_id, extra = sess.cards[sess.idx]
    step_idx, step, _ = P.resolve(prog, ex_id, plan.week, user["ladder"])
    has_image = P.find_image(config.IMAGE_DIR, ex_id, step_idx) is not None
    n = len(sess.cards)
    if sess.slot == "am":
        per_round = n // prog["rounds"]
        iv = P.interval_for(prog, plan.week, user["shift"])
        head = f"☀️ Круг {extra}/{prog['rounds']} · упражнение {sess.idx % per_round + 1}/{per_round}"
        unit = iv["work"] + iv["rest"]
        t0, total = sess.idx * unit, n * unit
        timing = (f"⏱ {mmss(t0)}–{mmss(t0 + iv['work'])} работа ({iv['work']} с)\n"
                  f"⏸ {mmss(t0 + iv['work'])}–{mmss(t0 + unit)} отдых ({iv['rest']} с)\n"
                  f"Всего {mmss(total)}, после этого упражнения останется {mmss(total - t0 - unit)}")
    else:
        head = f"🌙 Упражнение {sess.idx + 1}/{n}"
        t0 = sum(c[1] for c in sess.cards[:sess.idx])
        total = sum(c[1] for c in sess.cards)
        timing = (f"⏱ {mmss(t0)}–{mmss(t0 + extra)} ({extra // 60} мин без паузы)\n"
                  f"Всего {mmss(total)}, после этого упражнения останется {mmss(total - t0 - extra)}")
    lines = [head, "", f"<b>{esc(step['name'])}</b>", esc(step["how"]), "", timing]
    last = sess.idx == n - 1
    if not last:
        nxt = P.resolve(prog, sess.cards[sess.idx + 1][0], plan.week, user["ladder"])[1]["name"]
        lines.append(f"Дальше: {esc(nxt)}")

    def cb(act: str) -> SCb:
        return SCb(d=sess.d, slot=sess.slot, idx=sess.idx, act=act)

    return "\n".join(lines), kb(
        [btn("🖼 Как делать", cb("img"))] if has_image else None,
        [btn("⬇ Легче", cb("easier")), btn("⬆ Сложнее", cb("harder"))],
        [btn("🏁 Финиш" if last else "Дальше ▶", cb("next"))],
    )


def rpe_prompt(d: date, slot: str) -> tuple[str, InlineKeyboardMarkup]:
    row = [btn(str(v), RCb(d=d.isoformat(), slot=slot, val=v)) for v in range(1, 6)]
    return (f"{SLOT_NAME[slot]}: засчитано. Насколько было тяжело?\n"
            "1 — слишком легко, 3 — в самый раз, 5 — на пределе"), kb(row)


def fmt_delta(x: float, unit: str = "") -> str:
    return f"{x:+.1f}{unit}".replace("+0.0", "0.0").replace("-0.0", "0.0")


def weight_line(user: dict) -> str:
    w = user["weights"]
    if not w:
        return "Вес: нет данных, отправь /weight 92.4"
    t = today()
    cur = S.ma_at(w, t) or w[max(w)]
    first = w[min(w)]
    parts = [f"Вес: {cur:.1f} кг (среднее за 7 дней)"]
    prev = S.ma_at(w, t - timedelta(days=7))
    if prev is not None:
        parts.append(f"за неделю {fmt_delta(cur - prev)}")
    parts.append(f"от старта {fmt_delta(cur - first)}")
    return ", ".join(parts)


def tests_lines(user: dict) -> list[str]:
    tests = user["tests"]
    if not tests:
        return []
    first, last = tests[0], tests[-1]
    out = [f"Тесты ({len(tests)}):"]
    for key, title, _, unit in TESTS:
        if len(tests) == 1:
            out.append(f"  {title}: {last[key]}{unit}")
        else:
            out.append(f"  {title}: {first[key]} → {last[key]}{unit} ({last[key] - first[key]:+d})")
    return out


def render_stats(user: dict) -> str:
    t = today()
    wk = S.weekly(user["log"], t, lambda d: slots_on(user, d))
    prog, plan = ctx(user, t)
    lines = [f"<b>📊 Неделя {wk['from']:%d.%m}–{wk['to']:%d.%m}</b>"]
    if wk["planned"]:
        lines.append(f"Выполнение: {wk['pct']}% — ✅ {wk['done']} · ◐ {wk['partial']} · "
                     f"✖ {wk['missed']} из {wk['planned']}")
    lines.append(f"Серия: {streak(user)} дн.")
    lines.append(weight_line(user))
    lines += tests_lines(user)
    if plan:
        iv = P.interval_for(prog, plan.week, user["shift"])
        lines.append(f"Нагрузка: {iv['work']}/{iv['rest']} с, день {plan.day}/{prog['days']}")
    lines += ["", f"Время курса: сегодня {course_minutes(user, t)} мин, всего {course_minutes(user)} мин"]
    lines += quick_lines(user)
    return "\n".join(lines)


def course_minutes(user: dict, d: date | None = None) -> int:
    """10 минут за выполненную тренировку курса, 5 за частичную."""
    weight = {"done": 10, "partial": 5}
    days = [d.isoformat()] if d else list(user["log"])
    return sum(weight.get(s.get("status"), 0) for k in days for s in user["log"].get(k, {}).values())


def quick_lines(user: dict) -> list[str]:
    q = user["quick"]
    log = q["log"]
    if not log:
        return ["⚡ Разовых тренировок пока нет"]
    t = today().isoformat()
    today_n = sum(1 for w in log if w["date"] == t)
    lvl = Q.suggest_level(q)
    out = [f"⚡ Разовые: {len(log)} ({len(log) * 10} мин), сегодня {today_n}",
           f"Уровень: {lvl} ({quick_cfg['levels'][str(lvl)]})"]
    last = [f"{w['rounds']} кр." for w in log[-5:]]
    out.append("Последние круги: " + " · ".join(last))
    return out


async def send_stats(bot: Bot, chat_id: int, user: dict) -> None:
    await bot.send_message(chat_id, render_stats(user), reply_markup=menu_kb())
    if len(user["weights"]) >= 2:
        png = await asyncio.to_thread(S.weight_chart, dict(user["weights"]))
        await bot.send_photo(chat_id, BufferedInputFile(png, "weight.png"))


# ---------- онбординг и команды ----------

ONB = [
    ("weight", "Текущий вес в кг, например 93:"),
    ("height", "Рост в см, например 176:"),
    ("am", f"Во сколько присылать утреннюю тренировку? Формат ЧЧ:ММ, например {config.DEFAULT_AM}:"),
    ("pm", f"А вечернюю? Например {config.DEFAULT_PM}:"),
]


@router.message(CommandStart())
async def cmd_start(msg: Message) -> None:
    user = storage.user(msg.from_user.id)
    if ready(user):
        await msg.answer("С возвращением. Что делаем?", reply_markup=menu_kb())
        return
    awaiting[msg.from_user.id] = {"kind": "onb", "step": 0}
    await msg.answer("Привет. Две тренировки по 10 минут в день, я напоминаю, считаю и "
                     "постепенно добавляю нагрузку.\n\n" + ONB[0][1])


@router.message(Command("help"))
async def cmd_help(msg: Message) -> None:
    await msg.answer(
        "/today — тренировка на сегодня\n"
        "/yesterday — отметить вчерашний день\n"
        "/weight 92.4 — записать вес\n"
        "/stats — прогресс и график\n"
        "/quick — разовая тренировка на 10 минут\n"
        "/test — контрольный тест\n"
        "/time am 07:30 — время утреннего напоминания (pm для вечернего)",
        reply_markup=menu_kb())


async def need_setup(msg: Message, user: dict) -> bool:
    if ready(user):
        rollover(user, today())
        return False
    await msg.answer("Сначала настройка: /start")
    return True


async def show_day(msg: Message, user: dict, d: date) -> None:
    text, markup = render_slot(user, d, current_slot(user, d) if d == today() else "am")
    await msg.answer(text, reply_markup=markup)


@router.message(Command("today"))
async def cmd_today(msg: Message) -> None:
    user = storage.user(msg.from_user.id)
    if not await need_setup(msg, user):
        await show_day(msg, user, today())


@router.message(Command("yesterday"))
async def cmd_yesterday(msg: Message) -> None:
    user = storage.user(msg.from_user.id)
    if not await need_setup(msg, user):
        await show_day(msg, user, today() - timedelta(days=1))


def parse_num(text: str) -> float | None:
    try:
        return float(text.strip().replace(",", ".").replace("кг", "").strip())
    except ValueError:
        return None


async def save_weight(msg: Message, user: dict, text: str) -> bool:
    w = parse_num(text)
    if w is None or not 30 <= w <= 300:
        await msg.answer("Не понял. Нужен вес в кг, например 92.4")
        return False
    user["weights"][today().isoformat()] = round(w, 1)
    storage.mark_dirty()
    await msg.answer(f"Записал: {w:.1f} кг.\n{weight_line(user)}", reply_markup=menu_kb())
    return True


@router.message(Command("weight"))
async def cmd_weight(msg: Message, command: CommandObject) -> None:
    user = storage.user(msg.from_user.id)
    if await need_setup(msg, user):
        return
    if command.args:
        await save_weight(msg, user, command.args)
    else:
        awaiting[msg.from_user.id] = {"kind": "weight"}
        await msg.answer("Введи вес в кг, например 92.4")


@router.message(Command("quick"))
async def cmd_quick(msg: Message) -> None:
    await quick_begin(msg, msg.from_user.id)


@router.message(Command("stats"))
async def cmd_stats(msg: Message) -> None:
    user = storage.user(msg.from_user.id)
    if not await need_setup(msg, user):
        await send_stats(msg.bot, msg.chat.id, user)


async def start_test(msg: Message, uid: int, d: date | None = None) -> None:
    awaiting[uid] = {"kind": "test", "step": 0, "vals": {}, "d": (d or today()).isoformat()}
    await msg.answer("🧪 Контрольный тест, три замера.\n\n" + TESTS[0][2])


@router.message(Command("test"))
async def cmd_test(msg: Message) -> None:
    user = storage.user(msg.from_user.id)
    if not await need_setup(msg, user):
        await start_test(msg, msg.from_user.id)


@router.message(Command("time"))
async def cmd_time(msg: Message, command: CommandObject) -> None:
    user = storage.user(msg.from_user.id)
    if await need_setup(msg, user):
        return
    parts = (command.args or "").split()
    t = R.parse_hhmm(parts[1]) if len(parts) == 2 else None
    if len(parts) != 2 or parts[0] not in ("am", "pm") or t is None:
        await msg.answer("Формат: /time am 07:30 или /time pm 21:00")
        return
    user["profile"][parts[0]] = f"{t:%H:%M}"
    storage.mark_dirty()
    await msg.answer(f"{SLOT_NAME[parts[0]]}: теперь в {t:%H:%M}.")


# ---------- свободный ввод: только когда бот чего-то ждёт ----------

@router.message(F.text & ~F.text.startswith("/"))
async def on_text(msg: Message) -> None:
    uid = msg.from_user.id
    user = storage.user(uid)
    wait = awaiting.get(uid)
    if not wait:
        await msg.answer("Выбери действие:", reply_markup=menu_kb())
        return

    if wait["kind"] == "weight":
        if await save_weight(msg, user, msg.text):
            awaiting.pop(uid, None)
        return

    if wait["kind"] == "onb":
        key, _ = ONB[wait["step"]]
        prof = user["profile"]
        if key in ("weight", "height"):
            v = parse_num(msg.text)
            lo, hi = (30, 300) if key == "weight" else (120, 230)
            if v is None or not lo <= v <= hi:
                await msg.answer("Не понял, введи число.")
                return
            prof[key] = round(v, 1)
        else:
            t = R.parse_hhmm(msg.text)
            if t is None:
                await msg.answer("Нужен формат ЧЧ:ММ, например 07:30")
                return
            prof[key] = f"{t:%H:%M}"
        storage.mark_dirty()
        wait["step"] += 1
        if wait["step"] < len(ONB):
            await msg.answer(ONB[wait["step"]][1])
        else:
            awaiting.pop(uid, None)
            await msg.answer("Когда стартуем?", reply_markup=kb(
                [btn("Сегодня", MCb(act="start_today")), btn("Завтра", MCb(act="start_tomorrow"))]))
        return

    if wait["kind"] == "test":
        v = parse_num(msg.text)
        if v is None or not 0 <= v <= 2000:
            await msg.answer("Нужно целое число.")
            return
        wait["vals"][TESTS[wait["step"]][0]] = int(v)
        wait["step"] += 1
        if wait["step"] < len(TESTS):
            await msg.answer(TESTS[wait["step"]][2])
            return
        awaiting.pop(uid, None)
        d = date.fromisoformat(wait["d"])
        user["tests"].append({"date": d.isoformat(), **wait["vals"]})
        storage.mark_dirty()
        if ready(user):
            _, plan = ctx(user, d)
            if plan and plan.test and editable(d):
                apply_status(user, d, "am", "done")
        await msg.answer("Тест записан.\n" + "\n".join(tests_lines(user))
                         + f"\nСерия: {streak(user) if ready(user) else 0} дн.", reply_markup=menu_kb())


# ---------- колбэки ----------

@router.callback_query(MCb.filter())
async def on_menu(cb: CallbackQuery, callback_data: MCb) -> None:
    uid = cb.from_user.id
    user = storage.user(uid)
    act = callback_data.act
    await cb.answer()

    if act in ("start_today", "start_tomorrow"):
        prof = user["profile"]
        if ready(user) or not all(k in prof for k in ("weight", "height", "am", "pm")):
            return  # повторное нажатие или старая кнопка
        start = today() + timedelta(days=act == "start_tomorrow")
        user["program_id"] = P.first_program(programs)["id"]
        user["start_date"] = user["joined"] = start.isoformat()
        user["weights"][today().isoformat()] = prof["weight"]
        if start == today():   # карточку покажем сами, напоминание её не дублирует
            user["sent"].setdefault(start.isoformat(), []).append("am")
        storage.mark_dirty()
        await safe_edit(cb.message,
                        f"Готово. Старт {start:%d.%m}, напоминания в {prof['am']} и {prof['pm']}.\n"
                        "Первый день начинается с контрольного теста.")
        if start == today():
            await show_day(cb.message, user, start)
        return

    if act == "quick":
        await quick_begin(cb.message, uid)
        return
    if not ready(user):
        await cb.message.answer("Сначала настройка: /start")
        return
    rollover(user, today())
    if act == "today":
        await show_day(cb.message, user, today())
    elif act == "yesterday":
        await show_day(cb.message, user, today() - timedelta(days=1))
    elif act == "weight":
        awaiting[uid] = {"kind": "weight"}
        await cb.message.answer("Введи вес в кг, например 92.4")
    elif act == "stats":
        await send_stats(cb.bot, cb.message.chat.id, user)
    elif act == "test":
        await start_test(cb.message, uid)
    elif act == "quick":
        await quick_begin(cb.message, uid)
    elif act == "settings":
        p = user["profile"]
        await cb.message.answer(
            f"Утро: {p['am']}, вечер: {p['pm']}\n"
            "Изменить: /time am 07:30 или /time pm 21:00")


async def drop_photos(cb: CallbackQuery, sess: Session | None) -> None:
    """Убирает показанные картинки, чтобы чат не зарастал."""
    if not sess:
        return
    for mid in sess.photos:
        try:
            await cb.bot.delete_message(cb.message.chat.id, mid)
        except Exception:
            pass
    sess.photos.clear()
    sess.photo_for = None


async def show_image(cb: CallbackQuery, user: dict, sess: Session, prog: dict, plan: P.DayPlan) -> None:
    ex_id = sess.cards[sess.idx][0]
    step_idx, step, _ = P.resolve(prog, ex_id, plan.week, user["ladder"])
    found = P.find_image(config.IMAGE_DIR, ex_id, step_idx)
    if not found:
        await cb.answer("Картинки пока нет")
        return
    if sess.photo_for == (sess.idx, step_idx):
        await cb.answer("Картинка уже выше")
        return
    await cb.answer()
    await drop_photos(cb, sess)
    path, exact = found
    caption = f"<b>{esc(step['name'])}</b>\n{esc(step['how'])}"
    base = P.base_step(prog, ex_id)
    if not exact and step_idx != base:
        base_name = prog["exercises"][ex_id]["steps"][base]["name"]
        caption += f"\n\nНа картинке базовый вариант: {esc(base_name)}."
    key = f"{path}:{path.stat().st_mtime_ns}"
    sent = await cb.message.answer_photo(file_ids.get(key) or FSInputFile(path), caption=caption)
    if sent.photo:
        file_ids[key] = sent.photo[-1].file_id
    sess.photos.append(sent.message_id)
    sess.photo_for = (sess.idx, step_idx)


async def finish(cb: CallbackQuery, user: dict, d: date, slot: str, status: str) -> None:
    await drop_photos(cb, sessions.pop(cb.from_user.id, None))
    if not apply_status(user, d, slot, status):
        await cb.answer("Уже отмечено")
        return
    await cb.answer()
    if status == "skip":
        await safe_edit(cb.message, f"{SLOT_NAME[slot]}: пропуск записан. Серия: {streak(user)} дн.\n"
                                    "Следующую не пропускай.", menu_kb())
    else:
        await safe_edit(cb.message, *rpe_prompt(d, slot))


@router.callback_query(WCb.filter())
async def on_workout(cb: CallbackQuery, callback_data: WCb) -> None:
    uid = cb.from_user.id
    user = storage.user(uid)
    if not ready(user) or callback_data.slot not in SLOT_NAME:
        await cb.answer("Кнопка устарела, отправь /start", show_alert=True)
        return
    d = date.fromisoformat(callback_data.d)
    slot, act = callback_data.slot, callback_data.act

    if act == "show":
        await cb.answer()
        await safe_edit(cb.message, *render_slot(user, d, slot))
        return
    if not editable(d):
        await cb.answer("Эту дату уже нельзя менять: отметить можно сегодня и вчера.", show_alert=True)
        return
    if act == "start":
        sess = build_session(user, d, slot)
        if sess is None:
            await cb.answer("Кнопка устарела, открой /today", show_alert=True)
            return
        sessions[uid] = sess
        await cb.answer()
        await safe_edit(cb.message, *render_card(user, sess))
    elif act == "test":
        await cb.answer()
        await start_test(cb.message, uid, d)
    elif act in STATUS_NAME:
        await finish(cb, user, d, slot, act)
    else:
        await cb.answer()


@router.callback_query(SCb.filter())
async def on_step(cb: CallbackQuery, callback_data: SCb) -> None:
    uid = cb.from_user.id
    user = storage.user(uid)
    sess = sessions.get(uid)
    if not sess or sess.d != callback_data.d or sess.slot != callback_data.slot:
        await cb.answer("Сессия устарела, открой /today", show_alert=True)
        return
    if callback_data.idx != sess.idx:   # двойное нажатие или кнопка прошлого шага
        await cb.answer()
        return
    d = date.fromisoformat(sess.d)
    prog, plan = ctx(user, d)
    if plan is None:
        sessions.pop(uid, None)
        await cb.answer("Сессия устарела, открой /today", show_alert=True)
        return

    if callback_data.act == "img":
        await show_image(cb, user, sess, prog, plan)
        return

    if callback_data.act == "next":
        if sess.idx + 1 >= len(sess.cards):
            await finish(cb, user, d, sess.slot, "done")
            return
        sess.idx += 1
        await cb.answer()
        await drop_photos(cb, sess)
        await safe_edit(cb.message, *render_card(user, sess))
        return

    ex_id = sess.cards[sess.idx][0]
    n_steps = len(prog["exercises"][ex_id]["steps"])
    _, _, base = P.resolve(prog, ex_id, plan.week, user["ladder"])
    old = user["ladder"].get(ex_id, 0)
    cur = G.ladder_step(base, old, n_steps) - base   # фактическое смещение без «запаса» за краем
    new = G.adjust_offset(base, cur, 1 if callback_data.act == "harder" else -1, n_steps)
    if new == cur:
        await cb.answer("Это крайний вариант")
        return
    user["ladder"][ex_id] = new
    storage.mark_dirty()
    await cb.answer("Запомнил")
    await drop_photos(cb, sess)
    await safe_edit(cb.message, *render_card(user, sess))


@router.callback_query(RCb.filter())
async def on_rpe(cb: CallbackQuery, callback_data: RCb) -> None:
    user = storage.user(cb.from_user.id)
    entry = user["log"].get(callback_data.d, {}).get(callback_data.slot)
    if not ready(user) or not entry or entry.get("status") not in S.OK or not 1 <= callback_data.val <= 5:
        await cb.answer("Кнопка устарела", show_alert=True)
        return
    if "rpe" in entry:
        await cb.answer("Уже оценено")
        return
    entry["rpe"] = callback_data.val
    note = ""
    if callback_data.slot == "am":   # нагрузкой управляет только утренняя, силовая часть
        old = user["shift"]
        user["shift"], user["rpe_window"] = G.next_shift(old, user["rpe_window"], callback_data.val)
        if user["shift"] > old:
            note = "\n📈 Три лёгких тренировки подряд: интервалы станут плотнее."
        elif user["shift"] < old:
            note = "\n📉 Было на пределе: следующую тренировку облегчаю."
    storage.mark_dirty()
    await cb.answer()
    await safe_edit(cb.message,
                    f"{SLOT_NAME[callback_data.slot]}: {STATUS_NAME[entry['status']]}, "
                    f"сложность {callback_data.val}/5.\nСерия: {streak(user)} дн.{note}", menu_kb())


# ---------- разовая тренировка ----------

QSLOT = {"am": "☀️ Утро", "day": "🌤 День", "pm": "🌙 Вечер"}


def qcb(sess: QuickSession, act: str, val: int = 0) -> QCb:
    return QCb(sid=sess.sid, act=act, val=val)


async def quick_begin(msg: Message, uid: int) -> None:
    global quick_counter
    quick_counter += 1
    sess = QuickSession(sid=quick_counter, level=Q.suggest_level(storage.user(uid)["quick"]))
    quick_sessions[uid] = sess
    await msg.answer("⚡ Разовая тренировка, 10 минут. Когда?",
                     reply_markup=kb([btn(QSLOT[s], qcb(sess, "slot", i)) for i, s in enumerate(Q.SLOTS)]))


def quick_items(sess: QuickSession, user: dict) -> list[tuple[str, int, dict]]:
    out = []
    for ex_id in sess.items:
        ex = quick_cfg["by_id"][ex_id]
        idx, step = Q.step_for(ex, sess.level, user["quick"]["ladder"].get(ex_id, 0))
        out.append((ex_id, idx, step))
    return out


def quick_reroll(sess: QuickSession, user: dict, exclude: set[str] = frozenset()) -> None:
    rng = random.Random()
    sess.items = Q.pick(quick_cfg, sess.slot, Q.recent_ids(user["quick"], quick_cfg.get("avoid_recent", 3)),
                        rng, exclude)


def render_quick_preview(sess: QuickSession, user: dict) -> tuple[str, InlineKeyboardMarkup]:
    lvl = sess.level
    target = quick_cfg["target_rounds"]
    lines = [f"<b>⚡ {QSLOT[sess.slot]}</b> · уровень {lvl}: {quick_cfg['levels'][str(lvl)]}",
             f"AMRAP {quick_cfg['duration_sec'] // 60} мин: крути круги подряд, цель {target}.", ""]
    items = quick_items(sess, user)
    for i, (ex_id, idx, step) in enumerate(items, 1):
        lines.append(f"{i}. {esc(step['name'])} — {Q.dose(step)}")
    lines += ["", "Между кругами отдых по самочувствию, техника важнее скорости.",
              "⬇⬆ меняют вариант конкретного упражнения, ± уровень всей тренировки."]
    rows = [[btn(f"{i} ⬇", qcb(sess, "easier", i - 1)), btn(f"{i} ⬆", qcb(sess, "harder", i - 1))]
            for i in range(1, len(items) + 1)]
    rows.append([btn("− уровень", qcb(sess, "lvl", -1)), btn("+ уровень", qcb(sess, "lvl", 1))])
    rows.append([btn("🔄 Другой набор", qcb(sess, "reroll")), btn("▶ Начать", qcb(sess, "start"))])
    rows.append([btn("✖ Отмена", qcb(sess, "cancel"))])
    return "\n".join(lines), kb(*rows)


def render_quick_running(sess: QuickSession, user: dict) -> tuple[str, InlineKeyboardMarkup]:
    lines = [f"<b>⚡ Пошла тренировка</b> · таймер {quick_cfg['duration_sec'] // 60} мин на телефоне", ""]
    for i, (ex_id, idx, step) in enumerate(quick_items(sess, user), 1):
        lines += [f"<b>{i}. {esc(step['name'])} — {Q.dose(step)}</b>", esc(step["how"])]
    lines += ["", "Когда таймер прозвенит, доделай текущее упражнение и жми «Закончил»."]
    return "\n".join(lines), kb([btn("🏁 Закончил", qcb(sess, "finish"))], [btn("✖ Отмена", qcb(sess, "cancel"))])


def render_quick_rounds(sess: QuickSession) -> tuple[str, InlineKeyboardMarkup]:
    rows = [[btn(str(v), qcb(sess, "rounds", v)) for v in range(1, 6)],
            [btn(str(v), qcb(sess, "rounds", v)) for v in range(6, 11)]]
    return "Сколько полных кругов сделал? Неполный считай за предыдущий.", kb(*rows)


def render_quick_rpe(sess: QuickSession) -> tuple[str, InlineKeyboardMarkup]:
    return (f"Записал {sess.rounds} кр. Насколько было тяжело?\n1 — слишком легко, 3 — в самый раз, 5 — на пределе",
            kb([btn(str(v), qcb(sess, "rpe", v)) for v in range(1, 6)]))


@router.callback_query(QCb.filter())
async def on_quick(cb: CallbackQuery, callback_data: QCb) -> None:
    uid = cb.from_user.id
    user = storage.user(uid)
    q = user["quick"]
    sess = quick_sessions.get(uid)
    act, val = callback_data.act, callback_data.val
    if not sess or sess.sid != callback_data.sid:
        await cb.answer("Кнопка устарела, нажми /quick", show_alert=True)
        return

    if act == "cancel":
        quick_sessions.pop(uid, None)
        await cb.answer()
        await safe_edit(cb.message, "Разовая отменена.", menu_kb())
        return

    if act == "slot" and sess.stage == "slot" and 0 <= val < len(Q.SLOTS):
        sess.slot = Q.SLOTS[val]
        sess.stage = "preview"
        quick_reroll(sess, user)
    elif sess.stage == "preview" and act == "lvl":
        new = G.clamp(sess.level + val, Q.LEVEL_MIN, Q.LEVEL_MAX)
        if new == sess.level:
            await cb.answer("Это крайний уровень")
            return
        sess.level = new
    elif sess.stage == "preview" and act == "reroll":
        quick_reroll(sess, user, exclude=set(sess.items))
    elif sess.stage == "preview" and act in ("easier", "harder") and 0 <= val < len(sess.items):
        ex_id = sess.items[val]
        ex = quick_cfg["by_id"][ex_id]
        base = ex.get("schedule", [0] * 5)[sess.level - 1]
        cur = G.ladder_step(base, q["ladder"].get(ex_id, 0), len(ex["steps"])) - base
        new = G.adjust_offset(base, cur, 1 if act == "harder" else -1, len(ex["steps"]))
        if new == cur:
            await cb.answer("Это крайний вариант")
            return
        q["ladder"][ex_id] = new
        storage.mark_dirty()
    elif sess.stage == "preview" and act == "start":
        sess.stage = "running"
        sess.started = now_local().isoformat(timespec="seconds")
        await cb.answer()
        await safe_edit(cb.message, *render_quick_running(sess, user))
        return
    elif sess.stage == "running" and act == "finish":
        sess.stage = "rounds"
        await cb.answer()
        await safe_edit(cb.message, *render_quick_rounds(sess))
        return
    elif sess.stage == "rounds" and act == "rounds" and 1 <= val <= 10:
        sess.rounds = val
        sess.stage = "rpe"
        await cb.answer()
        await safe_edit(cb.message, *render_quick_rpe(sess))
        return
    elif sess.stage == "rpe" and act == "rpe" and 1 <= val <= 5:
        quick_sessions.pop(uid, None)
        target = quick_cfg["target_rounds"]
        old = sess.level
        q["log"].append({"date": today().isoformat(), "slot": sess.slot, "level": sess.level,
                         "exercises": list(sess.items), "rounds": sess.rounds, "rpe": val,
                         "ts": sess.started})
        q["level"], q["easy"], verdict = Q.next_level(sess.level, q.get("easy", 0), sess.rounds, val, target)
        storage.mark_dirty()
        note = {"up": f"\n📈 Дважды подряд легко и цель выполнена: следующая на уровне {q['level']}.",
                "down": f"\n📉 Тяжеловато: следующую предложу на уровне {q['level']}."}.get(verdict, "")
        if not verdict and q["easy"] == 1:
            note = "\nЕщё одна такая же лёгкая, и подниму уровень."
        await cb.answer()
        await safe_edit(cb.message,
                        f"⚡ {QSLOT[sess.slot]}: {sess.rounds} кр. на уровне {old}, сложность {val}/5. "
                        f"Разовых всего: {len(q['log'])}.{note}", menu_kb())
        return
    else:
        await cb.answer()
        return
    await cb.answer()
    await safe_edit(cb.message, *render_quick_preview(sess, user))


# ---------- напоминания ----------

async def tick(bot: Bot) -> None:
    now = now_local()
    d = now.date()
    iso = d.isoformat()
    for uid in list(storage.data["users"]):
        user = storage.user(uid)
        if not ready(user):
            continue
        rollover(user, d)
        prog, plan = ctx(user, d)
        start = date.fromisoformat(user["start_date"])
        ended = plan is None and (d - start).days == prog["days"]   # первый день после программы
        slots = ["am"] if ended else P.slots_for(plan)
        sent = user["sent"].setdefault(iso, [])
        report_day = d.weekday() == config.REPORT_WEEKDAY and d >= joined(user)
        for ev in R.due_events(user["profile"], sent, now, slots, report_day, config.REMINDER_GRACE_MIN):
            sent.append(ev)          # сначала запись: сбой отправки не должен зациклить рассылку
            storage.mark_dirty()
            try:
                if ev == "report":
                    await send_stats(bot, int(uid), user)
                elif ended:
                    await bot.send_message(int(uid), "🏁 Программа пройдена!\n\n" + render_stats(user),
                                           reply_markup=menu_kb())
                elif S.slot_status(user["log"], d, ev) is None:
                    text, markup = render_slot(user, d, ev)
                    await bot.send_message(int(uid), text, reply_markup=markup)
            except Exception:
                log.exception("reminder %s for %s failed", ev, uid)
        for key in [k for k in user["sent"] if k < (d - timedelta(days=7)).isoformat()]:
            del user["sent"][key]


async def reminder_loop(bot: Bot) -> None:
    while True:
        try:
            await tick(bot)
        except Exception:
            log.exception("reminder tick failed")
        await asyncio.sleep(config.REMINDER_TICK)


# ---------- запуск ----------

async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not config.BOT_TOKEN:
        raise SystemExit("BOT_TOKEN не задан в .env")
    programs.update(P.load_programs(config.PROGRAM_DIR))
    quick_cfg.update(Q.load(config.QUICK_FILE))
    storage.load()
    if config.ALLOWED_IDS:
        router.message.filter(F.from_user.id.in_(config.ALLOWED_IDS))
        router.callback_query.filter(F.from_user.id.in_(config.ALLOWED_IDS))
    bot = Bot(config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    await bot.set_my_commands([
        BotCommand(command="today", description="Тренировка на сегодня"),
        BotCommand(command="weight", description="Записать вес"),
        BotCommand(command="quick", description="Разовая тренировка 10 мин"),
        BotCommand(command="stats", description="Прогресс и график"),
        BotCommand(command="test", description="Контрольный тест"),
        BotCommand(command="yesterday", description="Отметить вчера"),
        BotCommand(command="help", description="Все команды"),
    ])
    tasks = [asyncio.create_task(storage.flush_loop(config.FLUSH_INTERVAL)),
             asyncio.create_task(reminder_loop(bot))]
    try:
        await dp.start_polling(bot)
    finally:
        for t in tasks:
            t.cancel()
        storage.flush()


if __name__ == "__main__":
    asyncio.run(main())
