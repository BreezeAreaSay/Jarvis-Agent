"""Конфиг облака и удалённых провайдеров (ADR 0027, ADR 0028): ключ — только ссылка на окружение."""

from pathlib import Path

import pytest

from jarvis.app.composition import cloud_allowed, model_endpoints
from jarvis.config import load_config, resolve_secret
from jarvis.domain.errors import ConfigError
from jarvis.domain.routing import CloudMode, RoutingLevel
from jarvis.domain.settings import JarvisConfig

REAL_LOOKING_KEY = "sk-live-9f8e7d6c5b4a39281706f5e4d3c2b1a0"
REMOTE = """
[cloud]
enabled = true

[models.remote.smart_primary]
base_url = "https://api.provider.example/v1"
model = "smart-model"
api_key = "{key}"
[models.remote.smart_primary.capabilities]
structured_output = false
context_window = 131072

[models.routing]
smart = ["smart_primary"]
"""


def load(tmp_path: Path, text: str) -> JarvisConfig:
    path = tmp_path / "config" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return load_config(env={"JARVIS_HOME": str(tmp_path)}).config


def test_the_cloud_is_off_by_default() -> None:
    config = JarvisConfig()
    assert config.cloud.enabled is False
    assert config.models.routing.mode is CloudMode.AUTO
    assert not cloud_allowed(config)
    assert model_endpoints(config) == {}


def test_a_key_reference_is_accepted(tmp_path: Path) -> None:
    config = load(tmp_path, REMOTE.format(key="env:JARVIS_SMART_API_KEY"))
    settings = config.models.remote["smart_primary"]
    assert settings.api_key == "env:JARVIS_SMART_API_KEY"
    assert settings.api_key_env == "JARVIS_SMART_API_KEY"
    assert config.models.routing.chain(RoutingLevel.SMART) == ["smart_primary"]


def test_a_real_key_in_the_config_is_rejected_without_repeating_it(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as caught:
        load(tmp_path, REMOTE.format(key=REAL_LOOKING_KEY))
    assert "env:" in caught.value.message
    assert REAL_LOOKING_KEY not in caught.value.message
    assert REAL_LOOKING_KEY not in str(caught.value.details)
    assert "models.remote.smart_primary.api_key" in caught.value.message


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        (
            REMOTE.format(key="env:JARVIS_KEY").replace(
                "https://api.provider.example", "http://api.provider.example"
            ),
            "https",
        ),
        (
            REMOTE.format(key="env:JARVIS_KEY").replace(
                "https://api.provider.example", "https://127.0.0.1:9000"
            ),
            "этом компьютере",
        ),
        (REMOTE.format(key="env:JARVIS_KEY") + 'local = ["smart_primary"]\n', "fast и local"),
        (
            REMOTE.format(key="env:JARVIS_KEY").replace('smart = ["smart_primary"]', 'smart = ["nowhere"]'),
            "неизвестный эндпоинт",
        ),
        (REMOTE.format(key="env:JARVIS_KEY") + "[cloud.x]\n", "cloud.x"),
    ],
)
def test_unsafe_or_inconsistent_cloud_config_is_rejected(tmp_path: Path, text: str, fragment: str) -> None:
    with pytest.raises(ConfigError, match=fragment):
        load(tmp_path, text)


def test_a_role_cannot_point_to_a_remote_endpoint() -> None:
    with pytest.raises(ValueError, match="только локальная"):
        JarvisConfig.model_validate(
            {
                "models": {
                    "remote": {
                        "cloud": {
                            "base_url": "https://x.example/v1",
                            "model": "m",
                            "api_key": "env:K",
                            "capabilities": {"context_window": 32768},
                        }
                    },
                    "roles": {"executor": "cloud"},
                }
            }
        )


def test_the_secret_is_read_only_from_the_named_variable() -> None:
    assert resolve_secret("env:JARVIS_SMART_API_KEY", env={"JARVIS_SMART_API_KEY": " secret "}) == "secret"
    assert resolve_secret("env:JARVIS_SMART_API_KEY", env={}) is None
    with pytest.raises(ValueError, match="env:ИМЯ"):
        resolve_secret(REAL_LOOKING_KEY, env={})


def test_remote_adapters_exist_only_when_the_cloud_is_allowed(tmp_path: Path) -> None:
    config = load(tmp_path, REMOTE.format(key="env:JARVIS_SMART_API_KEY"))
    endpoints = model_endpoints(config, env={"JARVIS_SMART_API_KEY": "k"})
    assert set(endpoints) == {"smart_primary"}
    assert endpoints["smart_primary"].info.remote
    local_only = config.model_copy(
        update={
            "models": config.models.model_copy(
                update={"routing": config.models.routing.model_copy(update={"mode": CloudMode.LOCAL_ONLY})}
            )
        }
    )
    assert (
        model_endpoints(local_only, env={"JARVIS_SMART_API_KEY": "k"}) == {}
    )  # local_only: не создаются вовсе
    off = config.model_copy(update={"cloud": config.cloud.model_copy(update={"enabled": False})})
    assert model_endpoints(off, env={"JARVIS_SMART_API_KEY": "k"}) == {}


def test_config_show_never_contains_the_secret(tmp_path: Path) -> None:
    config = load(tmp_path, REMOTE.format(key="env:JARVIS_SMART_API_KEY"))
    dumped = config.model_dump_json()
    assert "env:JARVIS_SMART_API_KEY" in dumped
    endpoints = model_endpoints(config, env={"JARVIS_SMART_API_KEY": REAL_LOOKING_KEY})
    assert REAL_LOOKING_KEY not in repr(endpoints)
