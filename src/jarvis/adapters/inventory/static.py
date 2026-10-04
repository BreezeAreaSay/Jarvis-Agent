"""Инвентарь из данных: для тестов, eval и набора фраз роутера — без чтения диска и реестра."""

from collections.abc import Mapping, Sequence

from jarvis.domain.inventory import AppEntry, KnownFolder


class StaticInventory:
    def __init__(
        self,
        apps: Sequence[AppEntry] = (),
        *,
        default_browser: str | None = None,
        folders: Mapping[KnownFolder, str] | None = None,
    ) -> None:
        self._apps = list(apps)
        self._browser = next((app for app in self._apps if app.id == default_browser), None)
        if default_browser is not None and self._browser is None:
            raise ValueError(f"браузер по умолчанию {default_browser!r} не найден среди приложений")
        self._folders = dict(folders or {})

    def apps(self) -> Sequence[AppEntry]:
        return self._apps

    def default_browser(self) -> AppEntry | None:
        return self._browser

    def known_folder(self, folder: KnownFolder) -> str | None:
        return self._folders.get(folder)
