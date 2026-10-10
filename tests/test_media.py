"""pc.media: медиаклавиши через _input.send_key."""

import pytest

from pc import _input, media
from pc.result import Caller


class Keys:
    def __init__(self, result: bool = True) -> None:
        self.sent: list[tuple[int, bool]] = []
        self.result = result

    def __call__(self, vk: int, up: bool = False) -> bool:
        self.sent.append((vk, up))
        return self.result


@pytest.fixture
def keys(monkeypatch: pytest.MonkeyPatch) -> Keys:
    k = Keys()
    monkeypatch.setattr(_input, "send_key", k)
    return k


@pytest.mark.parametrize(
    ("action", "vk", "text"),
    [
        ("play_pause", 0xB3, "Пауза/продолжаю"),
        ("next", 0xB0, "Следующий трек"),
        ("prev", 0xB1, "Предыдущий трек"),
    ],
)
@pytest.mark.parametrize("caller", ["user", "brain"])
def test_media_keys(keys: Keys, action: str, vk: int, text: str, caller: Caller) -> None:
    r = media.media(action, caller)  # type: ignore[arg-type]
    assert r.ok
    assert r.text == text
    assert keys.sent == [(vk, False), (vk, True)]


def test_media_unknown_action(keys: Keys) -> None:
    assert not media.media("stop", "user").ok  # type: ignore[arg-type]
    assert keys.sent == []


def test_media_send_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    k = Keys(result=False)
    monkeypatch.setattr(_input, "send_key", k)
    r = media.media("next", "user")
    assert not r.ok
    assert len(k.sent) == 2  # отпускание отправляется, даже если нажатие не прошло


def test_media_brain_budget(keys: Keys) -> None:
    for _ in range(5):
        assert media.media("next", "brain").ok
    # шестое изменяющее действие мозга за минуту — «спросить»; обработчика нет — отказ
    assert not media.media("next", "brain").ok
    assert media.media("next", "user").ok
