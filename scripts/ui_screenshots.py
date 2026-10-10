"""Снимки окна Jarvis во всех состояниях (офскрин) — проверка вёрстки глазами.

    uv run python scripts/ui_screenshots.py [--out docs/screenshots] [--scale 2]

Без долгого событийного цикла: выставить состояние → processEvents → довести анимации до конца
(бегущий градиент и спиннер — в середине оборота) → grab() → PNG на фоне «рабочего стола».
docs/screenshots/ — в .gitignore: снимки в git не попадают.
"""

import argparse
import json
import os
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
FOUND = [
    r"C:\Users\me\Documents\отчёт.docx",
    r"C:\Users\me\Documents\Работа\2026\отчёт за квартал — финальная версия.docx",
    r"C:\Users\me\Desktop\отчёт (копия).pdf",
    r"C:\Users\me\Downloads\отчёт-по-проекту-Jarvis-с-очень-длинным-именем-файла-для-проверки.xlsx",
    r"D:\Архив\Отчёты\отчёт 2025.docx",
]
BRAIN_TEXT = [
    "Vulkan — низкоуровневый графический API от Khronos Group. ",
    "Он даёт приложению прямой контроль над GPU: память, синхронизацию и очереди команд ",
    "программа ведёт сама, поэтому накладные расходы драйвера меньше, чем у OpenGL.\n\n",
    "Для Jarvis это важно: llama.cpp на Radeon RX 7600 работает именно через Vulkan — ",
    "модель рук целиком лежит в видеопамяти, и ответ приходит за доли секунды.\n\n",
    "Главные понятия: instance, physical device, logical device, queue, command buffer, ",
    "pipeline и descriptor set.",
]


