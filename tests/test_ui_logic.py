"""Логика окна без Qt: история, «цифра+Enter», подтверждения, Esc, автоскрытие, высота, модель сферы."""

import itertools
import math
import subprocess
import sys
import threading
import time

import pytest

from jarvis.ui import logic
from jarvis.ui.logic import AutoHide, ConfirmQueue, ConfirmRequest, History, esc_action, parse_index


def test_logic_does_not_import_qt() -> None:
    code = "import sys, jarvis.ui.logic; assert 'PySide6' not in sys.modules, 'PySide6 импортирован'"
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr


# --- история ------------------------------------------------------------------------------------------


def test_history_up_down_and_draft() -> None:
    h = History()
    for text in ("открой телегу", "громкость 20", "найди отчёт"):
        h.add(text)
    assert h.up("черновик") == "найди отчёт"
    assert h.up("найди отчёт") == "громкость 20"
    assert h.up("громкость 20") == "открой телегу"
    assert h.up("открой телегу") is None  # выше некуда
    assert h.down("открой телегу") == "громкость 20"
    assert h.down("громкость 20") == "найди отчёт"
    assert h.down("найди отчёт") == "черновик"  # черновик вернулся
    assert h.down("черновик") is None


def test_history_no_consecutive_duplicates_and_limit() -> None:
    h = History(limit=3)
    for text in ("a", "a", " a ", "b", "a", "c", "d"):
        h.add(text)
    assert h.items == ["a", "c", "d"]
    h2 = History()
    h2.add("x")
    h2.add("x")
    h2.add("")
    h2.add("   ")
    assert h2.items == ["x"]


def test_history_empty_and_reset_after_add() -> None:
    h = History()
    assert h.up("что-то") is None
    assert h.down("что-то") is None
    h.add("один")
    assert h.up("") == "один"
    h.add("два")  # отправка возвращает вниз
    assert h.up("новое") == "два"


def test_history_draft_is_kept_only_from_bottom() -> None:
    h = History()
    h.add("1")
    h.add("2")
    assert h.up("мой текст") == "2"
    assert h.up("правка истории") == "1"  # черновик не перезаписан правкой в середине
    assert h.down("1") == "2"
    assert h.down("2") == "мой текст"


# --- «цифра+Enter» ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "count", "expected"),
    [
        ("2", 5, 1),
        ("2.", 5, 1),
        (" 2 ", 5, 1),
        ("1", 1, 0),
        ("5", 5, 4),
        ("0", 5, None),
        ("6", 5, None),
        ("2", 0, None),  # списка нет — обычная отправка
        ("2 файла", 5, None),
        ("открой 2", 5, None),
        ("", 5, None),
        ("-1", 5, None),
        ("²", 5, None),
        ("٢", 5, None),  # не ASCII-цифра
    ],
)
def test_parse_index(text: str, count: int, expected: int | None) -> None:
    assert parse_index(text, count) == expected


# --- подтверждения ------------------------------------------------------------------------------------


def test_confirm_request_resolve_first_wins() -> None:
    req = ConfirmRequest("Завершить chrome.exe?", "12 процессов", "brain")
    assert req.is_brain
    assert not req.done
    assert req.resolve(True) is True
    assert req.resolve(False) is False  # идемпотентно: первый ответ побеждает
    assert req.approved is True
    assert req.wait(0.01) is True
    assert not ConfirmRequest("x").is_brain


def test_confirm_request_wait_timeout_is_no() -> None:
    req = ConfirmRequest("Удалить в корзину отчёт.docx?")
    t0 = time.monotonic()
    assert req.wait(timeout=0.05) is False
    assert time.monotonic() - t0 < 2
    assert req.done and req.approved is False
    assert req.resolve(True) is False  # поздний «да» уже ничего не меняет


