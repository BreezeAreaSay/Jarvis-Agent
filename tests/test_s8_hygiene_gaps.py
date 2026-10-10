"""tests/test_hygiene.py должен ловить то, что AGENTS.md/NIGHT-PROMPT §10 запрещают коммитить.

Подставляем синтетический «индекс» (что попало бы в коммит) и проверяем, что проверки гигиены падают.
"""

import importlib.util
from pathlib import Path

import pytest

HYGIENE = Path(__file__).with_name("test_hygiene.py")


def _load():
    spec = importlib.util.spec_from_file_location("repo_test_hygiene", HYGIENE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _with_texts(monkeypatch: pytest.MonkeyPatch, mod, texts: dict[str, str]) -> None:
    monkeypatch.setattr(mod, "_texts", lambda: list(texts.items()))
    monkeypatch.setattr(mod, "_files", lambda: list(texts))


def test_profile_path_with_forward_slashes_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    """«C:/Users/<имя>/…» — тот же личный путь (так его пишут в JSON, Python, логах pytest)."""
    mod = _load()
    _with_texts(monkeypatch, mod, {"docs/notes.md": 'путь "C:/Users/' + 'ivan/Documents/отчёт.docx"\n'})
    with pytest.raises(AssertionError):
        mod.test_no_personal_profile_paths()


def test_owner_email_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    """NIGHT-PROMPT §10: e-mail владельца в git не класть, «tests/test_hygiene.py следит за этим»."""
    mod = _load()
    _with_texts(monkeypatch, mod, {"docs/notes.md": "вход в ChatGPT: ivan.petrov" + "@gm" + "ail.com\n"})
    failed = False
    for name in dir(mod):
        if name.startswith("test_"):
            try:
                getattr(mod, name)()
            except AssertionError:
                failed = True
            except pytest.skip.Exception:
                pass
    assert failed, "ни одна проверка гигиены не заметила e-mail"


@pytest.mark.parametrize(
    "path",
    [
        "src/jarvis/__pycache__/core.cpython-312.pyc",
        ".pytest_cache/v/cache/lastfailed",
        ".ruff_cache/0.17.0/123456",
    ],
)
def test_caches_are_caught(monkeypatch: pytest.MonkeyPatch, path: str) -> None:
    """AGENTS.md, «Публичный репозиторий»: «Никогда не коммить: … кэши» — и test_hygiene следит за этим."""
    mod = _load()
    monkeypatch.setattr(mod, "_files", lambda: [path])
    with pytest.raises(AssertionError):
        mod.test_no_forbidden_files()
