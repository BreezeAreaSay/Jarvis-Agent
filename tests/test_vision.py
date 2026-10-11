"""jarvis.vision: строгое распознавание «посмотри на экран», вопрос без служебных слов, строка «⚙ снимок …»,
JPEG из снимка (QImage без QApplication), data URL."""

import base64
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

import pytest

from jarvis import vision
from pc.screen import Shot
from pc.windows import WindowInfo

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
D = vision.DEFAULT_QUESTION

# (фраза, цель, вопрос): None — вся фраза как сказана, "" — вопрос по умолчанию
POSITIVE: list[tuple[str, str, str | None]] = [
    ("посмотри на экран", "screen", ""),
    ("глянь на экран и скажи, что не так", "screen", "скажи, что не так"),
    ("взгляни на окно", "window", ""),
    ("экран: что тут не так?", "screen", "что тут не так?"),
    ("окно: переведи", "window", "переведи"),
    ("Экран: Что За Ошибка?", "screen", "Что За Ошибка?"),
    ("окно:", "window", ""),
    ("скриншот: что тут", "screen", "что тут"),
    ("что у меня на экране", "screen", None),
    ("а что у меня на экране?", "screen", None),
    ("что в этом окне?", "window", None),
    ("что не так на экране?", "screen", None),
    ("что ты видишь на экране", "screen", None),
    ("что за ошибка на экране", "screen", None),
    ("какая ошибка на экране", "screen", None),
    ("прочитай ошибку в окне", "window", None),
    ("прочитай мне что написано в окне", "window", None),
    ("переведи текст на экране", "screen", None),
    ("переведи текст на экране на английский", "screen", None),
    ("переведи на английский текст на экране", "screen", None),
    ("переведи с экрана", "screen", None),
    ("опиши, что на экране", "screen", None),
    ("опиши экран", "screen", None),
    ("опиши окно", "window", None),
    ("объясни ошибку в окне", "window", None),
    ("сделай скриншот и опиши, что видишь", "screen", "опиши, что видишь"),
    ("сделай скриншот экрана и скажи что там", "screen", "скажи что там"),
    ("сделай скрин окна и переведи", "window", "переведи"),
    ("посмотри что у меня в окне", "window", None),
    ("можешь посмотреть на экран?", "screen", ""),
    ("посмотри на экран что тут не так", "screen", "что тут не так"),
    ("посмотри на экран и найди ошибку", "screen", "найди ошибку"),
    ("посмотри, пожалуйста, на экран", "screen", ""),
    ("посмотри на экран, пожалуйста", "screen", ""),
    ("джарвис, посмотри на экран", "screen", ""),
    ("посмотри в это окно", "window", ""),
    ("глянь окно", "window", ""),
    ("на экране ошибка, что делать?", "screen", None),
    ("в этом окне какое-то сообщение", "window", None),
    ("ты видишь мой экран?", "screen", None),
    ("видишь ошибку на экране?", "screen", None),
    ("помоги с ошибкой на экране", "screen", None),
]

# похожие команды, которые должны дойти до грамматики, рук или мозга без снимка
NEGATIVE = [
    "открой экран блокировки",
    "сверни окно",
    "сверни это окно",
    "закрой это окно",
    "закрой окно",
    "закрой окно хрома",
    "разверни на весь экран",
    "разверни окно на весь экран",
    "заблокируй экран",
    "найди скриншот",
    "найди скриншоты",
    "открой папку скриншоты",
    "выключи экран",
    "включи экран",
    "яркость экрана",
    "сделай яркость экрана поменьше",
    "убавь яркость экрана",
    "окно браузера открой",
    "как сделать скриншот части экрана",
    "как сделать скриншот",
    "переключись на окно с почтой",
    "убери это окно вниз",
    "сделай окно поменьше",
    "восстанови окно",
    "сверни все окна",
    "верни все окна",
    "покажи окно хрома",
    "сделай скриншот",
    "сделай скрин",
    "сделай снимок экрана",
    "почему мерцает экран",
    "как поменять разрешение экрана",
    "экран погас что делать",
    "открой окно",
    "переведи окно в полноэкранный режим",
    "объясни как настроить экран",
    "напиши письмо про ошибку на экране входа",
    "посмотри в окно",
    "посмотри в окно браузера",
    "что на экране блокировки",
    "что в окне браузера",
    "посмотри на экран телефона",
    "опиши экран блокировки windows",
    "переведи слово экран на английский",
    "что такое ошибка на экране смерти",
    "снимок экрана",
    "запиши экран",
    "покажи экран",
    "экранизация книги",
    "оконные рамы",
    "окно",
    "экран",
    "как мне настроить два монитора",
    "подключи монитор",
    "сделай экран ярче",
    "переведи экран в спящий режим",
    "где хранятся скриншоты",
]