def test_confirm_request_wait_from_worker_thread() -> None:
    req = ConfirmRequest("Выключить компьютер?", caller="user")
    result: dict[str, bool] = {}
    worker = threading.Thread(target=lambda: result.setdefault("ok", req.wait(5)))
    worker.start()
    time.sleep(0.02)
    req.resolve(True)
    worker.join(2)
    assert result == {"ok": True}


def test_confirm_request_remaining_and_expired() -> None:
    req = ConfirmRequest("x", created=100.0)
    assert req.remaining(now=130.0) == 30.0
    assert not req.expired(now=159.9)
    assert req.expired(now=160.0)
    assert req.remaining(now=200.0) == 0.0


def test_queue_two_sources_one_at_a_time() -> None:
    q = ConfirmQueue()
    local = ConfirmRequest("Завершить chrome.exe?", caller="user", created=0.0)  # обработчик процесса
    pipe = ConfirmRequest("Открыть https://example.com?", caller="brain", created=0.0)  # из pipe
    assert q.push(local, now=1.0) is True  # очередь была пуста — показать
    assert q.push(pipe, now=1.1) is False  # ждёт своей очереди
    assert q.push(local, now=1.2) is False  # повтор того же запроса не дублируется
    assert len(q) == 2
    assert q.current is local
    assert q.key("esc", now=2.0) is False
    assert local.approved is False
    assert q.current is pipe
    assert q.shown_at == 2.0  # у следующего — своя защита 700 мс
    assert q.key("ctrl+enter", now=2.5) is None  # защита
    assert q.key("ctrl+enter", now=2.8) is True
    assert pipe.approved is True
    assert q.current is None and q.shown_at is None


def test_queue_guard_700ms_for_keys_and_clicks() -> None:
    q = ConfirmQueue()
    req = ConfirmRequest("Завершить chrome.exe (12 процессов)?", created=10.0)
    q.push(req, now=10.0)
    for t in (10.0, 10.3, 10.69):
        assert q.key("ctrl+enter", now=t) is None
        assert q.key("enter", now=t) is None
        assert q.key("esc", now=t) is None
        assert q.click(True, now=t) is None
    assert not req.done
    assert q.guarded(now=10.69)
    assert not q.guarded(now=10.7)
    assert q.click(True, now=10.7) is True
    assert req.approved is True


def test_queue_enter_never_confirms() -> None:
    q = ConfirmQueue()
    req = ConfirmRequest("Переместить отчёт.docx в корзину?", created=0.0)
    q.push(req, now=0.0)
    assert q.key("enter", now=5.0) is False
    assert req.approved is False
    req2 = ConfirmRequest("ещё", created=0.0)
    q.push(req2, now=6.0)
    assert q.key("space", now=9.0) is None  # другие клавиши — не ответ
    assert q.key("esc", now=9.0) is False
    assert req2.approved is False


def test_queue_ctrl_enter_confirms() -> None:
    q = ConfirmQueue()
    req = ConfirmRequest("Завершить chrome.exe?", created=0.0)
    q.push(req, now=0.0)
    assert q.key("ctrl+enter", now=1.0) is True
    assert req.approved is True


def test_queue_timeout_60s_is_no_and_removed() -> None:
    q = ConfirmQueue()
    a = ConfirmRequest("a", created=0.0)
    b = ConfirmRequest("b", created=30.0)
    q.push(a, now=0.0)
    q.push(b, now=30.0)
    assert q.prune(now=59.9) is False
    assert q.prune(now=60.0) is True  # a истёк — «нет», показан b
    assert a.approved is False
    assert q.current is b and q.shown_at == 60.0
    assert q.prune(now=90.0) is True
    assert b.approved is False and q.current is None


def test_queue_drops_requests_resolved_by_worker_timeout() -> None:
    q = ConfirmQueue()
    a = ConfirmRequest("a", created=0.0)
    b = ConfirmRequest("b", created=0.0)
    c = ConfirmRequest("c", created=0.0)
    for r in (a, b, c):
        q.push(r, now=0.0)
    b.wait(0.001)  # рабочий поток не дождался — «нет»
    a.resolve(False)
    assert q.prune(now=1.0) is True
    assert q.current is c and len(q) == 1
    assert q.push(b, now=1.0) is False  # отвеченный запрос в очередь не встаёт


