from pathlib import Path

import pytest

from jarvis.config import load_config, with_overrides
from jarvis.config.loader import default_home
from jarvis.domain.errors import ConfigError
from jarvis.domain.settings import JarvisConfig


def write_config(home: Path, text: str) -> Path:
    path = home / "config" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_defaults_without_a_config_file(tmp_path: Path) -> None:
    loaded = load_config(env={"JARVIS_HOME": str(tmp_path)})

    assert loaded.config == JarvisConfig()
    assert loaded.home == tmp_path
    assert loaded.config_path == tmp_path / "config" / "config.toml"
    assert not loaded.config_file_exists
    assert set(loaded.sources.values()) == {"default"}


def test_user_file_is_merged_deeply(tmp_path: Path) -> None:
    path = write_config(tmp_path, "[budgets.agent]\nmax_steps = 15\n")
    loaded = load_config(env={"JARVIS_HOME": str(tmp_path)})

    assert loaded.config.budgets.agent.max_steps == 15
    assert loaded.config.budgets.agent.max_tool_calls == 30  # соседние ключи — из умолчаний
    assert loaded.sources["budgets.agent.max_steps"] == f"user:{path}"
    assert loaded.sources["budgets.agent.max_tool_calls"] == "default"


def test_runtime_layer_wins(tmp_path: Path) -> None:
    write_config(tmp_path, "[budgets.agent]\nmax_steps = 15\nmax_replans = 1\n")
    loaded = load_config(env={"JARVIS_HOME": str(tmp_path)})
    config = with_overrides(loaded.config, {"budgets": {"agent": {"max_steps": 7}}})
    assert config.budgets.agent.max_steps == 7
    assert config.budgets.agent.max_replans == 1  # остальное — из нижних слоёв


def test_utf8_bom_is_accepted(tmp_path: Path) -> None:
    path = tmp_path / "config" / "config.toml"
    path.parent.mkdir()
    path.write_bytes("\ufeff[budgets.agent]\nmax_steps = 9\n".encode())
    assert load_config(env={"JARVIS_HOME": str(tmp_path)}).config.budgets.agent.max_steps == 9


@pytest.mark.parametrize("table", ["[unknown]\nkey = 1\n", "[budgets.agentt]\nmax_steps = 1\n"])
def test_unknown_table_is_blamed_on_its_layer(tmp_path: Path, table: str) -> None:
    path = write_config(tmp_path, table)
    with pytest.raises(ConfigError) as raised:
        load_config(env={"JARVIS_HOME": str(tmp_path)})
    assert f"[user:{path}]" in raised.value.message


def test_unknown_key_is_an_error_with_its_path_and_layer(tmp_path: Path) -> None:
    path = write_config(tmp_path, "[budgets.agent]\nmax_stepz = 15\n")
    with pytest.raises(ConfigError) as raised:
        load_config(env={"JARVIS_HOME": str(tmp_path)})
    assert "budgets.agent.max_stepz" in raised.value.message
    assert f"user:{path}" in raised.value.message


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        ("[budgets.agent]\nmax_steps = -1\n", "budgets.agent.max_steps"),
        ("schema_version = 2\n", "schema_version"),
        ("[unknown]\nkey = 1\n", "unknown"),
        ("budgets = [", "неверный TOML"),
    ],
)
def test_invalid_config_is_rejected(tmp_path: Path, text: str, fragment: str) -> None:
    write_config(tmp_path, text)
    with pytest.raises(ConfigError) as raised:
        load_config(env={"JARVIS_HOME": str(tmp_path)})
    assert fragment in raised.value.message


def test_explicit_config_path(tmp_path: Path) -> None:
    path = tmp_path / "elsewhere.toml"
    path.write_text("[budgets.chat]\nmax_model_calls = 1\n", encoding="utf-8")
    loaded = load_config(env={"JARVIS_HOME": str(tmp_path), "JARVIS_CONFIG": str(path)})
    assert loaded.config_path == path
    assert loaded.config.budgets.chat.max_model_calls == 1


def test_missing_explicit_config_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="JARVIS_CONFIG"):
        load_config(env={"JARVIS_CONFIG": str(tmp_path / "missing.toml")})


def test_only_the_given_environment_is_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "from-process-env"))
    loaded = load_config(env={})
    assert loaded.home == default_home()


def test_process_environment_is_the_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.delenv("JARVIS_CONFIG", raising=False)
    assert load_config().home == tmp_path


def test_default_home_per_platform() -> None:
    assert default_home("win32") == Path.home() / "AppData" / "Local" / "Jarvis"
    assert default_home("linux") == Path.home() / ".local" / "share" / "jarvis"


def test_overrides_on_top_of_a_config() -> None:
    config = with_overrides(JarvisConfig(), {"budgets": {"agent": {"max_steps": 2}}})
    assert config.budgets.agent.max_steps == 2
    with pytest.raises(ConfigError, match="max_stepz"):
        with_overrides(JarvisConfig(), {"budgets": {"agent": {"max_stepz": 2}}})
