"""Хранилище заметок для фоновых задач: Obsidian (или просто папка с Markdown) и своя среда агента в нём.

Хранилище — settings.background.vault; пусто — открытое хранилище Obsidian из его списка (obsidian.json),
нет Obsidian — папка Документы\\Jarvis (её можно открыть в Obsidian как хранилище). Агент работает в своей
папке (settings.background.folder, по умолчанию «Jarvis»): пишет только туда; читать и искать может всё
хранилище (settings.background.read_vault). Пути проверяются: ни «..», ни ссылок наружу, ни служебных папок
(.obsidian, .git, .trash). Obsidian сам подхватывает изменённые файлы — открывать его не нужно.

Всё блокирующее (файлы) — из пула ввода-вывода (ctx.run_io).
"""

import contextlib
import json
import logging
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from jarvis import paths
from jarvis.ai.format import one_line
from jarvis.services import UserFacingError

if TYPE_CHECKING:
    from jarvis.background.model import Task

log = logging.getLogger(__name__)

NOTE_EXTS = (".md", ".txt", ".csv")  # что агент пишет; читает — ещё и .canvas, .json
READ_EXTS = (*NOTE_EXTS, ".canvas", ".json")
MAX_READ = 60_000  # знаков заметки за раз
MAX_WRITE = 400_000
MAX_SCAN_FILES = 8000  # поиск по хранилищу: не больше стольких файлов…
MAX_SCAN_BYTES = 400_000  # …и не больше стольких байт с файла
BAD_NAME = re.compile(r'[<>:"|?*\x00-\x1f]')
STATUS_WORDS = {
    "queued": "в очереди",
    "running": "работает",
    "waiting": "ждёт ответа",
    "done": "готово",
    "partial": "сделано не всё",
    "failed": "не получилось",
    "cancelled": "отменено",
}
STATUS_ICON = {"done": "✅", "partial": "⚠️", "failed": "❌", "cancelled": "⏹️"}

FOLDERS = ("Задачи", "Исследования", "Замеры", "Журнал")
README = """# Папка Jarvis

Здесь работает агент Jarvis: в фоне, пока вы заняты своим, он ищет, читает сайты и записывает сюда итоги.
Пишет агент только в эту папку; читать может всё хранилище (выключается в настройках Jarvis → Фоновые задачи).

- **Инструкции.md** — ваши постоянные указания агенту: как оформлять заметки, каким источникам доверять, о чём
  не писать. Он читает их перед каждой задачей.
- **Задачи/** — заметка на каждую задачу: что просили, план, ход работы, вопросы, итог и источники.
- **Исследования/** — собранные материалы со ссылками на источники.
- **Замеры/** — таблицы замеров (цены, метрики и т. п.).
- **Журнал/** — по дню: какие задачи шли и чем кончились.

Заметки агента можно править, переносить и связывать со своими — это ваше хранилище.
"""
INSTRUCTIONS = """# Инструкции для агента Jarvis

Пишите сюда постоянные указания — агент читает их перед каждой задачей. Например:

- Пиши кратко, сначала вывод, потом подробности.
- Источники — только с датой не старше года.
- Цены — в рублях, с датой замера.
"""


def obsidian_config_files() -> list[Path]:
    """Где Obsidian хранит список хранилищ (obsidian.json) на этой системе."""
    home = Path.home()
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        return [Path(appdata) / "obsidian" / "obsidian.json"] if appdata else []
    if sys.platform == "darwin":
        return [home / "Library" / "Application Support" / "obsidian" / "obsidian.json"]
    return [
        home / ".config" / "obsidian" / "obsidian.json",
        home / ".var" / "app" / "md.obsidian.Obsidian" / "config" / "obsidian" / "obsidian.json",
    ]


def obsidian_vaults(files: list[Path] | None = None) -> list[dict[str, Any]]:
    """Хранилища Obsidian: [{path, name, open, ts}] — открытое и недавние первыми; только те, что есть."""
    out: list[dict[str, Any]] = []
    for config in obsidian_config_files() if files is None else files:
        try:
            data = json.loads(config.read_text("utf-8"))
        except (OSError, ValueError):
            continue
        vaults = data.get("vaults") if isinstance(data, dict) else None
        for item in (vaults or {}).values():
            if not isinstance(item, dict) or not item.get("path"):
                continue
            path = Path(str(item["path"]))
            if path.is_dir() and all(v["path"] != str(path) for v in out):
                ts = item.get("ts")
                out.append(
                    {
                        "path": str(path),
                        "name": path.name,
                        "open": bool(item.get("open")),
                        "ts": float(ts) / 1000 if isinstance(ts, int | float) else 0.0,
                    }
                )
    out.sort(key=lambda v: (not v["open"], -v["ts"]))
    return out


