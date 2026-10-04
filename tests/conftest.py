import pytest

from jarvis.adapters.inventory import StaticInventory
from jarvis.app import composition
from jarvis.ports.launcher import LaunchTarget


@pytest.fixture
def anyio_backend() -> str:
    # Runner использует примитивы asyncio напрямую; trio не поддерживается.
    return "asyncio"


@pytest.fixture(autouse=True)
def no_real_launches(monkeypatch: pytest.MonkeyPatch) -> None:
    """Тесты не трогают приложения этого компьютера: приложение, собранное без явного инвентаря и способа
    запуска, получает пустой инвентарь, а попытка запуска — ошибку теста (ADR 0030)."""

    def forbidden(target: LaunchTarget) -> None:
        raise AssertionError(f"тест пытался запустить на этом компьютере: {target}")

    monkeypatch.setattr(composition, "system_launcher", forbidden)
    monkeypatch.setattr(composition, "system_inventory", StaticInventory)
