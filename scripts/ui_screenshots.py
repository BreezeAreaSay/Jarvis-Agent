"""Снимки окна Jarvis и сферы во всех состояниях (офскрин) — проверка вёрстки глазами.

    uv run python scripts/ui_screenshots.py [--out docs/screenshots] [--scale 2]

Без долгого событийного цикла: выставить состояние → processEvents → довести анимации до конца
(бегущий градиент и спиннер — в середине оборота, сфера — на характерном кадре) → grab() → PNG на фоне
«рабочего стола». 13-orb-states — сфера во всех состояниях, 14-orb-desktop — сфера в углу рабочего стола
рядом с окном. docs/screenshots/ — в .gitignore: снимки в git не попадают.
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

    from PySide6.QtCore import (
        QAbstractAnimation,
        QPoint,
        QPointF,
        QRectF,
        Qt,
        QtMsgType,
        qInstallMessageHandler,
    )
    from PySide6.QtGui import QColor, QFont, QFontDatabase, QImage, QLinearGradient, QPainter, QRadialGradient
    from PySide6.QtWidgets import QApplication

    from jarvis.config import UiConfig
    from jarvis.events import Done, Items, Level, Status, TextChunk
    from jarvis.ui import theme
    from jarvis.ui.logic import ORB_LEVELS, ConfirmRequest, OrbModel
    from jarvis.ui.orb import OrbWindow, paint_orb
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

    def orb_frame(m: OrbModel) -> None:
        """Сфера — на характерном кадре: вспышка в разгаре, работа в полную силу, покой — застывший."""
        seconds = 0.14 if m.state in ("success", "error") else 1.4
        for _ in range(round(seconds * 60)):
            m.tick(1 / 60)

    def settle(w: LauncherWindow, phase: float = 0.36) -> None:
        """Дойти до конечного кадра: обработать события и довести конечные анимации до конца."""
        orb_frame(w.orb_model)
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

    def wallpaper(p: QPainter, width: int, height: int) -> None:
        """Фон «рабочего стола»: сине-фиолетовый градиент и тёплый блик."""
        bg = QLinearGradient(0, 0, width, height)
        bg.setColorAt(0.0, QColor("#2B3A55"))
        bg.setColorAt(0.55, QColor("#3B2F4A"))
        bg.setColorAt(1.0, QColor("#1F2B33"))
        p.fillRect(0, 0, width, height, bg)
        glow = QRadialGradient(QPointF(width * 0.8, height * 0.1), width * 0.5)
        glow.setColorAt(0.0, QColor(255, 190, 120, 70))
        glow.setColorAt(1.0, QColor(255, 190, 120, 0))
        p.fillRect(0, 0, width, height, glow)

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
        wallpaper(p, canvas.width(), canvas.height())
        shot.setDevicePixelRatio(1.0)
        p.drawImage(pad, pad, shot)
        p.end()
        path = out / f"{name}.png"
        canvas.save(str(path), "PNG")
        return path

    def run(m: OrbModel, seconds: float) -> OrbModel:
        for _ in range(round(seconds * 60)):
            m.tick(1 / 60)
        return m

    def orb_states() -> list[tuple[str, OrbModel]]:
        """Сфера в каждом состоянии — как её видит человек."""
        items: list[tuple[str, OrbModel]] = [("покой", OrbModel())]
        m = OrbModel()
        m.wake()
        items.append(("пробуждение", run(m, 0.22)))
        m = OrbModel()
        m.wake()
        run(m, 3.0)
        m.key()
        items.append(("ввод", run(m, 0.05)))
        names = {"grammar": "грамматика", "hands": "руки", "brain": "GPT", "local": "локально"}
        for level in ORB_LEVELS:
            m = OrbModel()
            m.set_local(level == "local")
            m.wake()
            m.begin()
            m.set_level(level)
            items.append((f"работа: {names[level]}", run(m, 1.4)))
        m = OrbModel()
        m.wake()
        m.begin()
        m.set_level("brain")
        run(m, 0.8)
        for _ in range(6):
            m.chunk()
            run(m, 0.12)
        items.append(("поток GPT", m))
        m = OrbModel()
        m.wake()
        m.begin()
        m.set_level("brain")
        run(m, 0.5)
        m.confirm(True)
        items.append(("подтверждение", run(m, 1.4)))
        for ok, title in ((True, "успех"), (False, "ошибка")):
            m = OrbModel()
            m.wake()
            m.begin()
            m.set_level("hands")
            run(m, 0.6)
            m.done(ok)
            items.append((title, run(m, 0.14)))
        return items

    def orb_grid(name: str, scale: float) -> Path:
        """13-orb-states: сетка состояний сферы, каждая — на тёмной плитке с подписью."""
        items = orb_states()
        cols, size, label = 4, 150, 26
        cell_w, cell_h = size + 24, size + label + 18
        width, height = cols * cell_w + 24, ((len(items) + cols - 1) // cols) * cell_h + 24
        img = QImage(round(width * scale), round(height * scale), QImage.Format.Format_ARGB32_Premultiplied)
        img.setDevicePixelRatio(scale)
        p = QPainter(img)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(QRectF(0, 0, width, height), QColor("#0E0E12"))
        font = theme.font(theme.FONT_ROW)
        p.setFont(font)
        for i, (title, m) in enumerate(items):
            x, y = 12 + (i % cols) * cell_w, 12 + (i // cols) * cell_h
            tile = QRectF(x + 4, y + 4, cell_w - 8, cell_h - 8)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(theme.BG))
            p.drawRoundedRect(tile, theme.RADIUS_CARD, theme.RADIUS_CARD)
            paint_orb(p, QRectF(x + 12, y + 8, size, size), m)
            p.setPen(theme.color(theme.TEXT_MUTED))
            p.drawText(QRectF(x, y + size + 8, cell_w, label), Qt.AlignmentFlag.AlignCenter, title)
        p.end()
        path = out / f"{name}.png"
        img.save(str(path), "PNG")
        return path

    def orb_desktop(name: str, scale: float) -> Path:
        """14-orb-desktop: «рабочий стол» 1920×1080 — окно Jarvis отвечает, сфера в углу над панелью задач."""
        width, height, taskbar = 1920, 1080, 48
        model = OrbModel()
        w = LauncherWindow(UiConfig(), orb=model)
        brain_stream(w)
        orb = OrbWindow(model, UiConfig(), state={}, writer=lambda update: None)
        r = orb.orb_size // 2
        center = QPoint(width - 1 - 24 - r, height - taskbar - 1 - 24 - r)  # как default_center над панелью
        orb.place({"orb": {"x": center.x(), "y": center.y()}})
        orb.start()
        settle(w)
        img = QImage(round(width * scale), round(height * scale), QImage.Format.Format_ARGB32_Premultiplied)
        img.setDevicePixelRatio(scale)
        p = QPainter(img)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        wallpaper(p, width, height)
        p.fillRect(QRectF(0, height - taskbar, width, taskbar), QColor(24, 24, 30, 235))
        p.fillRect(QRectF(0, height - taskbar, width, 1), QColor(255, 255, 255, 20))
        for pos, shot in ((w.pos(), w.grab().toImage()), (orb.pos(), orb.grab().toImage())):
            shot_dpr = shot.devicePixelRatio()
            p.drawImage(QRectF(pos.x(), pos.y(), shot.width() / shot_dpr, shot.height() / shot_dpr), shot)
        p.end()
        path = out / f"{name}.png"
        img.save(str(path), "PNG")
        orb.close_orb()
        orb.deleteLater()
        w.close()
        w.deleteLater()
        app.processEvents()
        return path

    def empty(w: LauncherWindow) -> None:
        w.show_launcher()

    def typing(w: LauncherWindow) -> None:
        w.show_launcher()
        w._input.setText("открой телегу и включи музыку погромче")
        w._input.end(False)

    def hands_busy(w: LauncherWindow) -> None:
        w.show_launcher()
        # как в core: фраза не для грамматики, руки ещё грузят модель (первая команда после старта)
        w.begin_request("сверни телегу и открой дискорд")
        w.on_event(Level("hands", "hands", "4b"))
        w.on_event(Status("Запускаю руки…", key="hands-start"))

    def hands_done(w: LauncherWindow) -> None:
        w.show_launcher()
        w.begin_request("открой телегу")
        # как в core: грамматика без строк «⚙» — сразу итог действия (окно спрячется через ui.autohide_s)
        w.on_event(Level("grammar", "grammar:open"))
        w.on_event(Done(True, "Открыл Telegram", level="grammar", reason="grammar:open", autohide=True))

    def brain_stream(w: LauncherWindow) -> None:
        w.show_launcher()
        w.begin_request("gpt: расскажи про Vulkan")
        w.on_event(Level("brain", "prefix:gpt", "gpt-6-luna"))
        w.on_event(Status("⚙ find_files: vulkan", kind="tool", key="f1"))
        w.on_event(Status("⚙ find_files: vulkan", kind="tool", key="f1", done=True, ok=True))
        for chunk in BRAIN_TEXT:
            w.on_event(TextChunk(chunk))
        w.on_event(Status("⚙ буфер: vkCreateInstance", kind="tool", key="c1"))

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
        # как в жизни: грамматика разобрала команду, а pc.audio не нашёл устройство вывода
        w.begin_request("громкость 20")
        w.on_event(Level("grammar", "grammar:vol"))
        w.on_event(Done(False, "Нет устройства вывода звука", "grammar", reason="grammar:vol"))

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
    print(orb_grid("13-orb-states", max(2.0, args.scale)))
    print(orb_desktop("14-orb-desktop", args.scale))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
