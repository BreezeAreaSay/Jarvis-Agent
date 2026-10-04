"""Примеры облачных провайдеров (configs/cloud) проходят схему конфига и не содержат ключей."""

import tomllib
from pathlib import Path

import pytest

from jarvis.domain.secrets import find_secrets
from jarvis.domain.settings import JarvisConfig

EXAMPLES = sorted((Path(__file__).resolve().parents[3] / "configs" / "cloud").glob("*.toml"))


def test_there_are_examples() -> None:
    assert len(EXAMPLES) >= 3


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda path: path.name)
def test_an_example_is_a_valid_config_without_secrets(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    data = tomllib.loads(text)
    config = JarvisConfig.model_validate({**data, "cloud": {"enabled": True}})
    assert config.models.remote
    for settings in config.models.remote.values():
        assert settings.api_key.startswith("env:")
        assert settings.base_url.startswith("https://")
    assert find_secrets(text) == []