def test_queue_answer_skips_already_resolved_next() -> None:
    q = ConfirmQueue()
    a = ConfirmRequest("a", created=0.0)
    b = ConfirmRequest("b", created=0.0)
    c = ConfirmRequest("c", created=0.0)
    for r in (a, b, c):
        q.push(r, now=0.0)
    b.resolve(False)
    assert q.key("esc", now=1.0) is False
    assert q.current is c and q.shown_at == 1.0


def test_queue_answer_after_worker_timeout_reports_actual_result() -> None:
    q = ConfirmQueue()
    req = ConfirmRequest("a", created=0.0)
    q.push(req, now=0.0)
    req.resolve(False)  # таймаут в рабочем потоке раньше нажатия
    assert q.key("ctrl+enter", now=1.0) is False
    assert q.current is None


def test_queue_deny_all() -> None:
    q = ConfirmQueue()
    reqs = [ConfirmRequest(str(i), created=0.0) for i in range(3)]
    for r in reqs:
        q.push(r, now=0.0)
    assert q.deny_all() == 3
    assert all(r.approved is False for r in reqs)
    assert q.current is None and q.shown_at is None


def test_queue_no_current_ignores_keys() -> None:
    q = ConfirmQueue()
    assert q.key("enter", now=0.0) is None
    assert q.click(True, now=0.0) is None
    assert not q.guarded(now=0.0)


# --- Esc ----------------------------------------------------------------------------------------------


def test_esc_states() -> None:
    assert esc_action(confirm_shown=True, busy=True, cancel_sent=False) == "deny"
    assert esc_action(confirm_shown=True, busy=False, cancel_sent=True) == "deny"
    assert esc_action(confirm_shown=False, busy=True, cancel_sent=False) == "cancel"
    assert esc_action(confirm_shown=False, busy=True, cancel_sent=True) == "hide"  # второй Esc
    assert esc_action(confirm_shown=False, busy=False, cancel_sent=False) == "hide"


# --- автоскрытие --------------------------------------------------------------------------------------


def test_autohide_after_successful_action() -> None:
    a = AutoHide(1.2)
    assert a.arm(ok=True, autohide=True) is True
    assert a.fire(hovered=False) is True
    assert a.fire(hovered=False) is False  # один раз


@pytest.mark.parametrize(("ok", "autohide"), [(False, True), (True, False), (False, False)])
def test_autohide_not_armed(ok: bool, autohide: bool) -> None:
    a = AutoHide(1.2)
    assert a.arm(ok=ok, autohide=autohide) is False
    assert a.fire(hovered=False) is False


def test_autohide_zero_delay_disabled() -> None:
    a = AutoHide(0)
    assert a.arm(ok=True, autohide=True) is False


def test_autohide_key_cancels() -> None:
    a = AutoHide(1.2)
    a.arm(ok=True, autohide=True)
    a.cancel()  # человек начал печатать
    assert a.fire(hovered=False) is False
    assert a.leave() is False


def test_autohide_waits_while_mouse_over() -> None:
    a = AutoHide(1.2)
    a.arm(ok=True, autohide=True)
    assert a.fire(hovered=True) is False
    assert a.state == "wait_leave"
    assert a.leave() is True  # мышь ушла — отсчёт заново
    assert a.fire(hovered=False) is True
    assert a.leave() is False


# --- высота и текст -----------------------------------------------------------------------------------


def test_target_height_grows_and_caps_at_40_percent() -> None:
    assert logic.target_height(60, 0, 1080) == 60
    assert logic.target_height(60, 200, 1080) == 260
    assert logic.target_height(60, 5000, 1080) == 432  # 40 % от 1080
    assert logic.target_height(300, 500, 1080) == 432
    assert logic.target_height(500, 100, 1080) == 500  # обязательное (подтверждение) видно всегда
    assert logic.target_height(60, -5, 1080) == 60


