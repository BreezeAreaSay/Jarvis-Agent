"""Распознавание текста Windows (jarvis.platform.windows.ocr) на поддельных модулях pywinrt: сигнатуры — как в
колёсах winrt 3.2.1 (у SoftwareBitmap.create_copy_from_buffer четыре аргумента, с альфой — отдельный метод),
выбор языка, слова → строки. На настоящей Windows — то же на настоящем движке."""

import sys
import types
from typing import Any

import pytest

from jarvis.ai.ocr import OcrLine
from jarvis.platform.windows import ocr as win_ocr


class Inline:
    """Поток WinRT — прямо здесь."""

    def call(self, fn: Any, timeout: float) -> Any:
        return fn()


def _fake_winrt(monkeypatch: pytest.MonkeyPatch, languages: list[str], profile: str | None) -> dict[str, Any]:
    seen: dict[str, Any] = {}

    class Language:
        def __init__(self, tag: str) -> None:
            self.language_tag = tag

    class Rect:
        def __init__(self, x: float, y: float, width: float, height: float) -> None:
            self.x, self.y, self.width, self.height = x, y, width, height

    class Operation:
        def __init__(self, result: Any) -> None:
            self._result = result

        @property
        def completed(self) -> Any:
            return None

        @completed.setter
        def completed(self, handler: Any) -> None:
            handler(self, 1)

        def get_results(self) -> Any:
            return self._result

    class Engine:
        def __init__(self, tag: str) -> None:
            self.recognizer_language = Language(tag)

        def recognize_async(self, bitmap: Any) -> Operation:
            seen["bitmap"] = bitmap
            word = types.SimpleNamespace(bounding_rect=Rect(10, 20, 40, 12))
            line = types.SimpleNamespace(text="Отправить", words=[word])
            return Operation(types.SimpleNamespace(lines=[line]))

    class EngineMeta(type):
        @property
        def available_recognizer_languages(cls) -> list[Language]:
            return [Language(tag) for tag in languages]

        @property
        def max_image_dimension(cls) -> int:
            return 10000

    class OcrEngine(metaclass=EngineMeta):
        @staticmethod
        def try_create_from_language(language: Language, /) -> Engine:
            return Engine(language.language_tag)

        @staticmethod
        def try_create_from_user_profile_languages() -> Engine | None:
            return Engine(profile) if profile else None

    class Bitmap:
        def __init__(self, *args: Any) -> None:
            self.args = args
            self.closed = False

        def close(self) -> None:
            self.closed = True

    class SoftwareBitmap:
        @staticmethod
        def create_copy_from_buffer(source: Any, format: Any, width: int, height: int, /) -> Bitmap:
            return Bitmap(source, format, width, height)

        @staticmethod
        def create_copy_with_alpha_from_buffer(
            source: Any, format: Any, width: int, height: int, alpha: Any, /
        ) -> Bitmap:
            return Bitmap(source, format, width, height, alpha)

    class DataWriter:
        def write_bytes(self, value: Any, /) -> None:
            seen["bytes"] = bytes(value)

        def detach_buffer(self) -> str:
            return "buffer"

    modules = {
        "winrt": types.ModuleType("winrt"),
        "winrt.windows": types.ModuleType("winrt.windows"),
        "winrt.windows.media": types.ModuleType("winrt.windows.media"),
        "winrt.windows.media.ocr": types.SimpleNamespace(OcrEngine=OcrEngine),
        "winrt.windows.graphics": types.ModuleType("winrt.windows.graphics"),
        "winrt.windows.graphics.imaging": types.SimpleNamespace(
            SoftwareBitmap=SoftwareBitmap,
            BitmapPixelFormat=types.SimpleNamespace(BGRA8="BGRA8"),
            BitmapAlphaMode=types.SimpleNamespace(IGNORE="IGNORE"),
        ),
        "winrt.windows.storage": types.ModuleType("winrt.windows.storage"),
        "winrt.windows.storage.streams": types.SimpleNamespace(DataWriter=DataWriter),
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    return seen


def test_recognize_with_pywinrt_signatures(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _fake_winrt(monkeypatch, ["en-US", "ru-RU"], profile="en-US")
    backend = win_ocr.WindowsOcr(Inline())  # type: ignore[arg-type]
    pixels = b"\x01\x02\x03\xff" * (200 * 100)
    lines = backend.recognize(pixels, (200, 100), (1000, 500, 1400, 700))
    assert not backend.paused, "распознавание упало — см. журнал"
    # картинка 200×100 — кусок экрана 400×200: точка картинки = 2 точки экрана
    assert lines == [OcrLine("Отправить", (1020, 540, 1100, 564))]
    assert seen["bytes"] == pixels
    assert seen["bitmap"].args == ("buffer", "BGRA8", 200, 100, "IGNORE") and seen["bitmap"].closed
    # русский есть среди языков распознавания — он, даже если в профиле первым английский
    assert backend.language == "ru-RU"


@pytest.mark.parametrize(
    ("languages", "profile", "expected"),
    [(["en-US", "de-DE"], "de-DE", "de-DE"), (["en-US"], None, "en-US")],
)
def test_language_without_russian(
    monkeypatch: pytest.MonkeyPatch, languages: list[str], profile: str | None, expected: str
) -> None:
    _fake_winrt(monkeypatch, languages, profile)
    backend = win_ocr.WindowsOcr(Inline())  # type: ignore[arg-type]
    backend.recognize(b"\0" * 16, (2, 2), (0, 0, 2, 2))
    assert backend.language == expected


def test_no_language_pauses_recognition(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_winrt(monkeypatch, [], None)
    backend = win_ocr.WindowsOcr(Inline())  # type: ignore[arg-type]
    assert backend.recognize(b"\0" * 16, (2, 2), (0, 0, 2, 2)) == [] and backend.paused


@pytest.mark.skipif(sys.platform != "win32", reason="Windows.Media.Ocr есть только в Windows")
def test_real_windows_ocr_reads_rendered_text() -> None:
    """Windows (CI, ПК): настоящий движок читает нарисованную строку; нет языка распознавания — пропуск."""
    from PIL import Image, ImageDraw, ImageFont

    try:
        font = ImageFont.truetype(r"C:\\Windows\\Fonts\\segoeui.ttf", 24)
    except OSError:
        pytest.skip("нет шрифта Segoe UI")
    image = Image.new("RGB", (480, 60), (255, 255, 255))
    ImageDraw.Draw(image).text((10, 12), "Hello World 2026", font=font, fill=(0, 0, 0))
    backend = win_ocr.WindowsOcr()
    bgra = image.convert("RGBA").tobytes("raw", "BGRA")
    try:
        raw = backend._worker.call(lambda: backend._recognize(bgra, image.size), 10.0)
    except RuntimeError as e:
        pytest.skip(str(e))  # в Windows нет языка распознавания (чистый образ CI)
    text = " ".join(line for line, _words in raw)
    assert "Hello" in text and "2026" in text
