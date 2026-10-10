"""Канал подтверждений: ConfirmServer и клиент в одном процессе (Linux — AF_UNIX, Windows — AF_PIPE)."""

import json
import os
import sys
import threading
import time
from collections.abc import Callable, Iterator
from multiprocessing import connection
from pathlib import Path

import pytest

from pc import confirm_client as cc

Calls = list[tuple[str, str, str]]


@pytest.fixture
def servers() -> Iterator[Callable[..., cc.ConfirmServer]]:
    started: list[cc.ConfirmServer] = []

    def make(callback: cc.ConfirmHandler) -> cc.ConfirmServer:
        srv = cc.ConfirmServer(callback)
        srv.start()
        started.append(srv)
        return srv

    yield make
    for srv in started:
        srv.close()


def recorder(answer: bool) -> tuple[Calls, cc.ConfirmHandler]:
    calls: Calls = []

    def callback(summary: str, details: str, caller: str) -> bool:
        calls.append((summary, details, caller))
        return answer

    return calls, callback


def raw_request(address: str, payload: bytes, key: bytes | None = None) -> dict:
    """Запрос мимо confirm(): проверка того, что сервер делает с недоверенными данными."""
    with connection.Client(address, family=cc._family(), authkey=key or cc.load_key()) as conn:
        conn.send_bytes(payload)
        assert conn.poll(5), "сервер не ответил"
        return json.loads(conn.recv_bytes(1024))


def test_yes_reaches_callback_as_brain(servers, monkeypatch: pytest.MonkeyPatch) -> None:
    calls, callback = recorder(True)
    srv = servers(callback)
    monkeypatch.setenv(cc.PIPE_ENV, srv.address)
    assert cc.confirm("Закрыть окно «Блокнот»?", "notepad.exe\nотчёт.txt", "brain") is True
    assert calls == [("Закрыть окно «Блокнот»?", "notepad.exe\nотчёт.txt", "brain")]


def test_no(servers, monkeypatch: pytest.MonkeyPatch) -> None:
    calls, callback = recorder(False)
    monkeypatch.setenv(cc.PIPE_ENV, servers(callback).address)
    assert cc.confirm("Выключить компьютер?", "", "brain") is False
    assert len(calls) == 1


def test_caller_from_pipe_is_always_brain(servers, monkeypatch: pytest.MonkeyPatch) -> None:
    calls, callback = recorder(True)
    srv = servers(callback)
    monkeypatch.setenv(cc.PIPE_ENV, srv.address)
    assert cc.confirm("Открыть ссылку?", "https://example.com", "user") is True
    payload = json.dumps({"summary": "Очистить корзину?", "details": "", "caller": "user"}).encode()
    assert raw_request(srv.address, payload) == {"approved": True}
    assert [c[2] for c in calls] == ["brain", "brain"]


def test_timeout_is_no(servers, monkeypatch: pytest.MonkeyPatch) -> None:
    release = threading.Event()

    def slow(summary: str, details: str, caller: str) -> bool:
        release.wait(10)
        return True

    monkeypatch.setattr(cc, "TIMEOUT_S", 0.5)
    monkeypatch.setenv(cc.PIPE_ENV, servers(slow).address)
    started = time.monotonic()
    try:
        assert cc.confirm("Ввести текст?", "привет", "brain") is False
        assert time.monotonic() - started < 3
    finally:
        release.set()


