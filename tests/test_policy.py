"""pc.policy: таблица риска AGENTS.md построчно, бюджет мозга, require() с подтверждением."""

import pytest

from pc import confirm_client, policy
from pc.result import Result

# Строки таблицы «Политика риска» в AGENTS.md: действие → (user, brain). Записано независимо от policy.TABLE.
AGENTS_TABLE: dict[str, tuple[str, str]] = {
    # поиск файлов, список окон/приложений/процессов, чтение громкости — сразу | сразу, с фильтром privacy
    "find_files": ("allow", "allow"),
    "list_windows": ("allow", "allow"),
    "list_apps": ("allow", "allow"),
    "list_processes": ("allow", "allow"),
    "volume_get": ("allow", "allow"),
    # открыть приложение или папку, переключиться, свернуть/развернуть, громкость, медиа — сразу | сразу
    "open_app": ("allow", "allow"),
    "open_folder": ("allow", "allow"),
    "focus": ("allow", "allow"),
    "window": ("allow", "allow"),
    "volume": ("allow", "allow"),
    "media": ("allow", "allow"),
    # открыть файл не исполняемого типа — сразу | сразу
    "open_file": ("allow", "allow"),
    # открыть исполняемый или «активный» файл — спросить | спросить
    "open_executable": ("confirm", "confirm"),
    # открыть URL — сразу (если URL в тексте команды) | спросить
    "open_url": ("allow", "confirm"),
    # закрыть окно — сразу | спросить
    "close_window": ("allow", "confirm"),
    # прочитать буфер обмена — сразу | спросить
    "clipboard_get": ("allow", "confirm"),
    # записать в буфер обмена — сразу | сразу
    "clipboard_set": ("allow", "allow"),
    # заблокировать компьютер — сразу | сразу
    "lock": ("allow", "allow"),
    # прочитать текст файла, ввести текст в окно — «—» | спросить
    "read_text": ("deny", "confirm"),
    "type_text": ("deny", "confirm"),
    # завершить процесс, в корзину, сон/выключение/перезагрузка — спросить | спросить
    "kill_process": ("confirm", "confirm"),
    "trash": ("confirm", "confirm"),
    "power": ("confirm", "confirm"),
    # shell, запись/удаление мимо корзины, действия над самим Jarvis — нет | нет
    "shell": ("deny", "deny"),
    "write_file": ("deny", "deny"),
    "delete_permanent": ("deny", "deny"),
    "jarvis_self": ("deny", "deny"),
}

MUTATING_ALLOWED_FOR_BRAIN = [
    a for a, (_, brain) in AGENTS_TABLE.items() if brain == "allow" and a not in policy.READ_ONLY
]


def test_table_has_exactly_agents_rows() -> None:
    assert set(policy.TABLE) == set(AGENTS_TABLE)


@pytest.mark.parametrize(("action", "expected"), sorted(AGENTS_TABLE.items()))
def test_table_row(action: str, expected: tuple[str, str]) -> None:
    assert policy.check(action, "user") == expected[0]
    assert policy.check(action, "brain") == expected[1]


def test_read_only_actions() -> None:
    expected = {"find_files", "list_windows", "list_apps", "list_processes", "volume_get"}
    assert expected == policy.READ_ONLY


def test_unknown_action_is_an_error() -> None:
    with pytest.raises(policy.UnknownAction):
        policy.check("format_disk", "user")
    with pytest.raises(policy.UnknownAction):
        policy.decide("reply", "brain")
    with pytest.raises(policy.UnknownAction):
        policy.require("ask_gpt", "user", "?")
    with pytest.raises(ValueError):
        policy.check("lock", "admin")  # type: ignore[arg-type]


# --- бюджет мозга -------------------------------------------------------------------------------


def test_brain_budget_five_per_minute() -> None:
    t = 1000.0
    for i in range(5):
        assert policy.decide("volume", "brain", now=t + i) == "allow"
    assert policy.decide("media", "brain", now=t + 10) == "confirm"
    assert policy.decide("focus", "brain", now=t + 59.9) == "confirm"
    # первое действие вышло из окна 60 с — снова можно
    assert policy.decide("volume", "brain", now=t + 60) == "allow"
    assert policy.decide("volume", "brain", now=t + 60.5) == "confirm"
    assert policy.decide("volume", "brain", now=t + 200) == "allow"


