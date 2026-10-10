"""`jarvis doctor [--brain]`: проверка окружения — ✓/⚠/✗ и подсказка, что сделать.

Каждая проверка — отдельная функция (Doctor) -> Check(status, text, hint); проверки независимы: упавшая
печатается как ✗, остальные идут дальше. В локальном режиме codex не запускается вообще.
Сервер рук doctor не запускает: только читает уже запущенный (/health, /props, тёплый запрос).
"""

import json
import os
import re
import secrets
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from typing import Any, Literal, NamedTuple

import httpx
import psutil

from pc import settings, subproc

Status = Literal["ok", "warn", "fail", "skip"]
MARKS = {"ok": "✓", "warn": "⚠", "fail": "✗", "skip": "–"}

LLAMA_EXE = r"C:\llama\llama-server.exe"
ES_MIN = (1, 1, 0, 37)
ES_TIMEOUT_S = 15.0
ES_NOT_RUNNING = 8  # код выхода es.exe: Everything не запущен
APPS_STALE_DAYS = 7
NON_ASCII_DATA = (
    "в пути данных не-ASCII символы: codex 0.160.1 с таким CODEX_HOME не работает — задай JARVIS_DATA_DIR, "
    "например C:\\JarvisData, и войди заново (команда из S5)"
)


class Check(NamedTuple):
    status: Status
    text: str
    hint: str = ""


@dataclass
class Doctor:
    """Общее для проверок: конфиг, флаг --brain и кэш результатов (health читается один раз)."""

    cfg: Any
    brain: bool = False
    cache: dict[str, Any] = field(default_factory=dict)


def _client() -> httpx.Client:
    """HTTP к серверу рук; тесты подменяют на httpx.MockTransport. Без системного прокси (127.0.0.1)."""
    return httpx.Client(timeout=5.0, trust_env=False)


# --- конфиг, данные, установка ------------------------------------------------------------------


def check_config(d: Doctor) -> Check:
    path = settings.config_path()
    error = settings.config_error()
    if error:
        return Check(
            "fail", f"конфиг не разобран: {error}", "исправь синтаксис TOML (пример — jarvis.example.toml)"
        )
    if not path.is_file():
        example = settings.app_root() / "jarvis.example.toml"
        return Check("warn", f"конфига нет: {path} — значения по умолчанию", f"скопируй {example} в {path}")
    return Check("ok", f"конфиг: {path} (режим {d.cfg.mode})")


def check_data_dir(d: Doctor) -> Check:
    path = str(settings.data_dir())
    if not path.isascii():
        return Check("warn", f"каталог данных: {path} — {NON_ASCII_DATA}", login_command())
    source = "JARVIS_DATA_DIR" if os.environ.get("JARVIS_DATA_DIR", "").strip() else "по умолчанию"
    return Check("ok", f"каталог данных: {path} ({source})")


def check_install(d: Doctor) -> Check:
    if not settings.is_frozen():
        return Check("ok", f"запуск из исходников: {settings.app_root()}")
    root = settings.app_root()
    need = ["scripts/start_hands.cmd", "jarvis.example.toml", "bench/phrases.ru.jsonl"]
    missing = [p for p in need if not (root / p).is_file()]
    es = settings.install_dir() / "bin" / "es.exe"
    extra = "" if es.is_file() else " (bin\\es.exe не в комплекте — es_path из конфига)"
    if missing:
        return Check(
            "fail", f"установка {settings.install_dir()}: нет {', '.join(missing)}", "переустанови Jarvis"
        )
    return Check("ok", f"установка: {settings.install_dir()}, файлы комплекта на месте{extra}")


def check_mode_hotkey(d: Doctor) -> Check:
    hotkey = d.cfg.ui.hotkey
    try:
        from jarvis import winapp

        winapp.parse_hotkey(hotkey)
    except Exception as e:
        return Check(
            "fail", f"хоткей «{hotkey}» не разобран: {e}", "поправь [ui] hotkey, например ctrl+alt+space"
        )
    running = jarvis_running()
    where = (
        "зарегистрирован ли — видно в подсказке трея и в логе jarvis run"
        if running
        else "jarvis run не запущен — хоткей сейчас не зарегистрирован, doctor этого не знает"
    )
    return Check("ok", f"режим {d.cfg.mode}; хоткей {hotkey} ({where})")


