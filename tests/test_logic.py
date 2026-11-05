"""Логика без Telegram: python tests/test_logic.py"""
import asyncio
import os
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["DATA_FILE"] = str(Path(tempfile.mkdtemp()) / "progress.json")

import config
import program as P
import progression as G
import reminders as R
import stats as S
import quick as Q
from storage import Storage

progs = P.load_programs(config.PROGRAM_DIR)
m1 = progs["month1"]
MON = date(2026, 10, 5)  # понедельник


def test_program():
    assert P.plan_for(m1, MON, MON - timedelta(days=1)) is None
    assert P.plan_for(m1, MON, MON + timedelta(days=30)) is None
    p1 = P.plan_for(m1, MON, MON)
    assert (p1.day, p1.week, p1.test, p1.rest, p1.variant) == (1, 0, True, False, "A")
    assert P.plan_for(m1, MON, MON + timedelta(days=1)).variant == "B"
    sun = P.plan_for(m1, MON, MON + timedelta(days=6))
    assert sun.rest and P.slots_for(sun) == []
    # после воскресенья чередование продолжается по тренировочным дням
    seq = [P.plan_for(m1, MON, MON + timedelta(days=i)) for i in (0, 1, 2, 3, 4, 5, 7)]
    assert [p.variant for p in seq] == ["A", "B", "C", "D", "E", "A", "B"]   # вс не сбивает круг
    assert [p.pm_variant for p in seq] == ["1", "2", "1", "2", "1", "2", "1"]
    assert P.pm_items(m1, seq[1])[0]["ex"] == "catcow"
    used = {i for ids in m1["am"].values() for i in ids} | {x["ex"] for s in m1["pm"].values() for x in s}
    assert used == set(m1["exercises"]) and len(used) == 35
    assert P.plan_for(m1, MON, MON + timedelta(days=14)).test
    assert P.plan_for(m1, MON, MON + timedelta(days=29)).test
    # тест на дне отдыха: только утро
    sat = date(2026, 10, 3)
    last = P.plan_for(m1, sat, sat + timedelta(days=29))
    assert last.rest and last.test and P.slots_for(last) == ["am"]
    # лесенка: неделя 1 от стола, неделя 3 с колен, смещения
    assert P.resolve(m1, "pushup", 0, {})[1]["name"] == "Отжимания от стола"
    assert P.resolve(m1, "pushup", 2, {})[1]["name"] == "Отжимания с колен"
    assert P.resolve(m1, "pushup", 0, {"pushup": -1})[1]["name"] == "Отжимания от стены"
    assert P.resolve(m1, "pushup", 4, {"pushup": 9})[0] == 3
    assert P.interval_for(m1, 0, 0) == {"work": 30, "rest": 30}
    assert P.interval_for(m1, 4, 0) == {"work": 45, "rest": 15}
    assert P.interval_for(m1, 1, -3) == {"work": 30, "rest": 30}
    bad = dict(m1, am={"A": ["nope"], "B": []})
    assert len(P.validate_program(bad)) == 2


def test_progression():
    assert G.next_shift(0, [], 3) == (0, [3])
    assert G.next_shift(0, [2, 1], 2) == (1, [])
    assert G.next_shift(0, [2, 3], 2) == (0, [2, 3, 2])
    assert G.next_shift(1, [2, 2], 5) == (0, [])
    assert G.next_shift(G.SHIFT_MIN, [], 5) == (G.SHIFT_MIN, [])
    assert G.adjust_offset(1, 0, -1, 4) == -1
    assert G.adjust_offset(1, -1, -1, 4) == -1   # край лесенки
    assert G.adjust_offset(2, 1, 1, 4) == 1


