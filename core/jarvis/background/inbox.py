"""«Входящие.md» в папке агента: задачи, которые человек записал строками (в том числе с телефона — через
синхронизацию Obsidian). Jarvis берёт новые в работу и отмечает их: «- [x] … — [[заметка задачи|задача]]».

Новая задача — строка-пункт («- …», «- [ ] …», «* …», «1. …») или просто строка текста; строки с отступом
сразу под пунктом — его подробности. Не задачи: заголовки, цитаты и выноски («>»), таблицы, разделители,
отмеченные пункты («- [x]», «- [-]»), блоки кода, свойства в начале заметки, комментарии <!-- -->.

Здесь — только разбор и разметка текста; файл читает и пишет сервис (jarvis.background.service).
"""

import re
from dataclasses import dataclass, field

INBOX = "Входящие.md"
MAX_BRIEF = 2000
TEMPLATE = """# Входящие

> [!tip] Задачи для Jarvis
> Пишите сюда задачи — по одной на строку, можно с телефона. Jarvis сам заберёт новые в работу и отметит их
> галочкой со ссылкой на заметку задачи. Подробности — строками с отступом под задачей.
> Повторять: «каждый вечер записывай курс доллара ЦБ в замеры».

"""

_ITEM = re.compile(r"^(?P<indent>\s*)(?:[-*+]|\d{1,3}[.)])\s+(?:\[(?P<mark>.)\]\s*)?(?P<text>.*)$")
_SKIP = re.compile(r"^\s*(?:#|>|\||---|\*\*\*|___|<!--|```|~~~)")


@dataclass
class Item:
    line: int  # строка пункта (её отметит Jarvis)
    text: str
    details: list[str] = field(default_factory=list)

    @property
    def brief(self) -> str:
        parts = [self.text, *self.details]
        return "\n".join(p for p in parts if p)[:MAX_BRIEF]


def parse(text: str) -> list[Item]:
    """Новые задачи во «Входящих» (по порядку)."""
    lines = text.splitlines()
    items: list[Item] = []
    current: Item | None = None  # пункт, к которому идут строки с отступом
    taken = False  # …или отмеченный пункт: его подробности не задачи
    fence = False
    start = 0
    if lines and lines[0].strip() == "---":  # свойства заметки
        end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
        start = end + 1 if end is not None else 0
    comment = False
    for index in range(start, len(lines)):
        raw = lines[index]
        stripped = raw.strip()
        if stripped.startswith(("```", "~~~")):
            fence = not fence
            current, taken = None, False
            continue
        if fence:
            continue
        if comment:
            comment = "-->" not in stripped
            continue
        if stripped.startswith("<!--"):
            comment = "-->" not in stripped
            continue
        if not stripped:
            continue
        indented = raw[:1] in (" ", "\t")
        match = _ITEM.match(raw)
        if indented and (current is not None or taken) and not (match and not match["indent"]):
            if current is not None and not taken:
                detail = match["text"].strip() if match else stripped
                if detail:
                    current.details.append(detail)
            continue
        current, taken = None, False
        if _SKIP.match(raw):
            continue
        if match:
            mark = (match["mark"] or " ").strip().casefold()
            body = match["text"].strip()
            if mark:  # [x], [-], [>] — сделано, отменено, перенесено
                taken = True
                continue
        else:
            body = stripped
        if len(body) < 3:
            continue
        current = Item(index, body)
        items.append(current)
    return items


def mark(text: str, done: dict[int, str]) -> str:
    """Отметить взятые пункты: строка → «- [x] текст — [[заметка задачи|задача]]» (отступ сохраняется);
    «!причина» вместо заметки — «- [x] текст — ⚠️ не взято: причина» (чтобы не браться снова)."""
    lines = text.splitlines()
    for index, note in done.items():
        if not 0 <= index < len(lines):
            continue
        raw = lines[index]
        match = _ITEM.match(raw)
        indent = match["indent"] if match else raw[: len(raw) - len(raw.lstrip())]
        body = (match["text"] if match else raw).strip()
        if note.startswith("!"):
            link = f" — ⚠️ не взято: {note[1:].strip()}"
        else:
            link = f" — [[{note.removesuffix('.md')}|задача]]" if note else ""
        lines[index] = f"{indent}- [x] {body}{link}"
    return "\n".join(lines) + ("\n" if text.endswith("\n") or not text else "")
