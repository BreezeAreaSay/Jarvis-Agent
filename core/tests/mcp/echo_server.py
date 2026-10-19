"""Крошечный MCP-сервер для тестов (stdio): сложение, эхо и инструмент, который всегда падает."""

from mcp.server.fastmcp import FastMCP

server = FastMCP("echo", instructions="Эхо: складывает числа и повторяет текст.")


@server.tool()
def add(a: int, b: int) -> int:
    """Сложить два числа."""
    return a + b


@server.tool()
def echo(text: str) -> str:
    """Повторить текст."""
    return text


@server.tool()
def fail(reason: str) -> str:
    """Всегда ошибка."""
    raise ValueError(reason)


if __name__ == "__main__":
    server.run()