def test_stats():
    log = {}
    def mark(d, slot, st):
        log.setdefault(d.isoformat(), {})[slot] = {"status": st}
    rest = lambda d: d.weekday() == 6
    for i in range(6):                      # пн-сб
        mark(MON + timedelta(days=i), "am", "done")
    nxt_mon = MON + timedelta(days=7)
    assert S.streak(log, nxt_mon, MON, rest) == 6          # вс и пустое «сегодня» не рвут
    mark(nxt_mon, "pm", "partial")
    assert S.streak(log, nxt_mon, MON, rest) == 7
    mark(nxt_mon + timedelta(days=1), "am", "skip")
    assert S.streak(log, nxt_mon + timedelta(days=2), MON, rest) == 0
    wk = S.weekly(log, MON + timedelta(days=6), lambda d: [] if rest(d) else ["am", "pm"])
    assert (wk["planned"], wk["done"], wk["missed"], wk["pct"]) == (12, 6, 6, 50)
    w = {(MON + timedelta(days=i)).isoformat(): 93 - 0.1 * i for i in range(10)}
    assert abs(S.ma_at(w, MON + timedelta(days=9)) - 92.4) < 1e-9
    assert S.ma_at(w, MON - timedelta(days=1)) is None
    assert S.weight_chart(w)[:4] == b"\x89PNG"


def test_reminders():
    prof = {"am": "07:30", "pm": "21:00"}
    at = lambda h, m: datetime(2026, 10, 5, h, m, tzinfo=config.TZ)
    assert R.due_events(prof, [], at(7, 29), ["am", "pm"], False, 180) == []
    assert R.due_events(prof, [], at(7, 30), ["am", "pm"], False, 180) == ["am"]
    assert R.due_events(prof, ["am"], at(8, 0), ["am", "pm"], False, 180) == []
    assert R.due_events(prof, [], at(11, 0), ["am", "pm"], False, 180) == []   # проспали grace
    assert R.due_events(prof, [], at(21, 5), [], True, 180) == ["report"]
    assert R.parse_hhmm("7.05").minute == 5 and R.parse_hhmm("25:00") is None


def test_storage():
    path = Path(tempfile.mkdtemp()) / "p.json"
    st = Storage(path)
    st.user(1)["weights"]["2026-10-05"] = 93.0
    st.mark_dirty(); st.flush()
    st2 = Storage(path); st2.load()
    assert st2.user(1)["weights"] == {"2026-10-05": 93.0} and st2.user(1)["shift"] == 0
    path.write_text("{broken", encoding="utf-8")
    st3 = Storage(path); st3.load()
    assert st3.data == {"users": {}} and list(path.parent.glob("p.json.broken.*"))


def test_quick_logic():
    import random
    cfg = Q.load(config.QUICK_FILE)
    rng = random.Random(1)
    for slot in Q.SLOTS:
        ids = Q.pick(cfg, slot, [], rng)
        assert len(ids) == 4 and len(set(ids)) == 4
        groups = [cfg["by_id"][i]["group"] for i in ids]
        assert all(slot in cfg["by_id"][i].get("slots", Q.SLOTS) for i in ids)
        if slot == "pm":
            assert "cardio" not in groups and (set(groups) & {"back", "mobility"})
    first = Q.pick(cfg, "am", [], rng)
    second = Q.pick(cfg, "am", first, rng)
    assert not set(first) & set(second)
    third = Q.pick(cfg, "am", [], rng, exclude=set(first))
    assert not set(first) & set(third)
    ex = cfg["by_id"]["q_pushup"]
    assert Q.step_for(ex, 1)[1]["name"] == "Отжимания от стены"
    assert Q.step_for(ex, 5)[1]["name"] == "Отжимания с паузой внизу"
    assert Q.step_for(ex, 1, -5)[0] == 0 and Q.step_for(ex, 3, 1)[0] == 3
    assert Q.dose({"sec": 30}) == "30 с" and Q.dose({"reps": 12}) == "12 повт."
    # адаптация: 5/5 вниз, два лёгких подряд с целью вверх, иначе на месте
    assert Q.next_level(3, 0, 4, 5, 4) == (2, 0, "down")
    assert Q.next_level(3, 0, 2, 3, 4) == (2, 0, "down")
    assert Q.next_level(1, 0, 1, 5, 4) == (1, 0, "")
    assert Q.next_level(3, 0, 4, 2, 4) == (3, 1, "")
    assert Q.next_level(3, 1, 5, 1, 4) == (4, 0, "up")
    assert Q.next_level(5, 1, 5, 1, 4) == (5, 2, "")
    assert Q.next_level(3, 1, 3, 2, 4) == (3, 0, "")
    assert Q.next_level(3, 1, 4, 3, 4) == (3, 0, "")
    bad = dict(cfg, exercises=[dict(cfg["exercises"][0], schedule=[0, 0, 9, 0, 0])])
    assert any("schedule" in e for e in Q.validate(bad))