def test_display_text_hides_russian_stress_marks() -> None:
    """Знак ударения мозг ставит для голоса (brain.SPOKEN_HINT) — в окне его нет; «é» из латиницы остаётся."""
    assert logic.display_text("Это за\u0301мок, а не замо\u0301к.") == "Это замок, а не замок."
    assert logic.display_text("\u0301к на двери") == "к на двери"  # гласная была в прошлом куске потока
    assert logic.display_text("cafe\u0301") == "cafe\u0301"


def test_display_text_neutralizes_controls_and_bidi() -> None:
    raw = "отчёт‮xcod.exe\x00\r\n<b>x</b>\t1"
    shown = logic.display_text(raw)
    assert "‮" not in shown and "\x00" not in shown and "\r" not in shown
    assert "�" in shown
    assert "<b>x</b>" in shown  # HTML не трогаем — его просто не разбирают
    assert "\n" in shown
    assert logic.display_text("обычный текст") == "обычный текст"


def test_level_labels() -> None:
    assert logic.LEVEL_LABELS == {
        "grammar": "грамматика",
        "hands": "руки",
        "brain": "GPT",
        "local": "локально",
    }


# --- сфера (OrbModel) ---------------------------------------------------------------------------------

from jarvis.ui.logic import (  # noqa: E402
    ORB_EXTENT,
    ORB_FLAME_MAX,
    ORB_FLASH_S,
    ORB_LEVELS,
    ORB_LOBE_LIMIT,
    ORB_MAX_DT,
    ORB_PALETTES,
    ORB_REST,
    ORB_WAKE_S,
    OrbModel,
)


def _run(m: OrbModel, seconds: float, step: float = 1 / 60) -> bool:
    alive = True
    for _ in range(round(seconds / step)):
        alive = m.tick(step)
    return alive


def _calm_s(m: OrbModel, limit: float = 10.0, step: float = 1 / 60) -> float:
    """Через сколько секунд модель сама перестаёт анимировать."""
    t = 0.0
    while m.tick(step):
        t += step
        assert t < limit, f"сфера не успокоилась за {limit} с (состояние {m.state})"
    return t


def test_orb_starts_idle_and_still() -> None:
    m = OrbModel()
    assert m.state == "idle" and not m.animating and m.resting
    assert m.tick(1 / 60) is False
    assert m.energy == ORB_REST


def test_orb_wake_then_listen_and_freeze() -> None:
    m = OrbModel()
    m.wake()
    assert m.state == "wake" and m.animating and m.shown and not m.resting
    _run(m, ORB_WAKE_S + 0.05)
    assert m.state == "listen"
    assert _calm_s(m) < 2.5  # «пробуждение ~0,7 с, затем замирает»
    frozen = (m.t, m.frame())
    assert m.tick(0.5) is False
    assert (m.t, m.frame()) == frozen  # покой: кадр застыл, часы стоят


def test_orb_key_pulse_decays() -> None:
    m = OrbModel()
    m.wake()
    _calm_s(m)
    glow = m.glow()
    m.key()
    assert m.animating and m.glow() > glow
    assert _calm_s(m) < 1.5
    assert m.glow() == pytest.approx(glow)


def test_orb_request_flow() -> None:
    m = OrbModel()
    m.wake()
    m.begin()
    assert m.state == "work" and m.busy
    m.set_level("hands")
    assert m.level == "hands"
    assert _run(m, 12.0) is True  # пока идёт запрос — анимация не кончается
    rest = m.energy
    assert rest > 0.5  # работа ярче покоя
    m.chunk()
    assert m.state == "speak"
    m.confirm(True)
    assert m.state == "confirm"
    m.confirm(False)
    assert m.state == "speak"
    m.done(True)
    assert m.state == "success" and m.flash_kind == "success" and not m.busy
    assert m.frame().flash > 0.0 or _run(m, 0.05)
    _run(m, ORB_FLASH_S + 0.02)
    assert m.state == "listen"
    assert _calm_s(m) < 2.5
    assert m.energy == pytest.approx(ORB_REST)


