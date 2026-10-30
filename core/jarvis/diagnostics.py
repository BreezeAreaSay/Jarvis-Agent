"""Диагностика для настроек («Система» → «Диагностика») и для отчёта о проблеме.

GET /api/diagnostics — пункты {id, title, status: ok | warn | fail | off, detail, hint, action} и report — тот
же список текстом: его человек копирует в сообщение об ошибке. В отчёте нет ключей (redact) и личной папки
(C:\\Users\\<имя> → ~). POST /api/diagnostics/crashes/reset — забыть аварийные остановки (crashguard): этапы,
на которых ядро падало, снова пробуются.
"""

import contextlib
import platform
import shutil
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jarvis import __version__, crashguard, paths
from jarvis.redact import redact

if TYPE_CHECKING:
    from jarvis.context import AppContext

LOW_DISK_BYTES = 1 << 30  # меньше гигабайта свободно — модели и журналы не поместятся
STATUS_MARK = {"ok": "✓", "warn": "!", "fail": "✗", "off": "–"}
# этапы crashguard → по-человечески
STAGE_TITLES = {"ru-dict": "голос со словарём ударений"}


def _item(id: str, title: str, status: str, detail: str, hint: str = "", action: str = "") -> dict[str, str]:
    return {"id": id, "title": title, "status": status, "detail": detail, "hint": hint, "action": action}


def _safe(fn: Any, *args: Any) -> Any:
    with contextlib.suppress(Exception):
        return fn(*args)
    return None


def _home(text: str) -> str:
    """Личная папка в путях → «~» (в отчёте не нужно имя пользователя)."""
    home = str(Path.home())
    return text.replace(home, "~") if home and home not in ("/", "\\") else text


def _size(path: Path) -> int:
    total = 0
    with contextlib.suppress(OSError):
        for item in path.rglob("*"):
            with contextlib.suppress(OSError):
                if item.is_file():
                    total += item.stat().st_size
    return total


def _core(ctx: "AppContext") -> dict[str, str]:
    data = paths.data_dir()
    system = f"{platform.system()} {platform.release()}"
    detail = (
        f"Jarvis {__version__}, Python {platform.python_version()}, {system}, "
        f"платформа {getattr(ctx.platform, 'name', '?')}"
    )
    free = _safe(lambda: shutil.disk_usage(data).free)
    if isinstance(free, int):
        detail += f"; свободно {free / (1 << 30):.1f} ГБ"
        if free < LOW_DISK_BYTES:
            return _item(
                "core", "Ядро", "warn", detail, "Мало места на диске: модели и журналы могут не поместиться"
            )
    return _item("core", "Ядро", "ok", detail)


def _ai(ctx: "AppContext") -> list[dict[str, str]]:
    ai = getattr(ctx, "ai", None)
    settings = ctx.settings.value.ai
    if ai is None:
        return [_item("ai", "Jarvis AI", "fail", "сервис ИИ не запущен")]
    if not settings.enabled:
        return [_item("ai", "Jarvis AI", "off", "выключен в настройках")]
    out = []
    backend = _safe(lambda: ai.account_backend(routed=False)) if hasattr(ai, "account_backend") else None
    problem = _safe(backend.problem) if backend is not None else _safe(ai.llm.problem)
    preset = _safe(lambda: ai.llm.primary_config().preset.title) or settings.provider
    model = settings.model or "модель по умолчанию"
    if problem:
        out.append(_item("ai", "Jarvis AI", "fail", f"{preset}, {model}: {problem}", "Настройки → Jarvis AI"))
    else:
        out.append(_item("ai", "Jarvis AI", "ok", f"{preset}, {model}"))
    status = _safe(ai.llm.fallback_status) or {}
    if not settings.fallback_provider:
        out.append(
            _item("fallback", "Запасной ИИ", "off", "не выбран", "Настройки → Jarvis AI → «Запасной ИИ»")
        )
    elif not status.get("configured"):
        out.append(
            _item("fallback", "Запасной ИИ", "warn", f"{settings.fallback_provider}: нет ключа или адреса")
        )
    else:
        paused = status.get("paused") or []
        detail = f"{status.get('provider')}, {status.get('model')}"
        if paused:
            p = paused[0]
            detail += (
                f"; сейчас отвечает он: {p['provider']} — {p['text']}, ещё {max(1, p['seconds'] // 60)} мин"
            )
        out.append(_item("fallback", "Запасной ИИ", "warn" if paused else "ok", detail))
    return out


