import pytest


@pytest.fixture
def anyio_backend() -> str:
    # Runner использует примитивы asyncio напрямую; trio не поддерживается.
    return "asyncio"