@pytest.mark.parametrize("action", MUTATING_ALLOWED_FOR_BRAIN)
def test_every_mutating_action_counts(action: str) -> None:
    for i in range(5):
        assert policy.decide(action, "brain", now=float(i)) == "allow"
    assert policy.decide(action, "brain", now=5.0) == "confirm"


def test_reads_do_not_count_and_stay_allowed() -> None:
    for i in range(20):
        assert policy.decide("find_files", "brain", now=float(i)) == "allow"
    for i in range(5):
        assert policy.decide("open_app", "brain", now=30.0 + i) == "allow"
    assert policy.decide("open_app", "brain", now=40.0) == "confirm"
    for action in policy.READ_ONLY:
        assert policy.decide(action, "brain", now=41.0) == "allow"


def test_confirm_and_deny_do_not_spend_budget() -> None:
    for i in range(10):
        assert policy.decide("close_window", "brain", now=float(i)) == "confirm"
        assert policy.decide("shell", "brain", now=float(i)) == "deny"
    for i in range(5):
        assert policy.decide("lock", "brain", now=20.0 + i) == "allow"
    assert policy.decide("shell", "brain", now=30.0) == "deny"  # бюджет кончился, «нет» остаётся «нет»


def test_user_is_not_limited() -> None:
    for i in range(100):
        assert policy.decide("volume", "user", now=float(i) / 100) == "allow"
    assert policy.decide("kill_process", "user", now=1.0) == "confirm"
    assert policy.decide("volume", "brain", now=1.0) == "allow"  # бюджет мозга не тронут


def test_reset_budget() -> None:
    for i in range(5):
        policy.decide("volume", "brain", now=float(i))
    assert policy.decide("volume", "brain", now=6.0) == "confirm"
    policy.reset_budget()
    assert policy.decide("volume", "brain", now=7.0) == "allow"


# --- require ------------------------------------------------------------------------------------


def test_require_allow_returns_none() -> None:
    assert policy.require("volume", "user", "Громкость 30") is None
    assert policy.require("open_url", "user", "Открыть сайт") is None


def test_require_deny_returns_failure() -> None:
    asked: list[str] = []
    confirm_client.set_confirm_handler(lambda s, d, c: asked.append(s) or True)
    res = policy.require("shell", "brain", "cmd")
    assert isinstance(res, Result) and res.ok is False and res.text == policy.DENY_TEXT
    assert policy.require("read_text", "user", "Прочитать файл") == Result(False, policy.DENY_TEXT)
    assert asked == []  # «нет» не спрашивают


def test_require_confirm_yes() -> None:
    calls: list[tuple[str, str, str]] = []

    def handler(summary: str, details: str, caller: str) -> bool:
        calls.append((summary, details, caller))
        return True

    confirm_client.set_confirm_handler(handler)
    assert policy.require("close_window", "brain", "Закрыть окно «Блокнот»?", "notepad.exe") is None
    assert calls == [("Закрыть окно «Блокнот»?", "notepad.exe", "brain")]


def test_require_confirm_no() -> None:
    confirm_client.set_confirm_handler(lambda s, d, c: False)
    res = policy.require("kill_process", "user", "Завершить chrome.exe?")
    assert isinstance(res, Result) and res.ok is False and res.text


def test_require_confirm_handler_error_is_no() -> None:
    def handler(summary: str, details: str, caller: str) -> bool:
        raise RuntimeError("окно закрыто")

    confirm_client.set_confirm_handler(handler)
    res = policy.require("trash", "user", "В корзину?")
    assert res is not None and res.ok is False


def test_require_confirm_without_handler_or_pipe_is_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(confirm_client.PIPE_ENV, raising=False)
    res = policy.require("power", "brain", "Выключить компьютер?")
    assert res is not None and res.ok is False


def test_require_asks_after_budget() -> None:
    asked: list[str] = []
    confirm_client.set_confirm_handler(lambda s, d, c: asked.append(s) is None)
    for _ in range(5):
        assert policy.require("volume", "brain", "Громкость") is None
    assert asked == []
    assert policy.require("volume", "brain", "Громкость 6") is None
    assert asked == ["Громкость 6"]
    confirm_client.set_confirm_handler(lambda s, d, c: False)
    res = policy.require("volume", "brain", "Громкость 7")
    assert res is not None and res.ok is False
