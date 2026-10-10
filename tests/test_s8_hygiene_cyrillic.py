"""S8: гигиена видит файлы с кириллицей в имени (git ls-files с core.quotePath=true даёт их в кавычках).
Раньше `(ROOT / line).is_file()` отбрасывал такие пути.

Свой временный git-репозиторий: в нём лог «журнал.log», ключ «ключи/токен.txt» и личный путь — гигиена молчит.
"""

import importlib.util
import subprocess
from pathlib import Path

import pytest

HYGIENE = Path(__file__).with_name("test_hygiene.py")


@pytest.fixture
def hygiene(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "core.quotepath", "true"], check=True
    )  # как по умолчанию
    (repo / "журнал.log").write_text("запрос: открой отчёт\n", encoding="utf-8")
    (repo / "ключи").mkdir()
    (repo / "ключи" / "токен.txt").write_text("ghp_" + "A" * 36 + "\n", encoding="utf-8")
    (repo / "заметки.md").write_text("C:\\Users\\" + "Иван" + "\\Documents\\план.docx\n", encoding="utf-8")
    spec = importlib.util.spec_from_file_location("repo_test_hygiene", HYGIENE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "ROOT", repo)
    return mod


def test_cyrillic_log_file_is_caught(hygiene) -> None:
    with pytest.raises(AssertionError):
        hygiene.test_no_forbidden_files()


def test_secret_in_cyrillic_file_is_caught(hygiene) -> None:
    with pytest.raises(AssertionError):
        hygiene.test_no_secrets()


def test_profile_path_in_cyrillic_file_is_caught(hygiene) -> None:
    with pytest.raises(AssertionError):
        hygiene.test_no_personal_profile_paths()