def test_orb_energy_decays_monotonically_after_done() -> None:
    m = OrbModel()
    m.wake()
    m.begin()
    m.set_level("brain")
    _run(m, 1.5)
    m.done(True)
    values = []
    for _ in range(120):
        m.tick(1 / 60)
        values.append(m.energy)
    assert all(b <= a + 1e-9 for a, b in itertools.pairwise(values))
    assert values[-1] < values[0]


def test_orb_error_flash_is_crimson() -> None:
    m = OrbModel()
    m.wake()
    m.begin()
    m.done(False)
    assert m.state == "error" and m.flash_kind == "error"
    _run(m, 0.1)
    f = m.frame()
    assert f.flash > 0.5
    r, g, _b = f.flash_rgb
    assert r > 200 and g < 60  # малиново-красная
    s = OrbModel()
    s.wake()
    s.begin()
    s.done(True)
    _run(s, 0.1)
    assert min(s.frame().flash_rgb) > 230  # лавандово-белая


def test_orb_cancel_is_soft_without_flash() -> None:
    m = OrbModel()
    m.wake()
    m.begin()
    _run(m, 0.5)
    m.done(False, cancelled=True)
    assert m.state == "listen" and m.flash_t < 0
    assert _calm_s(m) < 3.0


def test_orb_hide_rules() -> None:
    m = OrbModel()
    m.wake()
    m.hide()
    assert m.state == "idle" and not m.shown
    _calm_s(m)
    assert m.resting
    m.wake()
    m.begin()
    m.hide()  # окно спрятали посреди запроса — работа продолжается на сфере рабочего стола
    assert m.state == "work" and not m.resting
    m.done(True)
    assert m.state == "success"  # итог видно и без окна
    _run(m, ORB_FLASH_S + 0.05)
    assert m.state == "idle"
    _calm_s(m)
    assert m.resting


def test_orb_hide_during_flash_lets_it_finish() -> None:
    m = OrbModel()
    m.wake()
    m.begin()
    m.done(True)
    m.hide()
    assert m.state == "success" and not m.resting
    _run(m, ORB_FLASH_S + 0.05)
    assert m.state == "idle"


def test_orb_wake_during_request_only_pulses() -> None:
    m = OrbModel()
    m.wake()
    m.begin()
    m.hide()
    m.wake()
    assert m.state == "work" and m.shown


def test_orb_late_done_is_ignored() -> None:
    m = OrbModel()
    m.wake()
    _calm_s(m)
    m.done(True)  # Done прошлого запроса пришёл, когда сфера уже в покое
    assert m.state == "listen" and m.flash_t < 0


def test_orb_without_animations_is_static() -> None:
    m = OrbModel(animations=False)
    m.wake()
    assert m.state == "listen" and not m.animating
    m.key()
    assert m.pulse == 0.0
    m.begin()
    m.set_level("brain")
    assert m.state == "work" and not m.animating
    work = m.frame()
    assert m.tick(1.0) is False
    m.done(True)
    assert m.state == "listen" and not m.animating  # без вспышки
    assert m.frame().glow < work.glow  # статичные, но разные кадры состояний
    m.hide()
    assert m.state == "idle"


def test_orb_tick_dt_is_bounded() -> None:
    m = OrbModel()
    m.wake()
    t0 = m.t
    m.tick(30.0)  # пропуск кадров (сон, нагрузка) — без рывка
    assert m.t - t0 <= ORB_MAX_DT * 3.0
    m.tick(-1.0)


