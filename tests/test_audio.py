"""pc.audio: громкость через фейковый COM-поток."""

import threading
from collections.abc import Iterator

import pytest

from pc import audio
from pc.result import Caller


class FakeEndpoint:
    """IAudioEndpointVolume: методы pycaw, каждый вызов запоминает поток."""

    def __init__(self, owner: "FakeAudioApi") -> None:
        self.owner = owner

    def _touch(self) -> None:
        self.owner.threads.add(threading.get_ident())

    def GetMasterVolumeLevelScalar(self) -> float:
        self._touch()
        return self.owner.level

    def SetMasterVolumeLevelScalar(self, level: float, ctx: object) -> None:
        self._touch()
        assert ctx is None
        assert 0.0 <= level <= 1.0
        self.owner.level = level

    def GetMute(self) -> int:
        self._touch()
        return int(self.owner.muted)

    def SetMute(self, muted: int, ctx: object) -> None:
        self._touch()
        assert ctx is None
        self.owner.muted = bool(muted)


class FakeAudioApi:
    def __init__(self, level: float = 0.3, muted: bool = False) -> None:
        self.level = level
        self.muted = muted
        self.threads: set[int] = set()
        self.init_threads: list[int] = []
        self.done_threads: list[int] = []
        self.endpoints = 0
        self.error: Exception | None = None

    def thread_init(self) -> None:
        self.init_threads.append(threading.get_ident())

    def thread_done(self) -> None:
        self.done_threads.append(threading.get_ident())

    def endpoint(self) -> FakeEndpoint:
        self.threads.add(threading.get_ident())
        self.endpoints += 1
        if self.error:
            raise self.error
        return FakeEndpoint(self)


@pytest.fixture
def com(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeAudioApi]:
    audio._stop_thread()
    fake = FakeAudioApi()
    monkeypatch.setattr(audio, "_api", fake)
    yield fake
    audio._stop_thread()


@pytest.mark.parametrize("caller", ["user", "brain"])
def test_volume_get(com: FakeAudioApi, caller: Caller) -> None:
    com.level, com.muted = 0.3, False
    r = audio.volume_get(caller)
    assert r.ok
    assert r.data == {"level": 30, "muted": False}
    assert r.text == "Громкость 30 %"
    com.muted = True
    assert audio.volume_get(caller).data == {"level": 30, "muted": True}


def test_all_com_work_in_one_dedicated_thread(com: FakeAudioApi) -> None:
    audio.volume_get("user")
    audio.volume(set=50)
    audio.volume(delta=-10)
    audio.volume(mute=True)
    assert len(com.threads) == 1
    worker = next(iter(com.threads))
    assert worker != threading.get_ident()
    assert com.init_threads == [worker]
    audio._stop_thread()
    assert com.done_threads == [worker]


def test_new_endpoint_per_operation(com: FakeAudioApi) -> None:
    audio.volume_get("user")
    audio.volume(set=10)
    audio.volume(delta=5)
    assert com.endpoints == 3


def test_set_unmutes_and_clamps(com: FakeAudioApi) -> None:
    com.muted = True
    r = audio.volume(set=50)
    assert r.ok
    assert r.text == "Громкость 50 %"
    assert r.data == {"level": 50, "muted": False}
    assert audio.volume(set=150).data["level"] == 100
    assert audio.volume(set=-5).data["level"] == 0


def test_set_zero_keeps_mute(com: FakeAudioApi) -> None:
    com.muted = True
    assert audio.volume(set=0).data == {"level": 0, "muted": True}


def test_delta(com: FakeAudioApi) -> None:
    com.level = 0.95
    assert audio.volume(delta=10).data["level"] == 100
    com.level = 0.10
    assert audio.volume(delta=-20).data["level"] == 0
    com.level, com.muted = 0.30, True
    assert audio.volume(delta=-5).data == {"level": 25, "muted": True}
    assert audio.volume(delta=5).data == {"level": 30, "muted": False}


def test_mute_unmute(com: FakeAudioApi) -> None:
    r = audio.volume(mute=True)
    assert (r.ok, r.text, com.muted) == (True, "Звук выключен", True)
    r = audio.volume(mute=False)
    assert (r.ok, r.text, com.muted) == (True, "Звук включён", False)


@pytest.mark.parametrize(
    "kwargs",
    [{}, {"set": 10, "delta": 5}, {"set": 10, "mute": True}, {"set": True}, {"set": "громко"}, {"mute": 1}],
)
def test_volume_bad_args(com: FakeAudioApi, kwargs: dict) -> None:
    assert not audio.volume(**kwargs).ok
    assert com.endpoints == 0


def test_volume_string_number(com: FakeAudioApi) -> None:
    assert audio.volume(set="40").data["level"] == 40  # type: ignore[arg-type]
    assert audio.volume(delta="-10%").data["level"] == 30  # type: ignore[arg-type]


def test_no_device(com: FakeAudioApi) -> None:
    com.error = audio.NoAudioDevice()
    r = audio.volume_get("user")
    assert not r.ok
    assert r.text == "Нет устройства вывода звука"
    assert not audio.volume(set=10).ok


def test_com_error(com: FakeAudioApi) -> None:
    com.error = RuntimeError("COMError -2147023728")
    r = audio.volume(set=10)
    assert not r.ok
    assert "звук" in r.text.casefold()
    com.error = None
    assert audio.volume(set=10).ok  # поток жив после ошибки


def test_thread_init_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    audio._stop_thread()

    class Broken(FakeAudioApi):
        def thread_init(self) -> None:
            raise OSError("CoInitialize")

    monkeypatch.setattr(audio, "_api", Broken())
    assert not audio.volume_get("user").ok
    audio._stop_thread()


def test_volume_get_not_in_brain_budget(com: FakeAudioApi) -> None:
    for _ in range(10):
        assert audio.volume_get("brain").ok
    for _ in range(5):
        assert audio.volume(delta=1, caller="brain").ok
    assert not audio.volume(delta=1, caller="brain").ok  # шестое изменение — «спросить», обработчика нет