def test_bot_flow():
    """Сквозной сценарий через настоящие хендлеры с поддельными Message/CallbackQuery."""
    import bot as B
    B.programs.update(progs)
    clock = {"now": datetime(2026, 10, 5, 7, 0, tzinfo=config.TZ)}
    B.now_local = lambda: clock["now"]
    uid = 42
    sent = []
    photos = []

    def message(text=None):
        m = SimpleNamespace(from_user=SimpleNamespace(id=uid), text=text, chat=SimpleNamespace(id=uid))
        async def answer(t, reply_markup=None): sent.append(t)
        async def edit_text(t, reply_markup=None): m.kb = reply_markup; sent.append(t)
        async def answer_photo(photo, caption=None):
            photos.append((photo, caption)); m.next_id = getattr(m, "next_id", 100) + 1
            return SimpleNamespace(photo=[SimpleNamespace(file_id=f"FID{m.next_id}")], message_id=m.next_id)
        m.answer, m.edit_text, m.answer_photo = answer, edit_text, answer_photo
        m.bot = SimpleNamespace(send_message=AsyncMock(), send_photo=AsyncMock(), delete_message=AsyncMock())
        return m

    def callback(msg):
        return SimpleNamespace(from_user=SimpleNamespace(id=uid), message=msg, answer=AsyncMock(), bot=msg.bot)

    run = asyncio.run
    run(B.cmd_start(message()))
    for t in ["93", "176", "7:30", "21:00"]:
        run(B.on_text(message(t)))
    msg = message()
    run(B.on_menu(callback(msg), B.MCb(act="start_today")))
    u = B.storage.user(uid)
    assert B.ready(u) and u["weights"] == {"2026-10-05": 93.0} and "тест" in sent[-1]

    # день 1: тест закрывает утро
    run(B.on_workout(callback(msg), B.WCb(d="2026-10-05", slot="am", act="test")))
    for t in ["8", "22", "35"]:
        run(B.on_text(message(t)))
    assert u["tests"][0]["plank"] == 35 and u["log"]["2026-10-05"]["am"]["status"] == "done"

    # вечер: проход по карточкам, «легче», двойное нажатие, финиш, оценка
    run(B.on_workout(callback(msg), B.WCb(d="2026-10-05", slot="pm", act="start")))
    assert "Dead bug" in sent[-1]
    labels = lambda: [b.text for row in msg.kb.inline_keyboard for b in row]
    assert "🖼 Как делать" not in labels()          # у dead bug картинки нет, кнопки тоже
    run(B.on_step(callback(msg), B.SCb(d="2026-10-05", slot="pm", idx=0, act="easier")))
    assert "только ноги" in sent[-1] and u["ladder"]["deadbug"] == -1
    run(B.on_step(callback(msg), B.SCb(d="2026-10-05", slot="pm", idx=0, act="easier")))
    assert u["ladder"]["deadbug"] == -1
    run(B.on_step(callback(msg), B.SCb(d="2026-10-05", slot="pm", idx=0, act="next")))
    # картинка: показывается по кнопке, не дублируется, убирается при переходе, file_id кэшируется
    assert "🖼 Как делать" in labels() and "Bird-dog" in sent[-1]
    run(B.on_step(callback(msg), B.SCb(d="2026-10-05", slot="pm", idx=1, act="img")))
    assert len(photos) == 1 and str(photos[0][0].path).endswith("birddog.jpg") and photos[0][1].startswith("<b>Bird-dog")
    cb2 = callback(msg)
    run(B.on_step(cb2, B.SCb(d="2026-10-05", slot="pm", idx=1, act="img")))
    assert len(photos) == 1 and cb2.answer.call_args.args == ("Картинка уже выше",)
    run(B.on_step(callback(msg), B.SCb(d="2026-10-05", slot="pm", idx=1, act="harder")))
    assert msg.bot.delete_message.call_count == 1
    run(B.on_step(callback(msg), B.SCb(d="2026-10-05", slot="pm", idx=1, act="img")))
    assert photos[1][0] == "FID101" and "базовый вариант: Bird-dog." in photos[1][1] and len(photos[1][1]) < 1024
    run(B.on_step(callback(msg), B.SCb(d="2026-10-05", slot="pm", idx=1, act="easier")))
    for i in range(1, 5):
        run(B.on_step(callback(msg), B.SCb(d="2026-10-05", slot="pm", idx=i, act="next")))
        run(B.on_step(callback(msg), B.SCb(d="2026-10-05", slot="pm", idx=i, act="next")))  # дубль игнорируется
    assert u["log"]["2026-10-05"]["pm"]["status"] == "done" and uid not in B.sessions
    run(B.on_rpe(callback(msg), B.RCb(d="2026-10-05", slot="pm", val=3)))
    run(B.on_rpe(callback(msg), B.RCb(d="2026-10-05", slot="pm", val=5)))
    assert u["log"]["2026-10-05"]["pm"]["rpe"] == 3 and "Серия: 1" in sent[-1]

    # три лёгких утра подряд поднимают уровень интервалов
    for day in (6, 7, 8):
        clock["now"] = datetime(2026, 10, day, 7, 40, tzinfo=config.TZ)
        iso = f"2026-10-{day:02d}"
        run(B.on_workout(callback(msg), B.WCb(d=iso, slot="am", act="done")))
        run(B.on_rpe(callback(msg), B.RCb(d=iso, slot="am", val=2)))
    assert u["shift"] == 1 and "плотнее" in sent[-1]
    assert "35 с работа" in B.render_slot(u, date(2026, 10, 9), "am")[0]

    # задним числом: вчера можно, позавчера нельзя
    clock["now"] = datetime(2026, 10, 9, 7, 40, tzinfo=config.TZ)
    run(B.on_workout(callback(msg), B.WCb(d="2026-10-08", slot="pm", act="partial")))
    cb = callback(msg)
    run(B.on_workout(cb, B.WCb(d="2026-10-07", slot="pm", act="done")))
    assert u["log"]["2026-10-08"]["pm"]["status"] == "partial" and "pm" not in u["log"]["2026-10-07"]
    assert cb.answer.call_args.kwargs.get("show_alert")

    # разовая: не зависит от курса, не трогает серию и лог курса
    B.quick_cfg.update(Q.load(config.QUICK_FILE))
    streak_before, log_before = B.streak(u), dict(u["log"])
    run(B.cmd_quick(message()))
    qs = B.quick_sessions[uid]
    assert "Когда?" in sent[-1]
    run(B.on_quick(callback(msg), B.QCb(sid=qs.sid, act="slot", val=2)))       # вечер
    assert qs.slot == "pm" and len(qs.items) == 4 and "уровень 2" in sent[-1]
    items0 = list(qs.items)
    run(B.on_quick(callback(msg), B.QCb(sid=qs.sid, act="reroll")))
    assert not set(items0) & set(qs.items)
    run(B.on_quick(callback(msg), B.QCb(sid=qs.sid, act="lvl", val=1)))
    assert qs.level == 3 and "уровень 3" in sent[-1]
    run(B.on_quick(callback(msg), B.QCb(sid=qs.sid, act="harder", val=0)))
    assert u["quick"]["ladder"][qs.items[0]] == 1
    stale = callback(msg)
    run(B.on_quick(stale, B.QCb(sid=qs.sid - 1, act="start")))
    assert stale.answer.call_args.kwargs.get("show_alert")
    run(B.on_quick(callback(msg), B.QCb(sid=qs.sid, act="finish")))              # не в той стадии
    assert qs.stage == "preview"
    run(B.on_quick(callback(msg), B.QCb(sid=qs.sid, act="start")))
    assert qs.stage == "running" and "Пошла" in sent[-1]
    run(B.on_quick(callback(msg), B.QCb(sid=qs.sid, act="finish")))
    run(B.on_quick(callback(msg), B.QCb(sid=qs.sid, act="rounds", val=5)))
    run(B.on_quick(callback(msg), B.QCb(sid=qs.sid, act="rpe", val=1)))
    assert uid not in B.quick_sessions and len(u["quick"]["log"]) == 1
    w = u["quick"]["log"][0]
    assert (w["slot"], w["level"], w["rounds"], w["rpe"]) == ("pm", 3, 5, 1) and w["exercises"] == qs.items
    assert u["quick"]["level"] == 3 and u["quick"]["easy"] == 1 and "Ещё одна" in sent[-1]
    assert B.streak(u) == streak_before and u["log"] == log_before
    # вторая лёгкая подряд поднимает уровень; недавние упражнения не повторяются
    run(B.cmd_quick(message())); qs2 = B.quick_sessions[uid]
    run(B.on_quick(callback(msg), B.QCb(sid=qs2.sid, act="slot", val=2)))
    assert not set(qs2.items) & set(qs.items)
    for act, val in (("start", 0), ("finish", 0), ("rounds", 4), ("rpe", 2)):
        run(B.on_quick(callback(msg), B.QCb(sid=qs2.sid, act=act, val=val)))
    assert u["quick"]["level"] == 4 and "📈" in sent[-1]
    assert "⚡ Разовые: 2" in B.render_stats(u) and "Уровень: 4" in B.render_stats(u)
    for sess in (qs, qs2):
        for row in B.render_quick_preview(sess, u)[1].inline_keyboard:
            for b in row:
                assert len(b.callback_data.encode()) <= 64
        assert len(B.render_quick_running(sess, u)[0]) < 4096

    # напоминания: одно на слот, отчёт в воскресенье, без дублей
    bot = SimpleNamespace(send_message=AsyncMock(), send_photo=AsyncMock())
    clock["now"] = datetime(2026, 10, 9, 7, 31, tzinfo=config.TZ)
    run(B.tick(bot)); run(B.tick(bot))
    assert bot.send_message.call_count == 1 and "Комплекс" in bot.send_message.call_args.args[1]
    clock["now"] = datetime(2026, 10, 11, 21, 1, tzinfo=config.TZ)   # воскресенье
    bot.send_message.reset_mock()
    run(B.tick(bot)); run(B.tick(bot))
    assert bot.send_message.call_count == 1 and "Неделя" in bot.send_message.call_args.args[1]

    # вес и статистика
    run(B.save_weight(message(), u, "92,4"))
    assert u["weights"]["2026-10-11"] == 92.4
    assert "Выполнение" in B.render_stats(u)
    # все тексты карточек укладываются в лимит Telegram
    for i in range(30):
        d = date(2026, 10, 5) + timedelta(days=i)
        for slot in ("am", "pm"):
            assert len(B.render_slot(u, d, slot)[0]) < 4096
    for markup in [B.menu_kb(), B.render_slot(u, date(2026, 10, 12), "am")[1]]:
        for row in markup.inline_keyboard:
            for b in row:
                assert len(b.callback_data.encode()) <= 64


for name, fn in list(globals().items()):
    if name.startswith("test_"):
        fn()
        print("ok ", name)
print("all passed")
