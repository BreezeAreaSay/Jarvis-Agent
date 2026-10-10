"""Тема окна Jarvis: палитра, радиусы, отступы, шрифты и единственная таблица стилей stylesheet().

Тёмная тема в духе Spotlight/Raycast. Цвета — строки "#RRGGBB"; QColor с прозрачностью даёт color().
"""

from PySide6.QtGui import QColor, QFont

# --- палитра -------------------------------------------------------------------------------------
BG = "#16161A"  # фон панели
SURFACE = "#202026"  # карточки, клавиши-подсказки
SURFACE_HOVER = "#2A2A32"  # подсветка строки под мышью
BORDER = "#FFFFFF"  # рамка панели — белый с малой прозрачностью (BORDER_ALPHA)
BORDER_ALPHA = 0.09
TEXT = "#ECECF1"
TEXT_MUTED = "#A3A3AE"
TEXT_FAINT = "#6C6C78"
ACCENT = "#7C9BFF"
SUCCESS = "#4ADE80"
ERROR = "#F87171"
WARN = "#FBBF24"
CONFIRM = "#F5A524"  # акцентная рамка карточки подтверждения
SHADOW = "#000000"

LEVEL_COLORS: dict[str, str] = {
    "grammar": "#60A5FA",  # голубой — правила, мгновенно
    "hands": "#F59E0B",  # янтарный — локальная модель
    "brain": "#A78BFA",  # фиолетовый — GPT
    "local": "#2DD4BF",  # бирюзовый — без облака
}
LOCAL_MODE = LEVEL_COLORS["local"]
TOAST_COLORS: dict[str, str] = {"error": ERROR, "warn": WARN, "info": ACCENT}

# --- размеры (логические px) ------------------------------------------------------------------------
RADIUS = 14  # панель
RADIUS_CARD = 12
RADIUS_ROW = 8
RADIUS_BADGE = 10
SHADOW_MARGIN = (22, 14, 22, 30)  # слева, сверху, справа, снизу — поле под мягкую тень
SHADOW_OFFSET_Y = 8
INPUT_H = 58
STRIP_H = 2
FOOTER_H = 34
PAD_X = 18  # горизонтальный отступ строки ввода
BODY_PAD = (10, 6, 10, 10)  # тело ответа: слева, сверху, справа, снизу
ROW_PAD_X = 8  # правый отступ строк ответа
ICON_X = 11  # значки строк — под значком строки ввода (от левого края тела)
GUTTER = 42  # колонка текста ответа — под текстом строки ввода (от левого края тела)
ICON_GAP = GUTTER - ICON_X - 16  # между значком 16 px и текстом
ITEM_H = 34
TOP_FRACTION = 0.2  # верх окна — 20 % высоты экрана

# --- анимации (мс) ----------------------------------------------------------------------------------
SHOW_MS = 140
SHOW_SHIFT_PX = 8
HIDE_MS = 90
HEIGHT_MS = 120
COLOR_MS = 220
BUTTONS_FADE_MS = 160
DRIVER_MS = 1400  # один оборот бегущего градиента; спиннер делает за это время два оборота
CONFIRM_TICK_MS = 100

# --- шрифты -----------------------------------------------------------------------------------------
# Segoe UI Variable (Windows 11), затем Segoe UI (Windows 10); нет ни одного — системный шрифт Qt.
TEXT_FAMILIES = ["Segoe UI Variable Text", "Segoe UI Variable", "Segoe UI"]
DISPLAY_FAMILIES = ["Segoe UI Variable Display", "Segoe UI Variable", "Segoe UI"]

FONT_INPUT = 20
FONT_BODY = 14
FONT_ROW = 13
FONT_SMALL = 12
FONT_TINY = 11


def font(px: int, weight: QFont.Weight = QFont.Weight.Normal, display: bool = False) -> QFont:
    """Шрифт интерфейса заданного размера в пикселях (не зависит от DPI-настройки шрифтов)."""
    f = QFont()
    f.setFamilies(DISPLAY_FAMILIES if display else TEXT_FAMILIES)
    f.setPixelSize(px)
    f.setWeight(weight)
    f.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    return f


def color(value: str, alpha: float = 1.0) -> QColor:
    c = QColor(value)
    c.setAlphaF(max(0.0, min(1.0, alpha)))
    return c


def _rgba(value: str, alpha: float) -> str:
    c = QColor(value)
    return f"rgba({c.red()}, {c.green()}, {c.blue()}, {round(alpha * 255)})"


def stylesheet() -> str:
    """Таблица стилей окна: поле ввода, текст ответа, прокрутка, кнопки подтверждения."""
    return f"""
QWidget#launcher, QWidget#panel, QWidget#body, QScrollArea#scroll, QWidget#scrollViewport {{
    background: transparent;
    border: none;
}}
QLineEdit#input {{
    background: transparent;
    border: none;
    padding: 0px;
    color: {TEXT};
    selection-background-color: {_rgba(ACCENT, 0.26)};
    selection-color: {TEXT};
}}
QTextEdit {{
    background: transparent;
    border: none;
    color: {TEXT};
    selection-background-color: {_rgba(ACCENT, 0.26)};
    selection-color: {TEXT};
}}
QTextEdit#details {{
    color: {TEXT_MUTED};
}}
QLabel {{
    color: {TEXT_MUTED};
    background: transparent;
}}
QLabel#confirmHint {{
    color: {TEXT_FAINT};
}}
QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 4px 2px 4px 0px;
}}
QScrollBar::handle:vertical {{
    background: {_rgba("#FFFFFF", 0.14)};
    border-radius: 3px;
    min-height: 28px;
    margin: 0px 1px;
}}
QScrollBar::handle:vertical:hover {{
    background: {_rgba("#FFFFFF", 0.26)};
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0px;
    border: none;
    background: transparent;
}}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
    background: transparent;
}}
QPushButton#confirmNo {{
    background: {_rgba("#FFFFFF", 0.09)};
    color: {TEXT};
    border: 1px solid {_rgba("#FFFFFF", 0.14)};
    border-radius: 8px;
    padding: 6px 18px;
}}
QPushButton#confirmNo:hover {{
    background: {_rgba("#FFFFFF", 0.14)};
}}
QPushButton#confirmNo:focus {{
    border: 1px solid {_rgba(ACCENT, 0.9)};
}}
QPushButton#confirmYes {{
    background: transparent;
    color: {CONFIRM};
    border: 1px solid {_rgba(CONFIRM, 0.55)};
    border-radius: 8px;
    padding: 6px 14px;
}}
QPushButton#confirmYes:hover {{
    background: {_rgba(CONFIRM, 0.14)};
}}
QMenu {{
    background: {SURFACE};
    color: {TEXT};
    border: 1px solid {_rgba("#FFFFFF", 0.12)};
    border-radius: 8px;
    padding: 4px;
}}
QMenu::item {{
    padding: 5px 18px 5px 12px;
    border-radius: 5px;
}}
QMenu::item:selected {{
    background: {_rgba(ACCENT, 0.28)};
}}
QMenu::item:disabled {{
    color: {TEXT_FAINT};
}}
QMenu::separator {{
    height: 1px;
    margin: 4px 6px;
    background: {_rgba("#FFFFFF", 0.08)};
}}
QPushButton#confirmNo:disabled, QPushButton#confirmYes:disabled {{
    color: {TEXT_FAINT};
    border-color: {_rgba("#FFFFFF", 0.08)};
}}
"""
