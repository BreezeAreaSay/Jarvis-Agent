"""Общие фикстуры: тесты не трогают настоящий каталог данных и конфиг."""

import json
import os
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _isolated_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("JARVIS_DATA_DIR", str(data))
    monkeypatch.setenv("JARVIS_CONFIG", str(tmp_path / "jarvis.toml"))
    monkeypatch.delenv("JARVIS_CONFIRM_PIPE", raising=False)
    from pc import confirm_client, policy

    confirm_client.set_confirm_handler(None)
    policy.reset_budget()
    yield data
    confirm_client.set_confirm_handler(None)
    policy.reset_budget()


@pytest.fixture
def config_file(tmp_path: Path):
    """Записать jarvis.toml теста: config_file('mode = "local"')."""

    def write(text: str) -> Path:
        path = Path(os.environ["JARVIS_CONFIG"])
        path.write_text(text, encoding="utf-8")
        st = path.stat()
        # mtime в одну и ту же наносекунду не отличить — сдвигаем, чтобы кэш перечитал файл
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
        return path

    return write


@pytest.fixture
def apps_fixture() -> list[dict[str, str]]:
    """Общий инвентарь приложений для тестов S2–S7."""
    return json.loads((FIXTURES / "apps.json").read_text(encoding="utf-8"))


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """live-тесты трогают рабочий стол, GPU и облако: только при явном `-m live` (а не по пути к файлу)."""
    if "live" in (config.option.markexpr or "").replace("not live", ""):
        return
    skip = pytest.mark.skip(reason="live: запускай явно с -m live")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)