def _platform(scale: float) -> None:
    """Офскрин-экран 1920×1080 (если платформа не задана снаружи)."""
    if os.environ.get("QT_QPA_PLATFORM"):
        return
    w, h = round(1920 * scale), round(1080 * scale)
    screen = {"name": "jarvis", "x": 0, "y": 0, "width": w, "height": h, "logicalDpi": 96}
    cfg = Path(tempfile.gettempdir()) / f"jarvis-offscreen-{os.getpid()}.json"
    cfg.write_text(json.dumps({"screens": [screen]}), encoding="utf-8")
    os.environ["QT_QPA_PLATFORM"] = f"offscreen:configfile={cfg}"
    if scale != 1.0:
        os.environ.setdefault("QT_SCALE_FACTOR", str(scale))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default=str(ROOT / "docs" / "screenshots"), help="папка для PNG")
    parser.add_argument("--scale", type=float, default=1.0, help="масштаб экрана (2 — чёткие снимки)")
    args = parser.parse_args(argv)
    _platform(args.scale)
    sys.path.insert(0, str(ROOT / "src"))

    from PySide6.QtCore import QAbstractAnimation, QPointF, QtMsgType, qInstallMessageHandler
    from PySide6.QtGui import QColor, QFont, QFontDatabase, QImage, QLinearGradient, QPainter, QRadialGradient
    from PySide6.QtWidgets import QApplication

    from jarvis.config import UiConfig
    from jarvis.events import Done, Items, Level, Status, TextChunk
    from jarvis.ui.logic import ConfirmRequest
    from jarvis.ui.window import LauncherWindow

    def quiet(mode: QtMsgType, context: object, message: str) -> None:
        # офскрин-платформа ругается на raise()/прозрачность окна — для снимков это не важно
        if "This plugin does not support" not in message:
            print(message, file=sys.stderr)

    qInstallMessageHandler(quiet)
    app = QApplication.instance() or QApplication([])
    if sys.platform != "win32" and "Inter" in QFontDatabase.families():
        # на Linux нет Segoe — для снимков подставляем близкий по метрикам Inter
        for name in ("Segoe UI Variable Text", "Segoe UI Variable Display", "Segoe UI Variable", "Segoe UI"):
            QFont.insertSubstitution(name, "Inter")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    def settle(w: LauncherWindow, phase: float = 0.36) -> None:
        """Дойти до конечного кадра: обработать события и довести конечные анимации до конца."""
        for _ in range(6):
            app.processEvents()
            for anim in w.findChildren(QAbstractAnimation):
                if anim.state() != QAbstractAnimation.State.Running or anim.group() is not None:
                    continue
                if anim.loopCount() == -1:
                    anim.setCurrentTime(int(anim.duration() * phase))
                else:
                    anim.setCurrentTime(anim.totalDuration())
        app.processEvents()

    def save(w: LauncherWindow, name: str) -> Path:
        shot = w.grab().toImage()
        dpr = shot.devicePixelRatio()
        pad = int(28 * dpr)
        canvas = QImage(
            shot.width() + 2 * pad, shot.height() + 2 * pad, QImage.Format.Format_ARGB32_Premultiplied
        )
        canvas.setDevicePixelRatio(1.0)
        p = QPainter(canvas)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        bg = QLinearGradient(0, 0, canvas.width(), canvas.height())
        bg.setColorAt(0.0, QColor("#2B3A55"))
        bg.setColorAt(0.55, QColor("#3B2F4A"))
        bg.setColorAt(1.0, QColor("#1F2B33"))
        p.fillRect(canvas.rect(), bg)
        glow = QRadialGradient(QPointF(canvas.width() * 0.8, canvas.height() * 0.1), canvas.width() * 0.5)
        glow.setColorAt(0.0, QColor(255, 190, 120, 70))
        glow.setColorAt(1.0, QColor(255, 190, 120, 0))
        p.fillRect(canvas.rect(), glow)
        shot.setDevicePixelRatio(1.0)
        p.drawImage(pad, pad, shot)
        p.end()
        path = out / f"{name}.png"
        canvas.save(str(path), "PNG")
        return path

    def empty(w: LauncherWindow) -> None:
        w.show_launcher()

    def typing(w: LauncherWindow) -> None:
        w.show_launcher()
        w._input.setText("открой телегу и включи музыку погромче")
        w._input.end(False)

    def hands_busy(w: LauncherWindow) -> None:
        w.show_launcher()
        w.begin_request("закрой окно хрома")
        w.on_event(Level("hands", "router:default", "4b"))
        w.on_event(Status("⚙ close: Google Chrome", kind="tool", key="t1"))

    def hands_done(w: LauncherWindow) -> None:
        w.show_launcher()
        w.begin_request("открой телегу")
        w.on_event(Level("grammar", "grammar:open"))
        w.on_event(Status("⚙ open: Telegram", kind="tool", key="t1"))
        w.on_event(Status("⚙ open: Telegram", kind="tool", key="t1", done=True, ok=True))
        w.on_event(Done(True, "Открыл Telegram", level="grammar", autohide=True))

    def brain_stream(w: LauncherWindow) -> None:
        w.show_launcher()
        w.begin_request("gpt: расскажи про Vulkan")
        w.on_event(Level("brain", "prefix:gpt", "gpt-6-luna"))
        w.on_event(Status("⚙ find_files: vulkan", kind="tool", key="f1"))
        w.on_event(Status("⚙ find_files: 3 файла", kind="tool", key="f1", done=True, ok=True))
        for chunk in BRAIN_TEXT:
            w.on_event(TextChunk(chunk))
        w.on_event(Status("⚙ буфер: «vkCreateInstance»", kind="tool", key="c1"))

    def find_list(w: LauncherWindow) -> None:
        w.show_launcher()
        w.begin_request("найди отчёт")
        w.on_event(Level("grammar", "grammar:find"))
        w.on_event(Items(FOUND))
        w.on_event(Done(True, "Нашёл 5 файлов", level="grammar"))
        settle(w)
        block = next(b for b in w._blocks if hasattr(b, "rows"))
        block.rows[1].set_hovered(True)

    def confirm(w: LauncherWindow) -> None:
        w.show_launcher()
        w.begin_request("gpt: закрой всё, что тормозит")
        w.on_event(Level("brain", "prefix:gpt"))
        w.on_event(Status("⚙ processes: chrome", kind="tool", key="p1", done=True, ok=True))
        w.on_event(Status("⚙ kill_process: chrome.exe", kind="tool", key="k1"))
        req = ConfirmRequest(
            "Завершить chrome.exe (12 процессов)?",
            "Несохранённые данные во вкладках могут пропасть.\n"
            "PID: 4120, 4188, 5012, 5544, 6080, 7124 и ещё 6",
            "brain",
            created=time.monotonic() - 9.0,
        )
        w.ask_confirm(req)
        w._confirms.shown_at = time.monotonic() - 1.0  # защита 700 мс уже прошла — кнопки видны
        w._confirm_tick()

    def error(w: LauncherWindow) -> None:
        w.show_launcher()
        w.begin_request("громкость 20")
        w.on_event(Level("hands", "router:default", "4b"))
        w.on_event(Status("⚙ vol: 20", kind="tool", key="v1"))
        w.on_event(
            Done(
                False,
                "Руки не ответили за 4 с: сервер llama не запущен. Трей → «Перезапустить руки».",
                "hands",
            )
        )

    def local(w: LauncherWindow) -> None:
        w.set_local_mode(True)
        w.show_launcher()
        # как в core: вопрос мозгу при «локально:» — без облака, только сообщение
        w.begin_request("локально: почему небо голубое")
        w.on_event(Level("local", "local:needs_gpt"))
        w.on_event(
            Done(False, "Это нужно GPT, а включён локальный режим", level="local", reason="local:needs_gpt")
        )

    def local_idle(w: LauncherWindow) -> None:
        w.set_local_mode(True)
        w.show_launcher()

    def untrusted(w: LauncherWindow) -> None:
        w.show_launcher()
        w.begin_request("найди <b>x</b>")
        w.on_event(Level("hands", "router:default"))
        w.on_event(
            Status('⚙ open: <b>x</b><a href="file:///C:/">y</a>', kind="tool", key="u1", done=True, ok=False)
        )
        w.on_event(TextChunk('<b>жирный?</b> <a href="file:///C:/">ссылка?</a> отчёт\u202excod.exe'))
        w.on_event(Done(True, level="hands"))

    states: list[tuple[str, Callable[[Any], None], bool]] = [
        ("01-empty", empty, True),
        ("02-typing", typing, True),
        ("03-hands-busy", hands_busy, True),
        ("04-hands-done", hands_done, True),
        ("05-brain-stream", brain_stream, True),
        ("06-find-list", find_list, True),
        ("07-confirm", confirm, True),
        ("08-error", error, True),
        ("09-local", local, True),
        ("10-local-idle", local_idle, True),
        ("11-untrusted-text", untrusted, True),
        ("12-hands-busy-static", hands_busy, False),
    ]
    for name, fill, animations in states:
        w = LauncherWindow(UiConfig(animations=animations))
        fill(w)
        settle(w)
        path = save(w, name)
        print(path)
        w.close()
        w.deleteLater()
        app.processEvents()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