def jarvis_running() -> bool:
    """Запущен ли `jarvis run` / Jarvis.exe: в командной строке за «jarvis» идёт run (или ничего)."""
    me = os.getpid()
    for p in psutil.process_iter(["pid", "cmdline"]):
        try:
            if p.info["pid"] == me:
                continue
            cmd = [str(a) for a in (p.info.get("cmdline") or [])]
        except (psutil.Error, OSError):
            continue
        for i, arg in enumerate(cmd):
            if PureWindowsPath(arg).name.casefold().removesuffix(".exe") == "jarvis":
                rest = cmd[i + 1 :]
                if not rest or rest[0] == "run":
                    return True
                break
    return False


def check_autostart(d: Doctor) -> Check:
    from jarvis import winapp

    if winapp.autostart_enabled():
        return Check("ok", "автозапуск при входе в Windows включён")
    return Check("warn", "автозапуск выключен", "jarvis autostart on")


def check_apps_cache(d: Doctor) -> Check:
    from pc import apps

    path = apps.cache_path()
    if not path.is_file():
        return Check(
            "warn", "кэша приложений нет", "jarvis apps --refresh (или запусти jarvis run — обновит в фоне)"
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
        count = len(raw.get("apps", [])) if isinstance(raw, dict) else len(raw)
        updated = raw.get("updated") if isinstance(raw, dict) else None
    except (OSError, ValueError, AttributeError) as e:
        return Check("fail", f"кэш приложений не читается: {e}", "jarvis apps --refresh")
    ts = float(updated) if isinstance(updated, int | float) else path.stat().st_mtime
    age_h = max(0.0, (time.time() - ts) / 3600)
    age = f"{age_h:.0f} ч" if age_h < 48 else f"{age_h / 24:.0f} дн."
    if not count:
        return Check("fail", "кэш приложений пуст", "jarvis apps --refresh")
    if age_h > APPS_STALE_DAYS * 24:
        return Check("warn", f"кэш приложений: {count} шт., обновлён {age} назад", "jarvis apps --refresh")
    return Check("ok", f"кэш приложений: {count} шт., обновлён {age} назад")


# --- руки (llama-server) ------------------------------------------------------------------------


def _url(d: Doctor) -> str:
    return str(d.cfg.hands.url).rstrip("/")


def _health(d: Doctor) -> int | None:
    if "health" not in d.cache:
        try:
            with _client() as c:
                d.cache["health"] = c.get(f"{_url(d)}/health").status_code
        except httpx.HTTPError:
            d.cache["health"] = None
    return d.cache["health"]


HANDS_START = "запусти scripts\\start_hands.cmd 4b (или jarvis run — он поднимет сервер сам)"


def check_hands_health(d: Doctor) -> Check:
    code = _health(d)
    if code == 200:
        return Check("ok", f"сервер рук отвечает: {d.cfg.hands.url}")
    if code == 503:
        return Check("warn", "сервер рук грузит модель (503)", "подожди несколько секунд и повтори")
    if code is None:
        return Check("fail", f"сервер рук не отвечает: {d.cfg.hands.url}", HANDS_START)
    return Check("fail", f"сервер рук: /health → HTTP {code}", "смотри C:\\Jarvis\\logs\\hands.log")


def check_hands_props(d: Doctor) -> Check:
    if _health(d) != 200:
        return Check("skip", "модель рук: сервер не готов")
    with _client() as c:
        props = c.get(f"{_url(d)}/props").json()
    model_path = str(props.get("model_path", "?"))
    n_ctx = (props.get("default_generation_settings") or {}).get("n_ctx", "?")
    name = PureWindowsPath(model_path).name
    want = d.cfg.hands.model.casefold()
    if want and want not in name.casefold().replace("-", "").replace("_", ""):
        return Check(
            "warn",
            f"модель рук: {name}, n_ctx {n_ctx} — а в конфиге hands.model = {d.cfg.hands.model}",
            "перезапусти сервер рук с нужной моделью (трей → «Перезапустить руки»)",
        )
    return Check("ok", f"модель рук: {name}, n_ctx {n_ctx}")


def _port_owner(port: int) -> tuple[bool, str | None]:
    """(кто-то слушает порт, exe владельца или None)."""
    for c in psutil.net_connections(kind="tcp"):
        if c.status == psutil.CONN_LISTEN and c.laddr and c.laddr[1] == port:
            if not c.pid:
                return True, None
            try:
                return True, psutil.Process(c.pid).exe() or None
            except (psutil.Error, OSError):
                return True, None
    return False, None


def check_hands_owner(d: Doctor) -> Check:
    port = httpx.URL(d.cfg.hands.url).port or 8081
    try:
        listening, exe = _port_owner(port)
    except (psutil.Error, OSError) as e:
        return Check("warn", f"владелец порта {port} не определён: {e}")
    if not listening:
        return Check(
            "fail" if _health(d) is not None else "skip", f"порт {port} никто не слушает", HANDS_START
        )
    if exe is None:
        return Check("warn", f"порт {port}: владелец неизвестен (нет доступа к процессу)")
    if PureWindowsPath(exe) == PureWindowsPath(LLAMA_EXE):
        return Check("ok", f"порт {port}: {LLAMA_EXE}")
    return Check(
        "fail",
        f"порт {port} занят чужим процессом: {exe}",
        "закрой его (Ollama? другой llama-server?): свой сервер Jarvis — C:\\llama\\llama-server.exe",
    )


def check_hands_warm(d: Doctor) -> Check:
    """Тёплый запрос через Hands.warmup(): кэш префикса (cache_n ≥ префикс + 3) и скорость генерации."""
    if _health(d) != 200:
        return Check("skip", "тёплый запрос рук: сервер не готов")
    from jarvis.hands import Hands

    hands = Hands(d.cfg.hands)
    try:
        hands.warmup()
    except Exception as e:
        return Check("fail", f"тёплый запрос рук не прошёл: {e}", HANDS_START)
    finally:
        hands.close()
    st = dict(hands.status)
    floor = d.cfg.hands.min_tokens_per_s.get(d.cfg.hands.model)
    detail = f"префикс {st.get('prefix_tokens')} токенов, {st.get('tps')} т/с (порог {floor})"
    if st.get("server") != "ready":
        return Check(
            "fail", f"тёплый запрос рук не прошёл: {st.get('error') or st.get('server')}", HANDS_START
        )
    if st.get("prefix_cache") == "broken":
        return Check(
            "fail",
            f"кэш префикса рук сломан: cache_n < префикс + 3 ({detail})",
            "SYSTEM и TOOLS рук должны быть байт-в-байт одинаковыми; сервер — с -np 1",
        )
    if st.get("vram") == "slow":
        return Check(
            "warn",
            f"руки медленные — VRAM переполнена ({detail})",
            "закрой игры, Ollama и прочее, что держит видеопамять; или hands.model = 4b",
        )
    if st.get("prefix_cache") != "ok" or st.get("vram") != "ok":
        return Check(
            "warn", f"тёплый запрос рук: проверить не удалось ({detail})", "смотри лог jarvis-cli.log"
        )
    return Check("ok", f"руки тёплые: кэш префикса работает, {detail}")


def check_ollama(d: Doctor) -> Check:
    """Ollama держит VRAM; ollama CLI не вызываем — он сам запускает Ollama."""
    names = set()
    for p in psutil.process_iter(["name"]):
        try:
            name = p.info.get("name") or ""
        except (psutil.Error, OSError):
            continue
        if name.casefold().startswith("ollama"):
            names.add(name)
    if names:
        return Check(
            "warn",
            f"запущена Ollama ({', '.join(sorted(names))}): она держит видеопамять — руки станут медленными",
            "выйди из Ollama в трее; OLLAMA_KEEP_ALIVE=0 выгружает модели сразу",
        )
    return Check("ok", "Ollama не запущена")


# --- Everything (как scripts/es_check.py) -------------------------------------------------------


def _es(*args: str) -> tuple[int, str]:
    """es.exe с -cp 65001 (кириллица) через pc.subproc; (код, вывод)."""
    r = subproc.run([str(settings.es_path()), "-cp", "65001", *args], timeout=ES_TIMEOUT_S)
    out = r.stdout.decode("utf-8", "replace").strip() or r.stderr.decode("utf-8", "replace").strip()
    return r.returncode, out


def _es_version(text: str) -> tuple[int, ...] | None:
    m = re.search(r"(\d+)\.(\d+)\.(\d+)\.(\d+)", text)
    return tuple(int(x) for x in m.groups()) if m else None


ES_HINT = "положи ES ≥ 1.1.0.37 в C:\\Jarvis\\bin\\es.exe или укажи [pc] es_path в jarvis.toml"


def check_es_version(d: Doctor) -> Check:
    path = settings.es_path()
    if not path.is_file():
        d.cache["es"] = False
        return Check("fail", f"es.exe не найден: {path}", ES_HINT)
    code, out = _es("-version")
    version = _es_version(out)
    d.cache["es"] = version is not None
    if version is None:
        return Check("fail", f"es.exe не ответил версией (код {code}): {out[:100]}", ES_HINT)
    text = ".".join(map(str, version))
    if version < ES_MIN:
        return Check("fail", f"es.exe {text} — старый (нужен ≥ 1.1.0.37: -cp 65001)", ES_HINT)
    return Check("ok", f"es.exe {text}: {path}")


def check_everything_running(d: Doctor) -> Check:
    if d.cache.get("es") is False:
        return Check("skip", "Everything: нет es.exe")
    code, out = _es("-get-everything-version")
    d.cache["everything"] = code == 0
    if code == ES_NOT_RUNNING:
        return Check("fail", "Everything не запущен (код 8)", "запусти Everything и включи его автозапуск")
    if code != 0:
        return Check("fail", f"Everything: сбой связи (код {code}) {out[:80]}", "перезапусти Everything")
    return Check("ok", f"Everything {out} запущен")


def _count(path: str) -> int:
    code, out = _es("-get-result-count", "-path", path)
    try:
        return int(out.split()[0]) if code == 0 and out else 0
    except (ValueError, IndexError):
        return 0


def check_everything_index(d: Doctor) -> Check:
    if not d.cache.get("everything"):
        return Check("skip", "индекс Everything: Everything не отвечает")
    if _count("C:\\Windows") > 0:
        return Check("ok", "весь C: в индексе")
    profile = os.environ.get("USERPROFILE", "") or str(Path.home())
    if _count(profile) > 0:
        return Check("ok", "режим папок: поиск только в проиндексированных папках")
    return Check(
        "fail",
        "индекс Everything пуст и для C:\\Windows, и для профиля",
        "служба Everything или индексы папок: Сервис → Параметры → Индексы (NTFS или «Папки»)",
    )


def check_everything_cyrillic(d: Doctor) -> Check:
    """Файл с кириллицей в имени (во временной папке в data_dir) находится по точному пути."""
    if not d.cache.get("everything"):
        return Check("skip", "кириллица в Everything: Everything не отвечает")
    folder = settings.data_dir() / "doctor"
    folder.mkdir(parents=True, exist_ok=True)
    probe = folder / f"Тест_ёЁ_поиска_{secrets.token_hex(3)}.txt"
    probe.touch()
    try:
        found = False
        for _ in range(8):  # Everything подхватывает новый файл не мгновенно
            code, out = _es("-n", "5", probe.stem)
            found = code == 0 and any(
                line.strip().casefold() == str(probe).casefold() for line in out.splitlines()
            )
            if found:
                break
            time.sleep(0.5)
    finally:
        probe.unlink(missing_ok=True)
    if found:
        return Check("ok", "кириллица: es.exe нашёл файл «Тест_ёЁ_поиска…» по точному пути")
    return Check(
        "fail",
        f"кириллица: файл в {folder} не найден по точному пути",
        "нужен ES ≥ 1.1.0.37 (-cp 65001); в режиме папок добавь каталог данных в индекс Everything",
    )


# --- мозг ---------------------------------------------------------------------------------------


def login_command() -> str:
    """Команда входа мозга (PowerShell). В exe — codex.exe из бандла, в разработке — codex из PATH."""
    codex = "codex"
    if settings.is_frozen():
        try:
            from jarvis.brain import codex_bin

            codex = f"& '{codex_bin()}'"
        except Exception:
            codex = "& '<папка Jarvis>\\_internal\\codex_cli_bin\\bin\\codex.exe'"
    return (
        '$d = if ($env:JARVIS_DATA_DIR) { $env:JARVIS_DATA_DIR } else { "$env:LOCALAPPDATA\\Jarvis" }; '
        'New-Item -ItemType Directory -Force "$d\\codex-home" | Out-Null; '
        '$env:CODEX_HOME = "$d\\codex-home"; '
        f"{codex} login; Remove-Item Env:CODEX_HOME"
    )


def check_brain_login(d: Doctor) -> Check:
    if d.cfg.mode == "local":
        text = "мозг: локальный режим — codex не запускается"
        if d.brain:
            return Check("warn", f"{text}; --brain не выполняется", "выключи «Локальный режим» в трее")
        return Check("ok", text)
    from jarvis.brain import codex_bin, codex_env, codex_home

    home = codex_home()
    if not home.is_dir():
        return Check("fail", f"мозг: вход не выполнен — нет папки {home}", login_command())
    env = {**os.environ, **codex_env(d.cfg.brain)}
    r = subproc.run([str(codex_bin()), "login", "status"], timeout=30, env=env)
    out = (r.stderr.decode("utf-8", "replace") + "\n" + r.stdout.decode("utf-8", "replace")).strip()
    # строки «WARNING: …» (алиасы PATH) — шум; первая содержательная строка — итог
    lines = [line.strip() for line in out.splitlines() if line.strip() and not line.startswith("WARNING")]
    first = lines[0][:160] if lines else ""
    d.cache["login"] = r.returncode == 0 and "Logged in using ChatGPT" in out
    if d.cache["login"]:
        return Check("ok", f"мозг: вход выполнен ({first})")
    if "Not logged in" in out:
        return Check("fail", "мозг: вход не выполнен (Not logged in)", login_command())
    if "Error loading configuration" in out:
        bad = next((line for line in lines if "Error loading configuration" in line), first)
        return Check("fail", f"мозг: {bad[:200]}", f"почини config.toml в {home} (или удали его)")
    if r.returncode == 0:
        return Check("warn", f"мозг: {first}", "Jarvis рассчитан на вход через ChatGPT: " + login_command())
    return Check("fail", f"мозг: codex login status — код {r.returncode}: {first}", login_command())


def check_brain_turn(d: Doctor) -> Check:
    """--brain: один тестовый ход (тратит квоту) — до первых слов и всего."""
    if not d.brain:
        return Check("skip", "тестовый ход GPT: только с --brain (тратит квоту)")
    if d.cfg.mode == "local":
        return Check("skip", "тестовый ход GPT: локальный режим — codex не запускается")
    if not d.cache.get("login"):
        return Check("skip", "тестовый ход GPT: сначала вход")
    from jarvis.brain import Brain
    from jarvis.context import Context
    from jarvis.events import Done, TextChunk
    from pc.confirm_client import ConfirmServer

    server = ConfirmServer(lambda summary, details, caller: False)  # действий не ждём — всё «нет»
    server.start()
    brain = Brain(d.cfg.brain, confirm_address=server.address)
    t0 = time.perf_counter()
    first: float | None = None
    done: Any = None
    try:
        brain.start()
        for ev in brain.ask("Ответь одним словом: готов?", Context()):
            if isinstance(ev, TextChunk) and ev.text and first is None:
                first = time.perf_counter() - t0
            if isinstance(ev, Done):
                done = ev
    finally:
        brain.close()
        server.close()
    total = time.perf_counter() - t0
    first_s = f"{first:.1f} с" if first is not None else "нет текста"
    text = f"до первых слов {first_s}, всего {total:.1f} с (холодный старт процесса codex)"
    if done is None or not done.ok:
        return Check("fail", f"тестовый ход GPT не удался: {done.text if done else 'нет ответа'}; {text}")
    return Check("ok", f"тестовый ход GPT: {text}")


CHECKS: list[Callable[[Doctor], Check]] = [
    check_config,
    check_data_dir,
    check_install,
    check_mode_hotkey,
    check_autostart,
    check_apps_cache,
    check_hands_health,
    check_hands_props,
    check_hands_owner,
    check_hands_warm,
    check_ollama,
    check_es_version,
    check_everything_running,
    check_everything_index,
    check_everything_cyrillic,
    check_brain_login,
    check_brain_turn,
]


def run_checks(d: Doctor, checks: list[Callable[[Doctor], Check]] | None = None) -> list[Check]:
    """Все проверки по очереди; исключение одной — её ✗, остальные идут."""
    out = []
    for fn in checks or CHECKS:
        try:
            res = fn(d)
        except Exception as e:
            name = fn.__name__.removeprefix("check_")
            res = Check("fail", f"{name}: проверка упала: {type(e).__name__}: {e}")
        out.append(res)
    return out


def _print(text: str) -> None:
    if sys.stdout is not None:
        print(text, flush=True)


def main(brain: bool = False) -> int:
    """Печать по мере проверки; код 1, если есть ✗."""
    from jarvis import config

    d = Doctor(cfg=config.load(), brain=brain)
    results = []
    for fn in CHECKS:
        res = run_checks(d, [fn])[0]
        results.append(res)
        _print(f"{MARKS[res.status]} {res.text}")
        if res.hint and res.status in ("warn", "fail"):
            _print(f"    → {res.hint}")
    n = {s: sum(r.status == s for r in results) for s in MARKS}
    _print(f"\nитог: ✓ {n['ok']}, ⚠ {n['warn']}, ✗ {n['fail']}")
    return 1 if n["fail"] else 0