def default_folder() -> Path:
    """Нет Obsidian — обычная папка с заметками."""
    if paths.overridden():
        return paths.data_dir() / "vault"
    return Path.home() / "Documents" / "Jarvis"


def safe_name(name: str, limit: int = 90) -> str:
    """Имя файла из заголовка: без запрещённых в Windows знаков, точек и пробелов по краям."""
    text = BAD_NAME.sub(" ", name.replace("/", "-").replace("\\", "-"))
    text = " ".join(text.split()).strip(" .")
    return (text[:limit].rstrip(" .") or "Без названия").replace("#", "№").replace("[", "(").replace("]", ")")


class Vault:
    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        self._detected: list[dict[str, Any]] | None = None

    # --- где хранилище ---------------------------------------------------------------------------------
    @property
    def settings(self) -> Any:
        return self.ctx.settings.value.background

    def vaults(self, refresh: bool = False) -> list[dict[str, Any]]:
        """Хранилища Obsidian на этом ПК (каталог данных задан явно — не ищем: проверка или разработка)."""
        if paths.overridden():
            return []
        if self._detected is None or refresh:
            self._detected = obsidian_vaults()
        return self._detected

    @property
    def root(self) -> Path:
        chosen = (self.settings.vault or "").strip()
        if chosen:
            return Path(os.path.expandvars(os.path.expanduser(chosen)))
        found = self.vaults()
        return Path(found[0]["path"]) if found else default_folder()

    @property
    def folder(self) -> str:
        name = safe_name(self.settings.folder or "Jarvis", 60)
        return name if not name.startswith(".") else "Jarvis"

    @property
    def home(self) -> Path:
        """Своя папка агента в хранилище."""
        return self.root / self.folder

    def obsidian(self) -> bool:
        """Хранилище — хранилище Obsidian (есть .obsidian)."""
        return (self.root / ".obsidian").is_dir()

    def state(self) -> dict[str, Any]:
        root = self.root
        return {
            "path": str(root),
            "folder": self.folder,
            "exists": root.is_dir(),
            "obsidian": self.obsidian(),
            "auto": not (self.settings.vault or "").strip(),
            "vaults": self.vaults(),
        }

    def link(self, rel: str) -> str:
        """Ссылка «открыть в Obsidian» на заметку (путь внутри хранилища)."""
        vault = quote(self.root.name)
        file = quote(rel.removesuffix(".md"))
        return f"obsidian://open?vault={vault}&file={file}"

    # --- среда агента ------------------------------------------------------------------------------------
    def ensure(self) -> None:
        """Своя папка агента и её устройство (создаётся один раз; правки человека не перезаписываются)."""
        home = self.home
        try:
            home.mkdir(parents=True, exist_ok=True)
            for folder in FOLDERS:
                (home / folder).mkdir(exist_ok=True)
            for name, text in (("README.md", README), ("Инструкции.md", INSTRUCTIONS)):
                path = home / name
                if not path.exists():
                    path.write_text(text, encoding="utf-8")
        except OSError as e:
            raise UserFacingError(
                f"Не получается писать в хранилище заметок «{self.root}»: {e}. Проверьте папку в настройках "
                "(Фоновые задачи → Хранилище)"
            ) from e

    def instructions(self) -> str:
        """Постоянные указания человека агенту (Инструкции.md) — без шаблонного текста."""
        try:
            text = (self.home / "Инструкции.md").read_text("utf-8")
        except OSError:
            return ""
        if text.strip() == INSTRUCTIONS.strip():
            return ""
        return text.strip()[:4000]

    # --- пути ------------------------------------------------------------------------------------------
    def rel(self, path: Path) -> str:
        return path.relative_to(self.root.resolve()).as_posix()

    def resolve(self, rel: str, *, write: bool = False) -> Path:
        """Путь внутри хранилища → файл. Запись — только в свою папку (путь без неё — в неё)."""
        raw = (rel or "").strip().replace("\\", "/").lstrip("/")
        if not raw:
            raise UserFacingError("Ошибка: пустой путь")
        parts = [p for p in raw.split("/") if p not in ("", ".")]
        if any(p == ".." or p.startswith(".") for p in parts):
            raise UserFacingError("Ошибка: путь выходит за хранилище или в служебную папку")
        if write:
            if parts[0] != self.folder:
                parts = [self.folder, *parts]
            parts = [*parts[:-1], safe_name(parts[-1], 120)]
            if not parts[-1].lower().endswith(NOTE_EXTS):
                parts[-1] += ".md"
        root = self.root.resolve()
        path = (root / "/".join(parts)).resolve()
        limit = (root / self.folder).resolve() if write or not self.settings.read_vault else root
        if path != limit and limit not in path.parents:
            where = f"папке «{self.folder}»" if limit != root else "хранилище"
            raise UserFacingError(f"Ошибка: можно только в {where}")
        return path

    def locate(self, rel: str) -> str | None:
        """Заметка, которую назвала модель («Исследования/Отчёт», «Jarvis/…/Отчёт.md»): путь внутри хранилища,
        если она есть, иначе None. Пробует как есть, с «.md» и в своей папке."""
        raw = (rel or "").strip().replace("\\", "/").lstrip("/")
        variants = [raw, raw + ".md"]
        if not raw.startswith(self.folder + "/"):
            variants += [f"{self.folder}/{raw}", f"{self.folder}/{raw}.md"]
        for variant in variants:
            try:
                path = self.resolve(variant)
            except UserFacingError:
                continue
            if path.is_file():
                return self.rel(path)
        return None

    # --- чтение, поиск, запись -------------------------------------------------------------------------
    def read(self, rel: str) -> tuple[str, str, bool]:
        """(путь, текст, обрезан ли)."""
        path = self.resolve(rel)
        if not path.is_file():
            raise UserFacingError(f"Ошибка: заметки «{rel}» нет")
        if not path.name.lower().endswith(READ_EXTS):
            raise UserFacingError("Ошибка: это не заметка")
        text = path.read_text("utf-8", errors="replace")
        return self.rel(path), text[:MAX_READ], len(text) > MAX_READ

    def listing(self, rel: str = "") -> list[str]:
        """Папки и заметки в папке (папки — с «/» в конце)."""
        base = self.resolve(rel) if rel.strip(" /") else self.root.resolve()
        if not self.settings.read_vault and not rel.strip(" /"):
            base = self.home.resolve()
        if not base.is_dir():
            raise UserFacingError(f"Ошибка: папки «{rel}» нет")
        out = []
        for item in sorted(base.iterdir(), key=lambda p: (not p.is_dir(), p.name.casefold())):
            if item.name.startswith("."):
                continue
            if item.is_dir():
                out.append(self.rel(item) + "/")
            elif item.name.lower().endswith(READ_EXTS):
                out.append(self.rel(item))
            if len(out) >= 300:
                break
        return out

    def search(self, query: str, limit: int = 10) -> list[dict[str, str]]:
        """Заметки, где есть слова запроса (в имени — весомее): [{path, snippet}], лучшие первыми."""
        words = [w for w in re.split(r"\W+", query.casefold()) if len(w) >= 2][:8]
        if not words:
            raise UserFacingError("Ошибка: пустой запрос")
        start = (self.root if self.settings.read_vault else self.home).resolve()
        scored: list[tuple[int, str, str]] = []
        seen = 0
        for folder, dirs, files in os.walk(start):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for name in files:
                if not name.lower().endswith(NOTE_EXTS):
                    continue
                seen += 1
                if seen > MAX_SCAN_FILES:
                    break
                path = Path(folder) / name
                try:
                    with path.open("r", encoding="utf-8", errors="replace") as f:
                        text = f.read(MAX_SCAN_BYTES)
                except OSError:
                    continue
                low, title = text.casefold(), name.casefold()
                score = sum(low.count(w) + 5 * title.count(w) for w in words)
                if score and all(w in low or w in title for w in words[:3]):
                    at = min((low.find(w) for w in words if w in low), default=0)
                    snippet = one_line(text[max(0, at - 80) : at + 220], 300)
                    scored.append((score, self.rel(path.resolve()), snippet))
        scored.sort(key=lambda s: -s[0])
        return [{"path": p, "snippet": s} for _, p, s in scored[:limit]]

    def write(self, rel: str, content: str, mode: str = "replace") -> str:
        """Записать заметку в своей папке: replace — целиком, append — в конец, create — новую (имя занято —
        с номером). Вернёт путь внутри хранилища."""
        if len(content) > MAX_WRITE:
            raise UserFacingError("Ошибка: слишком длинный текст — раздели на несколько заметок")
        self.ensure()
        path = self.resolve(rel, write=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        if mode == "create" and path.exists():
            for n in range(2, 100):
                candidate = path.with_name(f"{path.stem} {n}{path.suffix}")
                if not candidate.exists():
                    path = candidate
                    break
        if mode == "append" and path.exists():
            with path.open("a", encoding="utf-8") as f:
                old_end = path.stat().st_size
                f.write(("" if old_end == 0 else "\n") + content.rstrip() + "\n")
        else:
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(content.rstrip() + "\n", encoding="utf-8")
            os.replace(tmp, path)
        return self.rel(path)

    # --- заметка задачи и журнал -------------------------------------------------------------------------
    def task_note(self, task: "Task") -> str:
        """Путь заметки задачи (выбирается один раз, при первой записи)."""
        if task.note:
            return task.note
        day = datetime.fromtimestamp(task.created).strftime("%Y-%m-%d")
        name = safe_name(f"{day} {task.title or task.brief}", 90)
        return f"{self.folder}/Задачи/{name}.md"

    def save_task(self, task: "Task") -> str:
        """Заметка задачи целиком заново (её ведёт Jarvis): свойства, бриф, план, ход работы, вопросы, итог,
        источники."""
        rel = self.task_note(task)
        self.ensure()
        path = self.resolve(rel, write=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(render_task(task, self), encoding="utf-8")
        os.replace(tmp, path)
        return self.rel(path)

    def journal(self, task: "Task") -> None:
        """Строка о законченной задаче — в журнал дня."""
        day = datetime.now().strftime("%Y-%m-%d")
        icon = STATUS_ICON.get(task.status, "•")
        name = Path(task.note).stem if task.note else one_line(task.title, 80)
        line = f"- {datetime.now():%H:%M} {icon} [[{name}]] — {one_line(task.summary or task.error, 200)}"
        with contextlib.suppress(OSError, UserFacingError):
            path = self.resolve(f"{self.folder}/Журнал/{day}.md", write=True)
            path.parent.mkdir(parents=True, exist_ok=True)
            fresh = not path.exists()
            with path.open("a", encoding="utf-8") as f:
                if fresh:
                    f.write(f"# Журнал Jarvis — {day}\n\n")
                f.write(line + "\n")


def _yaml(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _when(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else ""


def render_task(task: "Task", vault: Vault) -> str:
    """Заметка задачи для Obsidian."""
    lines = [
        "---",
        "type: jarvis-task",
        f"status: {_yaml(STATUS_WORDS.get(task.status, task.status))}",
        f"created: {_when(task.created)}",
        f"updated: {_when(time.time())}",
    ]
    if task.finished:
        lines.append(f"finished: {_when(task.finished)}")
    lines += [f"sources: {len(task.sources)}", f"steps: {task.steps}", "tags: [jarvis]", "---", ""]
    lines += [f"# {one_line(task.title or task.brief, 120)}", "", "> [!info] Задача"]
    lines += [f"> {row}" if row.strip() else ">" for row in task.brief.strip().splitlines()]
    if task.deliverable:
        lines += [">", f"> **Нужен результат:** {one_line(task.deliverable, 300)}"]
    lines.append("")
    if task.summary or task.error:
        lines += ["## Итог", "", (task.summary or task.error).strip(), ""]
        if task.result:
            lines += [f"Результат: [[{task.result.removesuffix('.md')}]]", ""]
    if task.plan:
        lines += ["## План", ""]
        lines += [
            f"- [{'x' if s.get('done') else ' '}] {one_line(str(s.get('text') or ''), 300)}"
            for s in task.plan
        ]
        lines.append("")
    asked = [q for q in task.questions]
    if asked:
        lines += ["## Вопросы", ""]
        for q in asked:
            answer = q.answer if q.answered else "_ждёт ответа_"
            lines.append(f"- **{one_line(q.text, 300)}** — {one_line(answer, 300)}")
        lines.append("")
    progress = [e for e in task.log if e.kind != "action"]
    if progress:
        lines += ["## Ход работы", ""]
        for entry in progress:
            mark = "⚠️ " if entry.kind == "error" else ""
            lines.append(
                f"- {datetime.fromtimestamp(entry.ts):%d.%m %H:%M} — {mark}{one_line(entry.text, 500)}"
            )
        lines.append("")
    if task.sources:
        lines += ["## Источники", ""]
        for source in task.sources:
            title = one_line(source.get("title") or source["url"], 160).replace("[", "(").replace("]", ")")
            lines.append(f"- [{title}]({source['url']})")
        lines.append("")
    return "\n".join(lines)
