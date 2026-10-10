"""Иконка Jarvis.exe и установщика: build/jarvis.ico (16…256 px) из jarvis.ui.icon.render_icon("ready", size).

Запуск из корня репозитория: `uv run python packaging/make_icon.py [--out build/jarvis.ico]`.
Рисует офскрин (QT_QPA_PLATFORM=offscreen), дисплей не нужен. Файл .ico в git не кладём (build/ в .gitignore).

Формат собираем сами: ICONDIR + записи; 256 px — PNG, остальные — 32-битный DIB с альфой и AND-маской
(так их понимают и Проводник, и ресурсы exe PyInstaller, и Inno Setup). После записи файл читается обратно:
своим разбором заголовков и, если есть плагин qico, через QImageReader.
"""

import argparse
import os
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)
PNG_MIN = 256  # с этого размера запись хранится как PNG

if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))


_app: list[object] = []  # держим QGuiApplication живым до конца процесса


def _qt():
    """Поднять QGuiApplication офскрин (нужен для шрифтов в QPainter) и вернуть модули Qt."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtCore, QtGui

    if QtGui.QGuiApplication.instance() is None:
        _app.append(QtGui.QGuiApplication([sys.argv[0]]))
    return QtCore, QtGui


def _fallback_render(size: int):
    """TODO: временный рисунок, пока нет jarvis.ui.icon.render_icon (тот же рисунок должен быть в трее)."""
    QtCore, QtGui = _qt()
    img = QtGui.QImage(size, size, QtGui.QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(QtCore.Qt.GlobalColor.transparent)
    p = QtGui.QPainter(img)
    p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
    m = max(1.0, size / 16)
    rect = QtCore.QRectF(m / 2, m / 2, size - m, size - m)
    grad = QtGui.QLinearGradient(rect.topLeft(), rect.bottomRight())
    grad.setColorAt(0.0, QtGui.QColor("#3b82f6"))
    grad.setColorAt(1.0, QtGui.QColor("#8b5cf6"))
    p.setPen(QtCore.Qt.PenStyle.NoPen)
    p.setBrush(grad)
    p.drawRoundedRect(rect, size * 0.22, size * 0.22)
    p.setBrush(QtGui.QColor("#ffffff"))
    r = size * 0.16
    p.drawEllipse(QtCore.QPointF(size / 2, size / 2), r, r)
    p.end()
    return img


def _renderer():
    """render_icon из jarvis.ui.icon; модуля нет или он заготовка — временный рисунок (с предупреждением)."""
    _qt()
    try:
        from jarvis.ui.icon import render_icon

        render_icon("ready", 16)
        return lambda size: render_icon("ready", size)
    except (ImportError, NotImplementedError) as e:
        print(f"make_icon: render_icon недоступен ({e!r}) — временный рисунок (TODO)", file=sys.stderr)
        return _fallback_render


def render(size: int, draw=None):
    """Картинка нужного размера в формате для .ico."""
    img = (draw or _renderer())(size)
    if img.isNull():
        raise RuntimeError(f"render_icon вернул пустую картинку для {size} px")
    if img.width() != size or img.height() != size:
        from PySide6 import QtCore

        img = img.scaled(
            size,
            size,
            QtCore.Qt.AspectRatioMode.IgnoreAspectRatio,
            QtCore.Qt.TransformationMode.SmoothTransformation,
        )
    return img


def png_entry(img) -> bytes:
    """Картинка как PNG (запись для 256 px)."""
    from PySide6 import QtCore

    buf = QtCore.QBuffer()
    buf.open(QtCore.QIODevice.OpenModeFlag.WriteOnly)
    if not img.save(buf, "PNG"):
        raise RuntimeError("PNG не записался")
    return bytes(buf.data().data())


def dib_entry(img) -> bytes:
    """Картинка как 32-битный DIB для .ico: BITMAPINFOHEADER, BGRA снизу вверх, затем AND-маска."""
    from PySide6 import QtGui

    img = img.convertToFormat(QtGui.QImage.Format.Format_ARGB32)  # прямая (не умноженная) альфа
    w, h, bpl = img.width(), img.height(), img.bytesPerLine()
    raw = bytes(img.constBits())[: bpl * h]
    rows = [raw[y * bpl : y * bpl + w * 4] for y in range(h)]  # в памяти: B, G, R, A
    xor = b"".join(reversed(rows))
    mask_stride = (w + 31) // 32 * 4
    mask = bytearray()
    for row in reversed(rows):
        line = bytearray(mask_stride)
        for x in range(w):
            if row[x * 4 + 3] == 0:  # полностью прозрачный пиксель
                line[x // 8] |= 0x80 >> (x % 8)
        mask += line
    header = struct.pack("<IiiHHIIiiII", 40, w, h * 2, 1, 32, 0, len(xor) + len(mask), 0, 0, 0, 0)
    return header + xor + bytes(mask)


def build_ico(entries: list[tuple[int, bytes]]) -> bytes:
    """Собрать .ico из записей (размер, данные): ICONDIR, ICONDIRENTRY…, данные."""
    head = struct.pack("<HHH", 0, 1, len(entries))
    offset = len(head) + 16 * len(entries)
    dirs, blobs = b"", b""
    for size, data in entries:
        dim = size if size < 256 else 0
        dirs += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset)
        blobs += data
        offset += len(data)
    return head + dirs + blobs


def parse_ico(data: bytes) -> list[int]:
    """Разобрать .ico и вернуть размеры записей; битый файл — ValueError."""
    if len(data) < 6:
        raise ValueError("файл короче заголовка ICONDIR")
    reserved, kind, count = struct.unpack_from("<HHH", data, 0)
    if reserved != 0 or kind != 1 or count == 0:
        raise ValueError(f"не .ico: reserved={reserved} type={kind} count={count}")
    sizes = []
    for i in range(count):
        w, h, _colors, _res, _planes, _bpp, length, offset = struct.unpack_from("<BBBBHHII", data, 6 + 16 * i)
        w, h = w or 256, h or 256
        blob = data[offset : offset + length]
        if len(blob) != length or length == 0:
            raise ValueError(f"запись {i}: данные за концом файла")
        if blob.startswith(b"\x89PNG\r\n\x1a\n"):
            pw, ph = struct.unpack_from(">II", blob, 16)
        else:
            hdr, pw, ph2, _planes2, bpp = struct.unpack_from("<IiiHH", blob, 0)
            if hdr != 40 or bpp != 32:
                raise ValueError(f"запись {i}: неожиданный DIB (header={hdr}, bpp={bpp})")
            ph = ph2 // 2
        if (pw, ph) != (w, h):
            raise ValueError(f"запись {i}: размер {pw}x{ph} не совпадает с каталогом {w}x{h}")
        sizes.append(w)
    return sizes


def qt_read_sizes(path: Path) -> list[int] | None:
    """Размеры картинок .ico глазами Qt (плагин qico); плагина нет — None."""
    _, QtGui = _qt()
    if b"ico" not in [bytes(f.data()) for f in QtGui.QImageReader.supportedImageFormats()]:
        return None
    reader = QtGui.QImageReader(str(path), b"ico")
    sizes = []
    for i in range(reader.imageCount()):
        reader.jumpToImage(i)
        img = reader.read()
        if img.isNull():
            raise ValueError(f"Qt не прочитал картинку {i}: {reader.errorString()}")
        sizes.append(img.width())
    return sizes


def make(out: Path) -> list[int]:
    draw = _renderer()
    entries = []
    for size in SIZES:
        img = render(size, draw)
        entries.append((size, png_entry(img) if size >= PNG_MIN else dib_entry(img)))
    data = build_ico(entries)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, out)
    sizes = parse_ico(out.read_bytes())
    if sorted(sizes) != sorted(SIZES):
        raise ValueError(f"в .ico размеры {sizes}, ждали {list(SIZES)}")
    qt_sizes = qt_read_sizes(out)
    if qt_sizes is not None and sorted(qt_sizes) != sorted(SIZES):
        raise ValueError(f"Qt читает размеры {qt_sizes}, ждали {list(SIZES)}")
    return sizes


def _utf8_streams() -> None:
    """Pipe и файл — UTF-8 (Windows-Python в CI или под wine пишет в cp1252 и падает на кириллице)."""
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and not stream.isatty():
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]


def main(argv: list[str] | None = None) -> int:
    _utf8_streams()
    parser = argparse.ArgumentParser(description="Нарисовать build/jarvis.ico")
    parser.add_argument("--out", type=Path, default=ROOT / "build" / "jarvis.ico")
    args = parser.parse_args(argv)
    sizes = make(args.out)
    print(f"иконка: {args.out} ({args.out.stat().st_size} байт, размеры {sizes})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