def test_callback_exception_is_no(servers, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(summary: str, details: str, caller: str) -> bool:
        raise RuntimeError("окно закрыто")

    monkeypatch.setenv(cc.PIPE_ENV, servers(broken).address)
    assert cc.confirm("Завершить процесс?", "notepad.exe", "brain") is False


def test_wrong_key_is_no_and_callback_not_called(servers, monkeypatch: pytest.MonkeyPatch) -> None:
    calls, callback = recorder(True)
    monkeypatch.setenv(cc.PIPE_ENV, servers(callback).address)
    cc.key_path().write_bytes(os.urandom(cc.KEY_BYTES))  # у клиента другой ключ, чем у сервера
    assert cc.confirm("Переместить в корзину?", r"C:\Users\me\отчёт.docx", "brain") is False
    assert calls == []


def test_no_env_is_no(servers) -> None:
    calls, callback = recorder(True)
    servers(callback)
    assert os.environ.get(cc.PIPE_ENV) is None
    assert cc.confirm("Заблокировать?", "", "brain") is False
    assert calls == []


def test_no_key_file_is_no(servers, monkeypatch: pytest.MonkeyPatch) -> None:
    calls, callback = recorder(True)
    monkeypatch.setenv(cc.PIPE_ENV, servers(callback).address)
    cc.key_path().unlink()
    assert cc.confirm("Перезагрузить?", "", "brain") is False
    assert calls == []
    assert not cc.key_path().exists()  # клиент ключ не создаёт


def test_no_server_is_no(monkeypatch: pytest.MonkeyPatch) -> None:
    cc.load_key(create=True)
    monkeypatch.setenv(cc.PIPE_ENV, cc.pipe_address(pid=999_999))
    started = time.monotonic()
    assert cc.confirm("Сон?", "", "brain") is False
    assert time.monotonic() - started < 25


def test_foreign_address_is_no(monkeypatch: pytest.MonkeyPatch) -> None:
    cc.load_key(create=True)
    monkeypatch.setenv(cc.PIPE_ENV, r"\\.\pipe\..\..\C:\Users\me\отчёт.docx")
    assert cc.confirm("Сон?", "", "brain") is False


def test_pipe_name_check() -> None:
    assert cc._PIPE_RX.fullmatch(r"\\.\pipe\jarvis-confirm-me-1234")
    assert cc._PIPE_RX.fullmatch(r"\\.\pipe\jarvis-confirm-Пользователь-1234-2")
    assert not cc._PIPE_RX.fullmatch(r"\\.\pipe\jarvis-confirm-me-1\..\..\C:\x")
    assert not cc._PIPE_RX.fullmatch(r"\\.\pipe\other")
    assert not cc._PIPE_RX.fullmatch(r"C:\Users\me\pipe.key")


def test_too_long_request_is_no_without_truncation(servers, monkeypatch: pytest.MonkeyPatch) -> None:
    calls, callback = recorder(True)
    monkeypatch.setenv(cc.PIPE_ENV, servers(callback).address)
    assert cc.confirm("Ввести текст?", "я" * (cc.MAX_DETAILS + 1), "brain") is False
    assert calls == []


def test_malformed_requests_rejected_without_callback(servers) -> None:
    calls, callback = recorder(True)
    srv = servers(callback)
    bad = [
        b"\xff\xfe",
        b"not json",
        b"[1, 2]",
        json.dumps({"summary": 1, "details": ""}).encode(),
        json.dumps({"summary": "  ", "details": ""}).encode(),
        json.dumps({"summary": "x" * (cc.MAX_SUMMARY + 1)}).encode(),
        json.dumps({"summary": "ok", "details": ["список"]}).encode(),
    ]
    for payload in bad:
        assert raw_request(srv.address, payload) == {"approved": False}
    assert calls == []


def test_silent_client_does_not_block_next(servers, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cc, "HANDSHAKE_TIMEOUT_S", 0.5)
    calls, callback = recorder(True)
    srv = servers(callback)
    monkeypatch.setenv(cc.PIPE_ENV, srv.address)
    silent = connection.Client(srv.address, family=cc._family())  # подключился и молчит
    try:
        started = time.monotonic()
        assert cc.confirm("Открыть файл?", r"C:\Users\me\отчёт.docx", "brain") is True
        assert time.monotonic() - started < 3
        # сервер сам закрывает молчащее соединение по таймауту рукопожатия
        with pytest.raises((EOFError, OSError)):
            for _ in range(3):
                assert silent.poll(5), "сервер не закрыл молчащее соединение"
                silent.recv_bytes()
    finally:
        silent.close()
    assert len(calls) == 1


def test_second_server_does_not_break_first(servers, monkeypatch: pytest.MonkeyPatch) -> None:
    calls1, cb1 = recorder(True)
    calls2, cb2 = recorder(False)
    first, second = servers(cb1), servers(cb2)
    assert first.address != second.address
    monkeypatch.setenv(cc.PIPE_ENV, second.address)
    assert cc.confirm("Второй?", "", "brain") is False
    second.close()
    monkeypatch.setenv(cc.PIPE_ENV, first.address)
    assert cc.confirm("Первый?", "", "brain") is True
    assert [c[0] for c in calls1] == ["Первый?"]
    assert [c[0] for c in calls2] == ["Второй?"]


def test_close_stops_listener_thread(servers, monkeypatch: pytest.MonkeyPatch) -> None:
    calls, callback = recorder(True)
    srv = servers(callback)
    thread = srv._thread
    assert thread is not None and thread.is_alive()
    srv.close()
    assert not thread.is_alive()
    srv.close()  # повторный close — не ошибка
    if sys.platform != "win32":
        assert not Path(srv.address).exists()
    monkeypatch.setenv(cc.PIPE_ENV, srv.address)
    assert cc.confirm("После close?", "", "brain") is False
    assert calls == []


def test_close_does_not_hang_if_listener_left_before_accept(monkeypatch: pytest.MonkeyPatch) -> None:
    def lazy_serve(self: cc.ConfirmServer, listener: object) -> None:
        self._closing.wait(10)  # вышел по флагу, так и не приняв «будильник»

    monkeypatch.setattr(cc.ConfirmServer, "_serve", lazy_serve)
    srv = cc.ConfirmServer(lambda s, d, c: True)
    srv.start()
    started = time.monotonic()
    srv.close()
    assert time.monotonic() - started < 3


def test_close_without_start() -> None:
    srv = cc.ConfirmServer(lambda s, d, c: True)
    srv.close()
    srv.start()  # после close сервер не оживает
    assert srv._thread is None


def test_in_process_handler_wins(servers, monkeypatch: pytest.MonkeyPatch) -> None:
    calls, callback = recorder(False)
    monkeypatch.setenv(cc.PIPE_ENV, servers(callback).address)
    cc.set_confirm_handler(lambda summary, details, caller: caller == "user")
    assert cc.confirm("Свернуть?", "", "user") is True
    assert cc.confirm("Свернуть?", "", "brain") is False
    assert calls == []


def test_load_key_created_once_and_never_overwritten() -> None:
    assert cc.load_key() is None
    assert not cc.key_path().exists()
    first = cc.load_key(create=True)
    assert first is not None and len(first) == cc.KEY_BYTES
    assert cc.load_key(create=True) == first
    assert cc.load_key() == first
    assert cc.key_path().read_bytes() == first
    assert [p.name for p in cc.key_path().parent.iterdir()] == ["pipe.key"]  # без хвостов .tmp


def test_damaged_key_is_not_replaced() -> None:
    cc.key_path().parent.mkdir(parents=True, exist_ok=True)
    cc.key_path().write_bytes(b"short")
    assert cc.load_key(create=True) is None
    assert cc.key_path().read_bytes() == b"short"


def test_server_without_key_fails_to_start() -> None:
    cc.key_path().parent.mkdir(parents=True, exist_ok=True)
    cc.key_path().write_bytes(b"short")
    srv = cc.ConfirmServer(lambda s, d, c: True)
    with pytest.raises(RuntimeError):
        srv.start()
    srv.close()


@pytest.mark.skipif(sys.platform != "win32", reason="named pipe AF_PIPE есть только в Windows")
def test_windows_pipe_address(servers) -> None:
    user = cc._user()
    assert cc.pipe_address() == rf"\\.\pipe\jarvis-confirm-{user}-{os.getpid()}"
    assert cc.pipe_address(pid=42) == rf"\\.\pipe\jarvis-confirm-{user}-42"
    srv = servers(lambda s, d, c: True)
    assert srv.address.startswith(cc.pipe_address())
    assert cc._valid_address(srv.address)
    assert connection.address_type(srv.address) == "AF_PIPE"
