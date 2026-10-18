"""«Блокировка, когда отходишь»: поддельная камера и часы — блокирует после таймаута, но не при человеке."""

from typing import Any

import pytest

from jarvis.context import AppContext
from jarvis.platform.fake import FakePlatform
from jarvis.plugins.autolock import AutoLock
from jarvis.services import UserFacingError
from tests.conftest import EventLog
from tests.plugins.conftest import enable, manager


class FakeDetector:
    def __init__(self) -> None:
        self.face: bool | None = True
        self.calls = 0
        self.closed = 0

    def detect(self) -> bool | None:
        self.calls += 1
        return self.face

    def close(self) -> None:
        self.closed += 1


class Clock:
    def __init__(self) -> None:
        self.now = 5000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def lock(pctx: AppContext, monkeypatch: pytest.MonkeyPatch) -> tuple[AutoLock, FakeDetector, Clock]:
    """Плагин без фонового цикла: проверки зовём сами (tick), камера и часы — поддельные."""
    plugin = manager(pctx).get("autolock")
    assert isinstance(plugin, AutoLock)
    detector = FakeDetector()
    clock = Clock()
    plugin.detector_factory = lambda config: detector
    plugin.clock = clock
    plugin.wall = lambda: 1_790_000_000.0 + clock.now
    pctx.settings.patch({"plugins": {"autolock": {"config": {"away_seconds": 20, "use_input": False}}}})
    monkeypatch.setattr(pctx.platform, "idle_seconds", lambda: 999.0)
    plugin._reset()
    return plugin, detector, clock


def locks(ctx: AppContext) -> int:
    platform = ctx.platform
    assert isinstance(platform, FakePlatform)
    return sum(1 for name, args in platform.calls if name == "power" and args[0] == "lock")


async def test_locks_after_timeout_once(lock: tuple[AutoLock, FakeDetector, Clock], pctx: AppContext) -> None:
    plugin, detector, clock = lock
    assert await plugin.tick() is False and plugin.status()["present"] is True
    detector.face = False
    clock.now += 10
    assert await plugin.tick() is False and locks(pctx) == 0
    clock.now += 11  # лица нет 21 с
    assert await plugin.tick() is True and locks(pctx) == 1
    status = plugin.status()
    assert status["present"] is False and status["armed"] is False and status["locked_at"] > 0
    clock.now += 60
    assert await plugin.tick() is False and locks(pctx) == 1  # пока не вернулся — повторно не блокируем
    detector.face = True
    clock.now += 5
    await plugin.tick()
    assert plugin.status()["armed"] is True and plugin.status()["present"] is True
    detector.face = False
    clock.now += 25
    assert await plugin.tick() is True and locks(pctx) == 2


async def test_no_lock_while_present(lock: tuple[AutoLock, FakeDetector, Clock], pctx: AppContext) -> None:
    plugin, detector, clock = lock
    for _ in range(40):
        clock.now += 5
        assert await plugin.tick() is False
    assert locks(pctx) == 0 and detector.calls == 40


async def test_fullscreen_blocks_unless_forced(
    lock: tuple[AutoLock, FakeDetector, Clock], pctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin, detector, clock = lock
    monkeypatch.setattr(pctx.platform, "is_fullscreen_app", lambda: True)
    detector.face = False
    clock.now += 30
    assert await plugin.tick() is False and locks(pctx) == 0
    pctx.settings.patch({"plugins": {"autolock": {"config": {"force": True}}}})
    assert await plugin.tick() is True and locks(pctx) == 1


async def test_camera_trouble_never_locks(
    lock: tuple[AutoLock, FakeDetector, Clock], pctx: AppContext
) -> None:
    plugin, detector, clock = lock
    detector.face = None
    clock.now += 60
    assert await plugin.tick() is False and locks(pctx) == 0
    assert plugin.status()["camera"] == "error" and "Камера" in plugin.error

    def broken(config: dict[str, Any]) -> FakeDetector:
        raise UserFacingError("нет OpenCV")

    plugin.detector_factory = broken
    plugin._detector = None
    plugin._camera_retry_at = 0.0
    assert await plugin.tick() is False and locks(pctx) == 0
    assert plugin.error == "Камера недоступна: нет OpenCV"


async def test_keyboard_and_mouse_mean_present(
    lock: tuple[AutoLock, FakeDetector, Clock], pctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin, detector, clock = lock
    pctx.settings.patch({"plugins": {"autolock": {"config": {"use_input": True}}}})
    idle = [3.0]
    monkeypatch.setattr(pctx.platform, "idle_seconds", lambda: idle[0])
    detector.face = False  # лицо не видно (темно), но человек печатает
    clock.now += 60
    assert await plugin.tick() is False and locks(pctx) == 0
    idle[0] = 18.0  # последнее нажатие было 18 с назад — ещё рано
    clock.now += 15
    assert await plugin.tick() is False and locks(pctx) == 0
    idle[0] = 28.0  # ушёл: ни лица, ни нажатий уже 28 с
    clock.now += 10
    assert await plugin.tick() is True and locks(pctx) == 1


async def test_idle_mode_uses_input_only(
    lock: tuple[AutoLock, FakeDetector, Clock], pctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin, detector, clock = lock
    pctx.settings.patch({"plugins": {"autolock": {"config": {"mode": "idle"}}}})
    idle = [5.0]
    monkeypatch.setattr(pctx.platform, "idle_seconds", lambda: idle[0])
    assert await plugin.tick() is False
    idle[0] = 25.0
    clock.now += 20
    assert await plugin.tick() is True and locks(pctx) == 1
    assert detector.calls == 0  # камера в этом режиме не включается


async def test_pause_and_resume(lock: tuple[AutoLock, FakeDetector, Clock], pctx: AppContext) -> None:
    plugin, detector, clock = lock
    result = await plugin.handle("pause", {"minutes": 30})
    assert result["paused_until"] == plugin.wall() + 1800
    detector.face = False
    clock.now += 600
    assert await plugin.tick() is False and locks(pctx) == 0 and detector.calls == 0
    await plugin.handle("resume", {})
    clock.now += 21
    assert await plugin.tick() is True and locks(pctx) == 1


async def test_check_action_and_state_events(
    lock: tuple[AutoLock, FakeDetector, Clock], pctx: AppContext, pevents: EventLog
) -> None:
    plugin, detector, clock = lock
    await enable(pctx, "autolock", config={"away_seconds": 20})
    assert await manager(pctx).action("autolock", "check", {}) == {"ok": True, "present": True}
    detector.face = None
    with pytest.raises(UserFacingError, match="Камера"):
        await manager(pctx).action("autolock", "check", {})
    detector.face = False
    clock.now += 30
    await plugin.tick()
    states = pevents.of("plugin.autolock.state")
    assert states and states[-1]["locked"] is True and states[-1]["present"] is False
