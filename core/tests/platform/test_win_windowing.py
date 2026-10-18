"""Окна: «во весь экран», окна Jarvis, выбор окна по имени (чистый код — на Linux)."""

import pytest

from jarvis.platform.base import WindowInfo
from jarvis.platform.windows import windowing as w

MONITOR = (0, 0, 1920, 1080)
SECOND = (1920, 0, 4480, 1440)  # второй монитор 2560×1440 справа


@pytest.mark.parametrize(
    ("win", "monitor", "maximized_with_caption", "expected"),
    [
        ((0, 0, 1920, 1080), MONITOR, False, True),  # игра без рамки
        ((-8, -8, 1928, 1088), MONITOR, False, True),  # окно чуть больше монитора
        ((0, 0, 1920, 1040), MONITOR, False, False),  # развёрнутое: панель задач видна
        ((-8, -8, 1928, 1088), MONITOR, True, False),  # развёрнутое с заголовком при автоскрытии панели
        ((100, 100, 900, 700), MONITOR, False, False),
        ((1920, 0, 4480, 1440), SECOND, False, True),  # видео во весь экран на втором мониторе
        ((0, 0, 1920, 1080), SECOND, False, False),
        (None, MONITOR, False, False),
        ((0, 0, 1920, 1080), None, False, False),
        ((0, 0, 1920, 1080), (0, 0, 0, 0), False, False),
    ],
)
def test_fullscreen_rect(
    win: w.Rect | None, monitor: w.Rect | None, maximized_with_caption: bool, expected: bool
) -> None:
    assert w.fullscreen_rect(win, monitor, maximized_with_caption) is expected


def test_is_jarvis_window() -> None:
    own = frozenset({100, 200})
    assert w.is_jarvis_window("Что угодно", "electron.exe", 100, own)
    assert w.is_jarvis_window("Jarvis", "Jarvis.exe", 999, own)
    assert w.is_jarvis_window("Jarvis — оверлей", "electron.exe", 999, own)
    assert not w.is_jarvis_window("Jarvis — Google Chrome", "chrome.exe", 999, own)  # вкладка браузера
    assert not w.is_jarvis_window("Документ — Word", "WINWORD.EXE", 999, own)


@pytest.mark.parametrize(
    ("title", "name", "expected"),
    [
        ("Документ1 - Word", "Word", True),
        ("Калькулятор", "калькулятор", True),
        ("Безымянный — Блокнот", "Блокнот", True),
        ("Telegram Web — Google Chrome", "Telegram", False),
        ("Wordle — Google Chrome", "Word", False),
        ("", "Word", False),
        ("Word", "", False),
    ],
)
def test_title_matches_app(title: str, name: str, expected: bool) -> None:
    assert w.title_matches_app(title, name) is expected


WINDOWS = [
    WindowInfo(11, "Jarvis docs — Google Chrome", "chrome.exe", 1),
    WindowInfo(12, "Безымянный — Блокнот", "notepad.exe", 2),
    WindowInfo(13, "Spotify Premium", "Spotify.exe", 3),
    WindowInfo(14, "Отчёт за квартал.docx - Word", "WINWORD.EXE", 4),
    WindowInfo(15, "Калькулятор", "CalculatorApp.exe", 5),
    WindowInfo(16, "YouTube — Google Chrome", "chrome.exe", 6),
]


@pytest.mark.parametrize(
    ("target", "exes", "hwnd"),
    [
        ("chrome", {"chrome.exe"}, 11),  # верхнее окно программы
        ("spotify", {"spotify.exe"}, 13),
        ("Калькулятор", set(), 15),  # точный заголовок
        ("Блокнот", set(), 12),  # заголовок программы: «… — Блокнот»
        ("YouTube", set(), 16),  # слово заголовка
        ("отчет за квартал", set(), 14),  # ё/е и регистр не важны
        ("отчт за квартал", set(), 14),  # похожий заголовок
        ("Paint", {"mspaint.exe"}, None),
        ("", set(), None),
        ("ab", set(), None),  # слишком коротко для поиска по заголовку
    ],
)
def test_pick_window(target: str, exes: set[str], hwnd: int | None) -> None:
    found = w.pick_window(target, WINDOWS, exes)
    assert (found.hwnd if found else None) == hwnd


def test_clean_text_replaces_lone_surrogates() -> None:
    assert w.clean_text("ok\ud800x") == "ok�x"
    assert w.clean_text("Привет 😀") == "Привет 😀"
