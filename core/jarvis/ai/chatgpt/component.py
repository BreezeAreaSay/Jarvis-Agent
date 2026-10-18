"""Компонент «ChatGPT (подписка)»: SDK openai-codex с бинарём Codex, его папки и настройки запуска.

SDK (~150 МБ: openai_codex + codex_cli_bin с codex.exe) не входит в установщик — скачивается по кнопке в
<data>/pylib (jarvis.extras). Версия закреплена: модуль runtime опирается на внутренности SDK (поток чтения
stdout, маршрутизатор ходов) — их сверяли с 0.163.0.

CODEX_HOME — <data>/codex-home: там лежит вход в ChatGPT (auth.json). Прежний Jarvis держал вход там же,
поэтому готовый вход подхватывается сам. Рабочая папка Codex — пустая <data>/chatgpt: иначе Codex
подмешивает в ход AGENTS.md и файлы из текущей папки.
"""

import contextlib
import importlib
import importlib.util
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from jarvis import extras, paths

if TYPE_CHECKING:
    from jarvis.settings import AiSettings

log = logging.getLogger(__name__)

SDK_VERSION = "0.163.0"
SDK_SPEC = f"openai-codex=={SDK_VERSION}"
NO_PROXY = "127.0.0.1,localhost"
THREAD_UNLOAD_S = 30 * 60  # Codex выгружает простаивающий тред из памяти (потом — thread/resume с диска)


def codex_home() -> Path:
    """CODEX_HOME: вход в ChatGPT (auth.json), сохранённые разговоры (sessions), журналы Codex."""
    return paths.sub_dir("codex-home")


def work_dir() -> Path:
    """Пустая рабочая папка Codex."""
    return paths.sub_dir("chatgpt")


def auth_file() -> Path:
    return codex_home() / "auth.json"


def has_auth_file() -> bool:
    """Быстрая проверка «вход сделан» без запуска Codex (точный ответ даёт account/read)."""
    with contextlib.suppress(OSError):
        return auth_file().is_file()
    return False


@dataclass(frozen=True)
class SdkInfo:
    installed: bool
    version: str = ""
    binary: str = ""
    error: str = ""


def _package_dir(name: str) -> Path | None:
    spec = importlib.util.find_spec(name)
    if spec is None or not spec.submodule_search_locations:
        return None
    return Path(next(iter(spec.submodule_search_locations))).resolve()


def find_sdk() -> SdkInfo:
    """Есть ли SDK и бинарь Codex (после extras.ensure_on_path). openai_codex здесь не импортируется."""
    extras.ensure_on_path()
    importlib.invalidate_caches()
    try:
        sdk = _package_dir("openai_codex")
        bin_pkg = _package_dir("codex_cli_bin")
    except (ImportError, ValueError) as e:
        return SdkInfo(False, error=f"компонент повреждён: {e}")
    if sdk is None:
        return SdkInfo(False)
    if bin_pkg is None:
        return SdkInfo(
            False, error="нет пакета openai-codex-cli-bin с бинарём Codex — скачайте компонент заново"
        )
    exe = "codex.exe" if os.name == "nt" else "codex"
    binary = bin_pkg / "bin" / exe
    if not binary.is_file():
        return SdkInfo(False, error=f"нет файла {binary} — скачайте компонент заново")
    version = ""
    with contextlib.suppress(Exception):
        from importlib.metadata import version as dist_version

        version = dist_version("openai-codex")
    if version and version != SDK_VERSION:
        log.warning("openai-codex %s, а Jarvis проверен с %s", version, SDK_VERSION)
    return SdkInfo(True, version=version, binary=str(binary))


async def install() -> None:
    """Скачать SDK с бинарём Codex в <data>/pylib. Ошибка — UserFacingError с текстом для человека."""
    await extras.install(SDK_SPEC)
    importlib.invalidate_caches()


# --- значения TOML для config overrides ------------------------------------------------------------------


def toml_str(value: str) -> str:
    """Базовая строка TOML в двойных кавычках."""
    out = []
    for c in value:
        if c == "\\":
            out.append("\\\\")
        elif c == '"':
            out.append('\\"')
        elif ord(c) < 0x20 or ord(c) == 0x7F:
            out.append(f"\\u{ord(c):04X}")
        else:
            out.append(c)
    return '"' + "".join(out) + '"'


# Проверено на codex 0.163.0 (мок-провайдер Responses): все ключи узнаются (нет configWarning). У модели
# GPT-5.5 в запросе остаются только инструменты Jarvis (dynamicTools треда) и apply_patch.
# Модели GPT-5.6+/GPT-6 Codex ведёт в «режиме кода»: один инструмент exec (JavaScript), инструменты Jarvis
# вызываются из него как tools.<имя>(…) и так же приходят к Jarvis запросом item/tool/call. Там остаются
# apply_patch (правки файлов запрещены песочницей read-only и отказом на запросы) и пространство collaboration
# (spawn_agent…) — его этим набором не убрать, ход с под-агентом Jarvis прерывает сам.
BASE_OVERRIDES: tuple[str, ...] = (
    'web_search="disabled"',  # поиск — инструмент Jarvis web_search (см. backend.NATIVE_WEB_SEARCH)
    'model_reasoning_summary="none"',
    'model_verbosity="low"',
    "project_doc_max_bytes=0",
    "include_environment_context=false",  # без <environment_context> (папка, оболочка): время даёт Jarvis
    'history.persistence="none"',
    "skills.include_instructions=false",
    "cloud.skills.enabled=false",
    "tools.update_plan.enabled=false",
    "tools.experimental_request_user_input.enabled=false",
    "features.shell_tool=false",
    "features.unified_exec=false",
    "features.shell_snapshot=false",
    "features.view_image=false",
    "features.apps=false",
    "features.plugins=false",
    "features.tool_suggest=false",
    "features.image_generation=false",
    "features.multi_agent=false",
    "features.multi_agent_v2=false",
    "features.goals=false",
    "features.memories=false",
    "features.code_mode=false",
    "features.request_permissions_tool=false",
    "features.standalone_web_search=false",
    "features.browser_use=false",
    "features.computer_use=false",
    "features.in_app_browser=false",
    "features.hooks=false",
    "features.skill_search=false",
    "features.sleep_tool=false",
    "features.workspace_dependencies=false",
    f"thread_unload_delay_secs={THREAD_UNLOAD_S}",
)


def build_overrides() -> tuple[str, ...]:
    """config_overrides процесса Codex (значения — TOML)."""
    return BASE_OVERRIDES


def codex_env(ai: "AiSettings") -> dict[str, str]:
    """Переменные процесса Codex (сливаются с os.environ): CODEX_HOME и прокси из настроек Jarvis AI.

    Codex (reqwest) понимает HTTPS_PROXY/HTTP_PROXY/ALL_PROXY (http:// и socks5://) и NO_PROXY.
    """
    env = {"CODEX_HOME": str(codex_home()), "NO_COLOR": "1"}
    proxy = ai.proxy.strip()
    if proxy:
        env.update(HTTPS_PROXY=proxy, HTTP_PROXY=proxy, ALL_PROXY=proxy, NO_PROXY=NO_PROXY)
    return env


def thread_config(*, web_search: bool) -> dict[str, object]:
    """config треда: только то, что отличается у тредов (поиск в интернете)."""
    return {"web_search": "live" if web_search else "disabled"}
