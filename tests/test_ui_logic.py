"""Логика окна без Qt: история, «цифра+Enter», подтверждения, Esc, автоскрытие, высота."""

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