def test_orb_advance_shared_clock() -> None:
    m = OrbModel()
    m.wake()
    m.advance(10.0)
    t1 = m.t
    m.advance(10.1)
    t2 = m.t
    m.advance(10.1)  # второй виджет в тот же момент
    m.advance(10.05)  # время назад — ничего
    assert t2 > t1 and m.t == t2


def test_orb_frame_is_deterministic() -> None:
    a, b = OrbModel(), OrbModel()
    for m in (a, b):
        m.wake()
        m.begin()
        m.set_level("grammar")
        m.chunk()
        _run(m, 1.3)
    assert a.frame() == b.frame()
    assert a.frame(5.0) == a.frame(5.0)
    assert a.frame(5.0) != a.frame(5.3)  # и при этом живой


def test_orb_shapes_stay_in_bounds() -> None:
    """Доли — внутри стекла; языки, искры и кольца — в пределах 1 + ORB_EXTENT (квадрат виджета)."""
    m = OrbModel()
    m.wake()
    m.begin()
    m.set_level("brain")
    for i in range(400):
        m.tick(1 / 30)
        if i % 7 == 0:
            m.chunk()
        for energy in (ORB_REST, 0.8, 1.2):
            m.energy = energy
            f = m.frame()
            for lobe in f.lobes:
                assert math.hypot(lobe.x, lobe.y) + lobe.r <= ORB_LOBE_LIMIT + 1e-9
                for x, y, r, _fade in lobe.trail:
                    assert math.hypot(x, y) + r <= ORB_LOBE_LIMIT + 1e-9
            assert ORB_LOBE_LIMIT <= 1.0
            for flame in f.flames:
                assert 1.0 <= flame.ring <= 1.0 + ORB_EXTENT
                for pts in flame.tongues:
                    for k in range(0, 14, 2):
                        assert math.hypot(pts[k], pts[k + 1]) <= 1.0 + ORB_FLAME_MAX + 1e-9
            assert ORB_FLAME_MAX <= ORB_EXTENT
            for s in f.sparks:
                assert math.hypot(s.x, s.y) + s.r * s.stretch * 2.2 <= 1.0 + ORB_EXTENT
            assert f.ring <= 1.0 + ORB_EXTENT


def test_orb_flames_rise_and_calm_at_rest() -> None:
    """Языки выше вверху сферы; в работе — выше, чем в покое."""
    rest, work = OrbModel(), OrbModel()
    work.wake()
    work.begin()
    work.set_level("brain")
    _run(work, 1.5)

    def tips(m: OrbModel) -> tuple[float, float]:
        f = m.frame()
        ys = [pts[7] for flame in f.flames for pts in flame.tongues]
        return min(ys), max(ys)

    top_rest, _ = tips(rest)
    top_work, bottom_work = tips(work)
    assert top_work < top_rest < -1.0  # ось y вниз: выше — меньше
    assert top_work < -1.35  # вверху язык выше трети радиуса
    assert bottom_work < 1.25  # внизу языки короткие


def test_orb_level_tints() -> None:
    """Оттенок уровня тонкий: гамма остаётся тёмно-фиолетовой, но уровни различимы."""

    def lobes(level: str, local: bool = False) -> list[tuple[int, int, int]]:
        m = OrbModel()
        m.set_local(local)
        m.wake()
        m.begin()
        m.set_level(level)
        _run(m, 2.0)
        return list(m.palette().lobes)

    base = list(ORB_PALETTES[""].lobes)
    for level in ORB_LEVELS:
        got = lobes(level, local=level == "local")
        assert got != base, level
        assert got == list(ORB_PALETTES[level].lobes), level
    assert any(b > r for r, _g, b in lobes("grammar"))  # холоднее — сине-фиолетовый
    assert any(r > 150 and b > 150 and g < 80 for r, g, b in lobes("hands"))  # маджента
    assert any(g > 150 and b > 120 and r < 60 for r, g, b in lobes("local", local=True))  # бирюза
    brain = OrbModel()
    brain.wake()
    brain.begin()
    brain.set_level("brain")
    _run(brain, 2.0)
    hands = OrbModel()
    hands.wake()
    hands.begin()
    hands.set_level("hands")
    _run(hands, 2.0)
    assert brain.frame().core > hands.frame().core  # GPT — ярче ядро
    core, edge = ORB_PALETTES[""].core, ORB_PALETTES[""].edge
    assert max(core) < 20 and max(edge) < 50  # почти чёрное тело


