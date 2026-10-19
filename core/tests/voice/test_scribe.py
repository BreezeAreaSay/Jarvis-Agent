"""Распознавание ElevenLabs Scribe v2 Realtime: протокол на поддельном сервере (локальный WebSocket), commit
на паузе, придержанный звук, ошибки и паузы сервиса, прокси (в том числе SOCKS5 от VPN)."""

import base64
import json
import socket
import threading
import time
from collections.abc import Iterator
from typing import Any

import numpy as np
import pytest

from jarvis import netproxy
from jarvis.context import AppContext
from jarvis.settings import Settings
from jarvis.voice import scribe as sc
from jarvis.voice.dsp import FRAME
from jarvis.voice.scribe import Scribe, ScribeError, ScribeStream, proxy_for, stream_url, tidy

KEY = "sk_" + "0" * 40


class FakeEleven:
    """Сервис распознавания: на commit — итог куска (texts по порядку); mode — сбой: 401, quota, throttle."""

    def __init__(self, *, texts: list[str] | None = None, mode: str = "") -> None:
        from websockets.sync.server import serve

        self.mode = mode
        self.texts = list(texts or [])
        self.requests: list[tuple[str, dict[str, str]]] = []
        self.segments: list[bytes] = []  # звук каждого куска (между commit)
        self.commits = 0
        self.connections = 0
        self._throttled = False
        self._server = serve(self._handler, "127.0.0.1", 0, process_request=self._process)
        self.port = self._server.socket.getsockname()[1]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def url(self, query: str = "model_id=scribe_v2_realtime") -> str:
        return f"ws://127.0.0.1:{self.port}/v1/speech-to-text/realtime?{query}"

    def close(self) -> None:
        self._server.shutdown()

    def _process(self, connection: Any, request: Any) -> Any:
        self.requests.append((request.path, {k.lower(): v for k, v in request.headers.raw_items()}))
        if self.mode == "401":
            body = json.dumps({"detail": {"status": "invalid_api_key", "message": "Invalid API key"}})
            return connection.respond(401, body)
        return None

    def _handler(self, ws: Any) -> None:
        self.connections += 1
        ws.send(json.dumps({"message_type": "session_started", "session_id": "s1", "config": {}}))
        segment = bytearray()
        for raw in ws:
            msg = json.loads(raw)
            assert msg["message_type"] == "input_audio_chunk" and msg["sample_rate"] == 16000
            segment += base64.b64decode(msg["audio_base_64"])
            if not msg["commit"]:
                ws.send(json.dumps({"message_type": "partial_transcript", "text": "Джарвис, откр"}))
                continue
            self.commits += 1
            if self.mode == "quota":
                ws.send(json.dumps({"message_type": "quota_exceeded", "error": "Quota exceeded"}))
                continue
            if self.mode == "throttle" and not self._throttled:
                self._throttled = True
                ws.send(json.dumps({"message_type": "commit_throttled", "error": "Commit too soon"}))
                continue
            if not segment:
                ws.send(json.dumps({"message_type": "insufficient_audio_activity", "error": "No speech"}))
                continue
            self.segments.append(bytes(segment))
            segment = bytearray()
            text = self.texts.pop(0) if self.texts else f"Кусок {len(self.segments)}."
            ws.send(json.dumps({"message_type": "committed_transcript", "text": text}))


@pytest.fixture
def eleven() -> Iterator[FakeEleven]:
    server = FakeEleven()
    yield server
    server.close()


def frames(n: int, amp: float = 0.25) -> list[np.ndarray]:
    return [np.full(FRAME, amp, dtype=np.float32) for _ in range(n)]


def stream(server: FakeEleven, **kwargs: Any) -> ScribeStream:
    return ScribeStream(server.url(), {"xi-api-key": KEY}, **kwargs)


def test_audio_streams_and_commit_returns_text(eleven: FakeEleven) -> None:
    s = stream(eleven)
    for f in frames(10):
        s.feed(f)
    assert s.finish(3.0) == "кусок 1"  # для команд — строчными, без точки в конце
    s.close()
    path, headers = eleven.requests[0]
    assert path.startswith("/v1/speech-to-text/realtime") and headers["xi-api-key"] == KEY
    assert eleven.commits == 1 and len(eleven.segments[0]) == 10 * FRAME * 2  # PCM 16 бит


