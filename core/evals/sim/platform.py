"""Платформа имитатора: то же, что jarvis.platform.fake (вызовы пишутся в calls), но окна, ввод, экран,
запуск программ, адреса, звук и музыка — из мира (evals.sim.world), а не заглушки.

Подключается как фейки в тестах: ctx.platform = SimPlatform(world), ctx.agent.ui = SimUi(world).
"""

from typing import Any

from evals.sim.render import Renderer
from evals.sim.world import SCREEN, World
from jarvis.ai.policy import normalize_combo
from jarvis.platform.base import AppInfo, MediaInfo, MediaKey, MouseButton, WindowAction, WindowInfo
from jarvis.platform.fake import FakePlatform


class SimPlatform(FakePlatform):
    name = "sim"

    def __init__(self, world: World) -> None:
        super().__init__()
        self.world = world
        self.render = Renderer(world)

    # окна FakePlatform (self.windows) больше не нужны: всё из мира
    def list_windows(self) -> list[WindowInfo]:
        world = self.world
        with world.lock:
            world.tick()
            front = world.foreground
            return [w.info(w is front) for w in reversed(world.windows)]

    def foreground_window(self) -> WindowInfo | None:
        with self.world.lock:
            self.world.tick()
            front = self.world.foreground
            return front.info(True) if front is not None else None

    def window_action(self, action: WindowAction, target: int | str | None = None) -> None:
        self._call("window_action", action, target)
        world = self.world
        with world.lock:
            world.tick()
            if action == "minimize_all":
                for window in world.windows:
                    window.minimized = True
                world.touch()
                return
            window = world.find_window(target)
            if window is None:
                return
            if action in ("focus", "restore", "maximize"):
                world.front(window)
            elif action == "minimize":
                window.minimized = True
                world.touch()
            elif action == "close":
                world.close(window)
            world.record("window", f"{action} «{window.title}»", window=window)

    def is_running(self, name: str) -> bool:
        want = name.casefold()
        return any(want in w.exe.casefold() or want in w.app.name.casefold() for w in self.world.windows)

    def close_app(self, name: str) -> int:
        self._call("close_app", name)
        with self.world.lock:
            want = name.casefold().removesuffix(".exe")
            closed = [
                w
                for w in self.world.windows
                if want and (want in w.exe.casefold() or want in w.app.name.casefold())
            ]
            for window in closed:
                self.world.close(window, force=True)
            return len(closed)

    # --- ввод ----------------------------------------------------------------------------------------
    def press_keys(self, combo: str) -> None:
        self._call("press_keys", combo)
        with self.world.lock:
            self.world.tick()
            self.world.press(normalize_combo(combo) or combo.casefold())

    def type_text(self, text: str) -> None:
        self._call("type_text", text)
        with self.world.lock:
            self.world.tick()
            self.world.type_text(text, "type")

    def paste_text(self, text: str) -> None:
        self._call("paste_text", text)
        with self.world.lock:
            self.world.tick()
            self.world.type_text(text, "paste")

    def mouse_move(self, x: int, y: int) -> None:
        super().mouse_move(x, y)
        self.world.cursor = (x, y)

    def mouse_click(
        self, x: int | None = None, y: int | None = None, button: MouseButton = "left", double: bool = False
    ) -> None:
        super().mouse_click(x, y, button, double)
        px, py = (x, y) if x is not None and y is not None else self.world.cursor
        with self.world.lock:
            self.world.tick()
            self.world.click(int(px), int(py), button, double)

    def mouse_scroll(self, amount: int) -> None:
        self._call("mouse_scroll", amount)
        with self.world.lock:
            window = self.world.window_at(*self.world.cursor)
            self.world.record("scroll", str(amount), window=window)
            scroll = getattr(window.app, "scroll", None) if window is not None else None
            if callable(scroll):
                scroll(window, amount)
                self.world.touch()

    def cursor_pos(self) -> tuple[int, int]:
        return self.world.cursor

    # --- экран ---------------------------------------------------------------------------------------
    def screen_size(self) -> tuple[int, int]:
        return SCREEN[2], SCREEN[3]

    def virtual_screen(self) -> tuple[int, int, int, int]:
        return 0, 0, SCREEN[2], SCREEN[3]

    def monitor_rect(self, hwnd: int) -> tuple[int, int, int, int] | None:
        return SCREEN

    def monitors(self) -> list[tuple[int, int, int, int]]:
        return [SCREEN]

    def screen_pixels(self, rect: tuple[int, int, int, int] | None = None) -> tuple[bytes, int, int]:
        self._call("screen_pixels", rect)
        return self.render.pixels(rect)

    def screenshot(self, window: int | None = None) -> bytes:
        self._call("screenshot", window)
        with self.world.lock:
            target = self.world.window(window) if window else None
            return self.render.png(target)

    # --- программы и адреса ---------------------------------------------------------------------------
    def list_apps(self, refresh: bool = False) -> list[AppInfo]:
        return self.world.installed()

    def launch(self, target: str, args: str = "") -> None:
        self._call("launch", target, args)
        if not target:
            from jarvis.platform.base import PlatformError

            raise PlatformError("Не указано, что запустить")
        with self.world.lock:
            self.world.tick()
            self.world.launch(target)

    def open_url(self, url: str) -> None:
        self._call("open_url", url)
        with self.world.lock:
            self.world.tick()
            self.world.open_url(url)

    def open_path(self, path: str) -> None:
        self._call("open_path", path)
        with self.world.lock:
            self.world.open_path(path)

    def reveal_path(self, path: str) -> None:
        super().reveal_path(path)
        with self.world.lock:
            self.world.launch("explorer.exe")
            explorer = self.world.foreground
            go = getattr(explorer.app, "go", None) if explorer is not None else None
            if callable(go):
                go(explorer, path.rsplit("\\", 1)[0])

    def power(self, action: Any, delay_s: int = 0) -> None:
        super().power(action, delay_s)
        self.world.record("effect", f"питание: {action}", risky=True, action="power")

    # --- звук и музыка --------------------------------------------------------------------------------
    def volume_get(self) -> int:
        return self.world.volume

    def volume_set(self, level: int) -> int:
        self._call("volume_set", level)
        with self.world.lock:
            self.world.volume = max(0, min(100, int(level)))
            self.world.record("effect", f"громкость {self.world.volume}", action="volume")
            return self.world.volume

    def volume_change(self, delta: int) -> int:
        return self.volume_set(self.world.volume + delta)

    def mute_set(self, muted: bool | None = None) -> bool:
        self._call("mute_set", muted)
        with self.world.lock:
            self.world.muted = (not self.world.muted) if muted is None else muted
            self.world.record(
                "effect", "звук выключен" if self.world.muted else "звук включён", action="mute"
            )
            return self.world.muted

    def is_muted(self) -> bool:
        return self.world.muted

    def _player(self) -> Any:
        """Музыкальная программа с открытым окном (последняя впереди)."""
        for window in reversed(self.world.windows):
            if callable(getattr(window.app, "media", None)):
                return window.app
        return None

    def media_key(self, key: MediaKey) -> None:
        self._call("media_key", key)
        with self.world.lock:
            player = self._player()
            self.world.record("key", f"медиа: {key}", app=player)
            if player is not None:
                player.media(key)

    def now_playing(self) -> MediaInfo | None:
        with self.world.lock:
            player = self._player()
            return player.now_playing() if player is not None else None