def test_orb_local_mode_tint_at_rest() -> None:
    m = OrbModel()
    m.set_local(True)
    _calm_s(m)
    assert m.palette() == ORB_PALETTES["local"]
    m.set_local(False)
    assert m.animating
    _calm_s(m)
    assert m.palette() == ORB_PALETTES[""]


def test_orb_sparks_only_while_working() -> None:
    m = OrbModel()
    m.wake()
    _run(m, 0.3)
    assert m.frame().sparks == ()
    m.begin()
    _run(m, 1.0)
    assert len(m.frame().sparks) > 0
    m.done(True)
    _run(m, 3.0)
    assert m.frame().sparks == ()


def test_orb_voice_level_pulse() -> None:
    m = OrbModel()
    m.wake()
    _calm_s(m)
    m.voice(0.8)
    assert m.pulse == pytest.approx(0.4) and m.animating
    m.voice(0.1)
    assert m.pulse == pytest.approx(0.4)  # тихий кадр не гасит импульс — он затухает сам
    m.voice(5.0)
    assert m.pulse == pytest.approx(0.5)
    m.voice(-1.0)
    assert _calm_s(m) < 2.0
    quiet = OrbModel(animations=False)
    quiet.voice(1.0)
    assert quiet.pulse == 0.0


def test_orb_listen_without_window() -> None:
    """Голос без окна: сфера просыпается и слушает (не в покое для режима auto), после — назад в idle."""
    m = OrbModel()
    m.listen(True)
    assert m.state == "wake" and m.listening and not m.shown and not m.resting
    _run(m, ORB_WAKE_S + 0.05)
    assert m.state == "listen"  # покой слушания, а не idle
    m.voice(0.8)
    assert m.pulse > 0.3
    m.listen(False)
    assert m.state == "idle" and not m.listening and m.resting
    m.listen(True)
    m.listen(False)  # отпустил до конца пробуждения
    assert m.state == "idle" and m.wake_t < 0
    m.begin()
    m.listen(True)  # идёт запрос — только импульс, работа не прерывается
    assert m.state == "work" and m.listening
    m.listen(False)
    assert m.state == "work"


def test_orb_speech_and_fail() -> None:
    m = OrbModel()
    m.speech(0.8)
    assert m.talking and not m.resting and m.pulse == pytest.approx(0.36)
    m.speech(0.0)
    assert not m.talking
    assert _calm_s(m) < 2.5 and m.resting
    m.fail()
    assert m.state == "error" and m.flash_kind == "error" and m.flash_t == 0.0
    _run(m, ORB_FLASH_S + 0.1)
    assert m.state == "idle"
    m.begin()
    m.fail()  # во время запроса — без вспышки
    assert m.state == "work"
    still = OrbModel(animations=False)
    still.fail()
    still.speech(1.0)
    assert still.state == "idle" and still.pulse == 0.0 and still.talking


def test_orb_listeners_and_state_changes_in_tick() -> None:
    m = OrbModel()
    seen: list[str] = []
    m.subscribe(lambda: seen.append(m.state))
    m.wake()
    assert seen == ["wake"]
    _run(m, ORB_WAKE_S + 0.05)
    assert seen[-1] == "listen"  # конец пробуждения внутри tick() — подписчики узнали

    def dead() -> None:
        raise RuntimeError("Internal C++ object already deleted")

    m.subscribe(dead)
    m.key()
    m.key()  # удалённый виджет отписан, остальные зовутся
    assert len(m._listeners) == 1