def _voice(ctx: "AppContext") -> list[dict[str, str]]:
    out = []
    voice = _safe(ctx.voice.status) or {}
    if not voice.get("enabled", True):
        out.append(_item("asr", "Распознавание речи", "off", "микрофон выключен"))
    elif voice.get("error"):
        out.append(_item("asr", "Распознавание речи", "fail", str(voice["error"]), "Настройки → Голос"))
    else:
        state = voice.get("state") or "?"
        out.append(_item("asr", "Распознавание речи", "ok", f"состояние: {state}"))
    tts = _safe(ctx.tts.status) if hasattr(ctx.tts, "status") else None
    if isinstance(tts, dict):
        engine = tts.get("engine", "")
        problem = (tts.get("engines") or {}).get(engine, "")
        name = f"{engine}" + (
            f" ({tts.get('piper_voice')})" if engine == "piper" and tts.get("piper_voice") else ""
        )
        if problem:
            out.append(_item("tts", "Голос Jarvis", "warn", f"{name}: {problem}", "Говорит запасной голос"))
        else:
            out.append(_item("tts", "Голос Jarvis", "ok", name))
        stress = tts.get("stress_dict") or {}
        if not ctx.settings.value.tts.stress_dict:
            out.append(_item("stress", "Словарь ударений", "off", "выключен в настройках голоса"))
        elif not stress.get("bundled"):
            out.append(_item("stress", "Словарь ударений", "off", "нет в этой сборке"))
        elif stress.get("loaded") == "espeak":
            hint = "Голос с ним ронял ядро или данные голоса другие — подробности в журнале"
            out.append(
                _item("stress", "Словарь ударений", "warn", "голос говорит со словарём по умолчанию", hint)
            )
        else:
            loaded = "у голоса" if stress.get("loaded") == "jarvis" else "поставится при загрузке голоса"
            out.append(_item("stress", "Словарь ударений", "ok", loaded))
    return out


def _crashes() -> dict[str, str]:
    crashed = _safe(crashguard.crashed) or {}
    if not crashed:
        return _item("crashes", "Аварийные остановки", "ok", "не было")
    parts = [f"{STAGE_TITLES.get(stage, stage)} — {count} раз" for stage, count in sorted(crashed.items())]
    blocked = any(count >= crashguard.LIMIT for count in crashed.values())
    hint = (
        "Эти части пропускаются, чтобы ядро не падало снова. «Попробовать снова» — после обновления или "
        "повторного скачивания модели"
        if blocked
        else "Ядро падало при загрузке — следующий сбой выключит эту часть"
    )
    return _item(
        "crashes", "Аварийные остановки", "fail" if blocked else "warn", "; ".join(parts), hint, "reset"
    )


def _background(ctx: "AppContext") -> list[dict[str, str]]:
    out = []
    background = getattr(ctx, "background", None)
    vault = _safe(background.vault.state) if background is not None else None
    if isinstance(vault, dict):
        where = "хранилище Obsidian" if vault.get("obsidian") else "папка заметок"
        if vault.get("exists"):
            out.append(
                _item("vault", "Заметки фоновых задач", "ok", f"{where}: {_home(str(vault.get('path')))}")
            )
        else:
            out.append(
                _item(
                    "vault",
                    "Заметки фоновых задач",
                    "warn",
                    f"{where} ещё не создана: {_home(str(vault.get('path')))}",
                    "Создастся с первой задачей или кнопкой в настройках фоновых задач",
                )
            )
    browser = _safe(ctx.agent_browser.state) if hasattr(ctx, "agent_browser") else None
    if isinstance(browser, dict):
        if not ctx.settings.value.ai.browser and not browser.get("found"):
            out.append(_item("browser", "Браузер агента", "off", "не найден Chrome или Edge"))
        elif browser.get("error"):
            out.append(_item("browser", "Браузер агента", "warn", str(browser["error"])))
        else:
            app = browser.get("app") or "Chrome или Edge"
            out.append(_item("browser", "Браузер агента", "ok", f"{app}: {browser.get('status', '?')}"))
    return out


def _remote(ctx: "AppContext") -> list[dict[str, str]]:
    info = _safe(ctx.remote.info) if hasattr(ctx, "remote") else None
    if not isinstance(info, dict):
        return []
    telegram = info.get("telegram") or {}
    if not telegram.get("enabled"):
        return [_item("telegram", "Пульт в Telegram", "off", "выключен")]
    if telegram.get("error"):
        return [_item("telegram", "Пульт в Telegram", "fail", str(telegram["error"]))]
    bot = f"@{telegram['bot']}" if telegram.get("bot") else "бот"
    return [_item("telegram", "Пульт в Telegram", "ok", f"{bot}: {telegram.get('status', '?')}")]


def _logs() -> dict[str, str]:
    folder = paths.logs_dir()
    size = _size(folder)
    return _item("logs", "Журналы", "ok", f"{_home(str(folder))}, {size / (1 << 20):.1f} МБ")


def collect(ctx: "AppContext") -> dict[str, Any]:
    """Пункты диагностики и отчёт текстом (без ключей и личной папки)."""
    items: list[dict[str, str]] = [_core(ctx)]
    for part in (_ai, _voice, _background, _remote):
        result = _safe(part, ctx)
        if result:
            items.extend(result)
    items += [_crashes(), _logs()]
    lines = [f"Jarvis {__version__} — диагностика ({sys.platform})"]
    for item in items:
        line = f"{STATUS_MARK.get(item['status'], '?')} {item['title']}: {item['detail']}"
        lines.append(_home(redact(line)))
    return {"items": items, "report": "\n".join(lines)}


def reset_crashes() -> None:
    crashguard.reset()