def test_pause_commits_early_and_tail_silence_is_not_sent(eleven: FakeEleven) -> None:
    s = stream(eleven)
    for f in frames(10):
        s.feed(f)
    s.pause()
    deadline = time.monotonic() + 3.0
    while not s.partial().startswith("Кусок") and time.monotonic() < deadline:
        time.sleep(0.01)
    assert s.partial() == "Кусок 1"  # итог куска пришёл ещё до конца фразы
    for f in frames(5, amp=0.0):  # тишина после паузы — придерживается
        s.feed(f)
    assert s.finish(3.0) == "кусок 1"
    s.close()
    assert eleven.commits == 1 and len(eleven.segments) == 1
    assert len(eleven.segments[0]) == 10 * FRAME * 2


def test_speech_after_pause_sends_held_audio_and_joins_parts() -> None:
    server = FakeEleven(texts=["Джарвис, открой", "браузер."])
    try:
        s = stream(server)
        for f in frames(10):
            s.feed(f)
        s.pause()
        for f in frames(3, amp=0.01):  # придержано
            s.feed(f)
        s.resume()  # речь продолжилась — придержанное уходит
        for f in frames(5):
            s.feed(f)
        assert s.finish(3.0) == "джарвис, открой браузер"
        assert s.finish(3.0, punct=True) == "Джарвис, открой браузер"  # края — как у GigaAM с пунктуацией
        s.close()
        assert server.commits == 2 and len(server.segments[1]) == 8 * FRAME * 2
    finally:
        server.close()


def test_bad_key_is_reported_with_cooldown() -> None:
    server = FakeEleven(mode="401")
    errors: list[ScribeError] = []
    try:
        s = stream(server, on_error=errors.append)
        s.feed(frames(1)[0])
        assert s.finish(3.0) is None
        assert errors and "неверный ключ" in str(errors[0]) and errors[0].cooldown_s == sc.BLOCKED_COOLDOWN_S
    finally:
        server.close()


def test_quota_message_is_reported() -> None:
    server = FakeEleven(mode="quota")
    errors: list[ScribeError] = []
    try:
        s = stream(server, on_error=errors.append)
        s.feed(frames(1)[0])
        assert s.finish(3.0) is None and s.failed()
        assert "кредиты" in str(errors[0]) and errors[0].cooldown_s == sc.QUOTA_COOLDOWN_S
    finally:
        server.close()


def test_throttled_commit_is_retried() -> None:
    server = FakeEleven(mode="throttle")
    try:
        s = stream(server)
        for f in frames(4):
            s.feed(f)
        assert s.finish(3.0) == "кусок 1"
        assert server.commits == 2
    finally:
        server.close()


def test_no_network_returns_none_quickly() -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]  # порт закрыт: соединение отвергнуто сразу
    errors: list[ScribeError] = []
    s = ScribeStream(f"ws://127.0.0.1:{port}/", {}, on_error=errors.append)
    s.feed(frames(1)[0])
    started = time.monotonic()
    assert s.finish(3.0) is None
    assert time.monotonic() - started < 2.0
    assert "нет связи" in str(errors[0]) and errors[0].cooldown_s == sc.NET_COOLDOWN_S


def test_service_messages_map_to_human_errors() -> None:
    assert "права" in str(sc.message_error("auth_error", "Missing permission speech_to_text"))
    assert "условия" in str(sc.message_error("unaccepted_terms", "Accept terms"))
    assert sc.message_error("rate_limited", "").cooldown_s == sc.NET_COOLDOWN_S
    body = json.dumps({"detail": {"status": "detected_unusual_activity", "message": "Unusual activity"}})
    assert "VPN" in str(sc.handshake_error(401, body.encode()))
    assert "регион" in str(sc.handshake_error(403, b"Forbidden"))


def test_tidy_drops_event_tags() -> None:
    assert tidy("Джарвис, сверни все окна (смех).", punct=False) == "джарвис, сверни все окна"
    assert tidy("[музыка] Купить хлеба, молока.", punct=True) == "Купить хлеба, молока"