@pytest.mark.parametrize(("text", "target", "question"), POSITIVE, ids=[p[0] for p in POSITIVE])
def test_match_positive(text: str, target: str, question: str | None) -> None:
    m = vision.match(text)
    assert m is not None, text
    want = D if question == "" else (" ".join(text.split()) if question is None else question)
    assert (m.target, m.question) == (target, want)


@pytest.mark.parametrize("text", NEGATIVE)
def test_match_negative(text: str) -> None:
    assert vision.match(text) is None


def test_empty_and_whitespace() -> None:
    assert vision.match("") is None
    assert vision.match("   ") is None


def test_question_keeps_case_and_drops_please() -> None:
    m = vision.match("Посмотри на экран и Переведи «Hello World»")
    assert m is not None and m.question == "Переведи «Hello World»"
    m = vision.match("ЭКРАН: пожалуйста")
    assert m is not None and m.question == D
    m = vision.match("посмотри на   экран ,  что   тут")
    assert m is not None and m.question == "что тут"


def test_cheap_precheck_skips_regexes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Без «экран»/«окн»/«скрин»/«монитор» регулярные выражения не запускаются вовсе."""

    class Boom:
        def fullmatch(self, text: str) -> None:
            raise AssertionError(f"регулярка запущена для «{text}»")

    monkeypatch.setattr(vision, "_COMPILED", [(Boom(), "text")])
    for text in ("открой телегу", "громкость 30", "почему тормозит комп", "который час"):
        assert vision.match(text) is None


def test_match_is_fast() -> None:
    """Обычная команда — доли микросекунды; фраза с «экран»/«окно» — микросекунды (бюджет грамматики
    300 мс)."""
    plain = ["открой телегу", "громкость 30", "пауза", "почему тормозит комп", "найди отчёт за март"] * 20
    hinted = [t for t, _, _ in POSITIVE] + NEGATIVE
    for t in plain + hinted:
        vision.match(t)
    limit = 3 if os.environ.get("CI") else 1

    def median_us(texts: list[str]) -> float:
        times = []
        for t in texts:
            t0 = time.perf_counter()
            vision.match(t)
            times.append((time.perf_counter() - t0) * 1e6)
        return statistics.median(times)

    assert median_us(plain) <= 10 * limit
    assert median_us(hinted) <= 100 * limit


# --- строка состояния ------------------------------------------------------------------------------


def test_status_label_screen_and_window() -> None:
    assert vision.status_label("screen") == "⚙ снимок экрана → GPT"
    win = WindowInfo(5, "отчёт.docx - Word", 7, "WINWORD.EXE")
    assert vision.status_label("window", win) == "⚙ снимок окна «отчёт.docx - Word» → GPT"
    assert (
        vision.status_label("window", WindowInfo(5, "", 7, "notepad.exe"))
        == "⚙ снимок окна «notepad.exe» → GPT"
    )
    assert vision.status_label("window", None) == "⚙ снимок окна → GPT"


def test_status_label_untrusted_title() -> None:
    """Заголовок — недоверенный текст: одной строкой, без управляющих символов и непарных суррогатов,
    обрезан."""
    title = "строка\nвторая\x00‮" + "я" * 100 + "\ud800"
    label = vision.status_label("window", WindowInfo(5, title, 7, "x.exe"))
    inner = label.removeprefix("⚙ снимок окна «").removesuffix("» → GPT")
    assert "\n" not in inner and "\x00" not in inner and "\ud800" not in inner and "\u202e" not in inner
    assert inner.startswith("строка вторая") and len(inner) <= vision.TITLE_MAX and inner.endswith("…")
    label.encode("utf-8")  # без исключения


# --- JPEG ---------------------------------------------------------------------------------------------


def solid(width: int, height: int, bgr: tuple[int, int, int] = (30, 120, 220), alpha: int = 0) -> Shot:
    return Shot(width, height, bytes([*bgr, alpha]) * (width * height))


def decode(jpeg: bytes) -> object:
    from PySide6.QtGui import QImage

    image = QImage.fromData(jpeg, "JPEG")
    assert not image.isNull()
    return image


@pytest.mark.parametrize(
    ("size", "max_side", "expected"),
    [
        ((3200, 1800), 1600, (1600, 900)),
        ((2560, 1440), 1600, (1600, 900)),
        ((1000, 3000), 1200, (400, 1200)),
        ((800, 600), 1600, (800, 600)),  # маленький снимок не увеличивается
        ((1600, 900), 1600, (1600, 900)),
    ],
)
def test_encode_jpeg_scales_long_side(
    size: tuple[int, int], max_side: int, expected: tuple[int, int]
) -> None:
    jpeg = vision.encode_jpeg(solid(*size), max_side=max_side, quality=80)
    assert jpeg[:2] == b"\xff\xd8" and jpeg[-2:] == b"\xff\xd9"
    image = decode(jpeg)
    assert (image.width(), image.height()) == expected  # type: ignore[attr-defined]


def test_encode_jpeg_ignores_gdi_alpha() -> None:
    """Альфа GDI в DIB-секции — 0: цвета после сглаживания должны остаться, а не стать чёрными."""
    image = decode(vision.encode_jpeg(solid(3200, 1800, (30, 120, 220), alpha=0), 1600, 90))
    c = image.pixelColor(800, 450)  # type: ignore[attr-defined]
    assert abs(c.red() - 220) <= 6 and abs(c.green() - 120) <= 6 and abs(c.blue() - 30) <= 6


def test_encode_jpeg_quality_changes_size() -> None:
    noisy = Shot(400, 300, bytes((i * 37 + (i // 4) * 11) % 251 for i in range(400 * 300 * 4)))
    assert len(vision.encode_jpeg(noisy, quality=50)) < len(vision.encode_jpeg(noisy, quality=95))


@pytest.mark.parametrize("shot", [Shot(0, 10, b""), Shot(10, 10, b"\x00" * 399), Shot(-1, 5, b"\x00" * 20)])
def test_encode_jpeg_rejects_broken_shot(shot: Shot) -> None:
    with pytest.raises(ValueError):
        vision.encode_jpeg(shot)


def test_data_url() -> None:
    url = vision.data_url(b"\xff\xd8abc\xff\xd9")
    assert url.startswith("data:image/jpeg;base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == b"\xff\xd8abc\xff\xd9"


def test_encode_without_qapplication_in_fresh_process() -> None:
    """QImage и JPEG работают без QApplication (рабочий поток Jarvis, `jarvis ask`): в новом процессе."""
    code = (
        "import threading\n"
        "from PySide6.QtCore import QCoreApplication\n"
        "from jarvis import vision\n"
        "from pc.screen import Shot\n"
        "out = {}\n"
        "def work():\n"
        "    out['jpeg'] = vision.encode_jpeg(Shot(2000, 1000, bytes([1, 2, 3, 0]) * 2000000), 1000, 80)\n"
        "t = threading.Thread(target=work)\n"
        "t.start(); t.join()\n"
        "assert QCoreApplication.instance() is None\n"
        "assert out['jpeg'][:2] == b'\\xff\\xd8', out\n"
    )
    env = {k: v for k, v in os.environ.items() if k != "QT_QPA_PLATFORM"}
    r = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120
    )
    assert r.returncode == 0, r.stderr


def test_qt_plugins_only_for_frozen_cli(tmp_path: Path) -> None:
    """jarvis-cli.exe (frozen, без QApplication) ищет плагин qjpeg в папке бандла; в разработке — ничего."""
    sub = ("PySide6", "plugins") if sys.platform == "win32" else ("PySide6", "Qt", "plugins")
    tmp_path.joinpath(*sub).mkdir(parents=True)  # несуществующую папку Qt молча не добавляет
    code = (
        "import sys\n"
        "from PySide6.QtCore import QCoreApplication\n"
        "from jarvis import vision\n"
        "before = list(QCoreApplication.libraryPaths())\n"
        "vision._qt_plugins()\n"
        "assert QCoreApplication.libraryPaths() == before\n"
        f"sys.frozen = True; sys._MEIPASS = {str(tmp_path)!r}\n"
        "vision._qt_plugins()\n"
        "paths = [p.replace('\\\\', '/') for p in QCoreApplication.libraryPaths()]\n"
        f"assert any(p.startswith({tmp_path.as_posix()!r}) and p.endswith('plugins') for p in paths), paths\n"
    )
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr


def test_import_is_light() -> None:
    """Роутер импортирует vision на горячем пути: без Qt и без pc.screen/pc.windows."""
    code = (
        "import sys, jarvis.vision\n"
        "heavy = [m for m in ('PySide6', 'pc.screen', 'pc.windows', 'openai_codex') if m in sys.modules]\n"
        "assert not heavy, heavy\n"
    )
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
