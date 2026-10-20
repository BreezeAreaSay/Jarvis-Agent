"""Музыкальная программа (Spotify, Яндекс Музыка): главная с обложками плейлистов и плеер внизу.

Как у Яндекс Музыки с «Моей волной»: клавиша «Play» (медиаклавиша, кнопка плеера) без выбранного трека ничего
не включает — нужно щёлкнуть плейлист на главной. Адреса spotify: выводят окно Spotify вперёд.
cards_uia=False — обложки только нарисованы (UI Automation их не видит, нужны картинка и click_point); кнопки
плеера и название трека видны всегда. Сеанс воспроизведения Windows (now_playing) — то, что играет.
"""

from evals.sim.world import App, Widget, Window, World
from jarvis.platform.base import MediaInfo

Playlists = dict[str, list[tuple[str, str]]]
PLAYLISTS: Playlists = {
    "Мой микс дня": [("Night Drive", "Neon Coast"), ("Slow Lights", "Aurora Lane")],
    "Для бега": [("Run", "Pulse Theory"), ("Faster", "Kilometer")],
    "Спокойная музыка": [("Rain Garden", "Mila Sato"), ("Low Tide", "Harbor")],
    "Хиты недели": [("Summer Again", "Vela"), ("Echoes", "The Ninth")],
}
COLORS = ((29, 185, 84), (232, 93, 4), (80, 120, 200), (214, 51, 132))


class Music(App):
    background = (18, 18, 22)

    def __init__(
        self,
        world: World,
        *,
        name: str = "Spotify",
        exe: str = "Spotify.exe",
        title: str = "Spotify Premium",
        cards_uia: bool = True,
        playlists: Playlists | None = None,
    ) -> None:
        super().__init__(world)
        self.name, self.exe, self.title = name, exe, title
        self.cards_uia = cards_uia
        self.playlists = dict(playlists or PLAYLISTS)
        self.playlist: str | None = None
        self.track = 0
        self.playing = False

    # --- плеер ---------------------------------------------------------------------------------------
    @property
    def current(self) -> tuple[str, str] | None:
        if self.playlist is None:
            return None
        tracks = self.playlists[self.playlist]
        return tracks[self.track % len(tracks)]

    def play(self, playlist: str) -> None:
        self.playlist, self.track, self.playing = playlist, 0, True
        self.world.record(
            "effect", f"{self.name}: играет «{playlist}»", app=self, action="play", playlist=playlist
        )

    def toggle(self) -> None:
        if self.current is None:
            self.world.record(
                "effect", f"{self.name}: нечего играть — трек не выбран", app=self, action="noop"
            )
            return
        self.playing = not self.playing
        self.world.record(
            "effect", f"{self.name}: {'играет' if self.playing else 'пауза'}", app=self, action="toggle"
        )

    def url(self, url: str) -> bool:
        if not (url.casefold().startswith("spotify:") and self.exe.casefold() == "spotify.exe"):
            return False
        window = self.main() or self.open_main()
        self.world.front(window)
        self.world.record("effect", f"{self.name}: адрес {url}", window=window, action="url", url=url)
        return True

    def media(self, key: str) -> None:
        if key == "play_pause":
            self.toggle()
        elif key in ("next", "prev") and self.current is not None:
            self.track += 1 if key == "next" else -1
        elif key == "stop":
            self.playing = False

    def now_playing(self) -> MediaInfo | None:
        if not self.windows():
            return None
        track = self.current
        if track is None:
            return MediaInfo(app=self.name, status="stopped")
        return MediaInfo(
            track[0],
            track[1],
            self.playlist or "",
            self.name,
            "playing" if self.playing else "paused",
            12.0,
            214.0,
        )

    # --- вид -------------------------------------------------------------------------------------------
    def view(self, window: Window) -> list[Widget]:
        width, height = window.size
        items = [
            Widget("nav", "Pane", rect=(0, 32, 220, height - 90), color=(0, 0, 0)),
            Widget("home", "ListItem", "Главная", (12, 50, 208, 86), style="selected"),
            Widget("find", "ListItem", "Поиск", (12, 92, 208, 128)),
            Widget("library", "ListItem", "Моя медиатека", (12, 134, 208, 170)),
            Widget("hello", "Text", "Добрый день", (250, 56, 800, 90), size=26, style="header"),
            Widget("made", "Text", "Подборки для вас", (250, 104, 800, 126), size=15, style="muted"),
        ]
        for i, playlist in enumerate(self.playlists):
            left = 250 + i * 250
            rect = (left, 140, left + 220, 420)
            items.append(
                Widget(
                    f"card:{playlist}",
                    "Image",
                    playlist,
                    rect,
                    uia=False,
                    style="card",
                    icon="play",
                    color=COLORS[i % len(COLORS)],
                    size=15,
                    on_click=lambda p=playlist: self.play(p),
                    on_double=lambda p=playlist: self.play(p),
                )
            )
            if self.cards_uia:
                items.append(
                    Widget(
                        f"play:{playlist}",
                        "Button",
                        f"Воспроизвести «{playlist}»",
                        (left + 160, 320, left + 210, 370),
                        icon="play",
                        style="primary",
                        color=(29, 185, 84),
                        label="",
                        on_click=lambda p=playlist: self.play(p),
                    )
                )
        track = self.current
        playing = self.playing and track is not None
        status = f"{track[0]} — {track[1]}" if track else "Ничего не играет — выберите, что слушать"
        items += [
            Widget("bar", "Pane", rect=(0, height - 90, width, height), color=(28, 28, 32)),
            Widget("now", "Text", status, (24, height - 62, 560, height - 40), size=15),
            Widget(
                "prev",
                "Button",
                "Предыдущий",
                (width // 2 - 110, height - 70, width // 2 - 64, height - 24),
                icon="prev",
                label="",
                style="flat",
                on_click=lambda: self.media("prev"),
            ),
            Widget(
                "play",
                "Button",
                "Пауза" if playing else "Воспроизвести",
                (width // 2 - 24, height - 72, width // 2 + 24, height - 22),
                icon="pause" if playing else "play",
                label="",
                on_click=self.toggle,
            ),
            Widget(
                "next",
                "Button",
                "Следующий",
                (width // 2 + 64, height - 70, width // 2 + 110, height - 24),
                icon="next",
                label="",
                style="flat",
                on_click=lambda: self.media("next"),
            ),
        ]
        return items