def test_stream_url_and_language() -> None:
    s = Settings()
    url = stream_url(s)
    assert url.startswith(sc.WS_URL + "?")
    for part in ("model_id=scribe_v2_realtime", "audio_format=pcm_16000", "commit_strategy=manual"):
        assert part in url
    assert "language_code=ru" in url
    s.voice.language = "auto"
    assert "language_code" not in stream_url(s)


def test_proxy_from_settings_then_system(monkeypatch: pytest.MonkeyPatch) -> None:
    s = Settings()
    monkeypatch.setattr(netproxy, "source", lambda: {})
    assert proxy_for(s) is None
    monkeypatch.setattr(netproxy, "source", lambda: {"https": "socks4://127.0.0.1:10808"})  # VPN в реестре
    assert proxy_for(s) == "socks5://127.0.0.1:10808"
    s.ai.proxy = "127.0.0.1:8080"
    assert proxy_for(s) == "http://127.0.0.1:8080"


class Socks5:
    """SOCKS5 без пароля (как у VPN-программ): CONNECT и пересылка в обе стороны."""

    def __init__(self) -> None:
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        self.port = self.sock.getsockname()[1]
        self.used = 0
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                client, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._client, args=(client,), daemon=True).start()

    def _client(self, client: socket.socket) -> None:
        def recv(n: int) -> bytes:
            data = b""
            while len(data) < n:
                part = client.recv(n - len(data))
                if not part:
                    raise OSError("closed")
                data += part
            return data

        with client:
            _ver, nmethods = recv(2)
            recv(nmethods)
            client.sendall(b"\x05\x00")
            _ver, _cmd, _rsv, atyp = recv(4)
            if atyp == 1:
                host = socket.inet_ntoa(recv(4))
            elif atyp == 3:
                host = recv(recv(1)[0]).decode()
            else:
                return
            port = int.from_bytes(recv(2), "big")
            upstream = socket.create_connection((host, port))
            self.used += 1
            client.sendall(b"\x05\x00\x00\x01" + socket.inet_aton("127.0.0.1") + (0).to_bytes(2, "big"))
            with upstream:
                done = threading.Event()

                def pipe(a: socket.socket, b: socket.socket) -> None:
                    try:
                        while data := a.recv(65536):
                            b.sendall(data)
                    except OSError:
                        pass
                    done.set()

                threading.Thread(target=pipe, args=(upstream, client), daemon=True).start()
                pipe(client, upstream)
                done.wait(1.0)

    def close(self) -> None:
        self.sock.close()


def test_works_through_socks5_proxy_of_vpn(eleven: FakeEleven) -> None:
    proxy = Socks5()
    try:
        s = stream(eleven, proxy=f"socks5://127.0.0.1:{proxy.port}")
        for f in frames(6):
            s.feed(f)
        assert s.finish(3.0) == "кусок 1"
        s.close()
        assert proxy.used == 1
    finally:
        proxy.close()


# --- Scribe: выбор, ключ, пауза после ошибки ---------------------------------------------------------------


def test_scribe_needs_selection_and_key(ctx: AppContext) -> None:
    scribe = Scribe(ctx)
    assert scribe.stream() is None and "ПК" in scribe.available()
    ctx.settings.patch({"voice": {"asr_model": "elevenlabs"}})
    assert scribe.stream() is None and "ключ" in scribe.available()


def test_scribe_cooldown_after_error_and_reset(ctx: AppContext, events: Any) -> None:
    ctx.settings.patch({"voice": {"asr_model": "elevenlabs"}})
    ctx.secrets.set("elevenlabs_api_key", KEY)
    seen: list[str] = []

    def refuse(url: str, **kwargs: Any) -> Any:
        seen.append(url)
        raise OSError("connection refused")

    scribe = Scribe(ctx, connect=refuse)
    s = scribe.stream()
    assert s is not None
    s.feed(frames(1)[0])
    assert s.finish(3.0) is None
    assert scribe.stream() is None and "нет связи" in scribe.available()  # полминуты не зовём
    toasts = [t for t in events.of("toast") if t["title"] == Scribe.title]
    assert len(toasts) == 1 and "на компьютере" in toasts[0]["message"]
    scribe.reset()
    assert scribe.available() == "" and scribe.stream() is not None
    assert seen and seen[0].startswith(sc.WS_URL)
