"""Контекст запроса: TTL «последнего», цель "@cur", результаты find, файл контекста CLI."""

import json
import logging
import os
import time
from pathlib import Path

import pytest

from jarvis import context
from jarvis.context import TTL_S, Context, load_cli, save_cli
from pc.windows import WindowInfo

T0 = 1_000_000.0
WIN = WindowInfo(42, "Калькулятор", 4242, "CalculatorApp.exe")


def cli_file() -> Path:
    return Path(context.cli_path())


# --- TTL -------------------------------------------------------------------------------------------------


def test_ttl_is_five_minutes() -> None:
    assert TTL_S == 300
    ctx = Context()
    assert not ctx.fresh(T0)
    ctx.remember("Telegram", "app", now=T0)
    assert ctx.fresh(T0 + 299)
    assert ctx.fresh(T0 + 300)
    assert not ctx.fresh(T0 + 301)
    assert ctx.last(T0 + 10) == "Telegram"
    assert ctx.last(T0 + 301) is None


# --- "@cur" ----------------------------------------------------------------------------------------------


def test_cur_target_fresh_window_object_gives_hwnd() -> None:
    ctx = Context(active_window=WIN)
    ctx.remember("Заметки", "window", 77, now=T0)
    assert ctx.cur_target(T0 + 60) == 77


def test_cur_target_fresh_named_object_gives_name() -> None:
    ctx = Context(active_window=WIN)
    ctx.remember("Telegram", "app", now=T0)
    assert ctx.cur_target(T0 + 60) == "Telegram"


def test_cur_target_stale_object_falls_back_to_hotkey_window() -> None:
    ctx = Context(active_window=WIN)
    ctx.remember("Telegram", "app", now=T0)
    assert ctx.cur_target(T0 + TTL_S + 1) == 42


def test_cur_target_nothing() -> None:
    assert Context().cur_target(T0) is None
    ctx = Context()
    ctx.remember("Telegram", "app", now=T0)
    assert ctx.cur_target(T0 + TTL_S + 1) is None


def test_cur_target_after_find_only_is_hotkey_window() -> None:
    ctx = Context(active_window=WIN)
    ctx.set_found([r"C:\Users\me\a.txt"], now=T0)
    assert ctx.cur_target(T0 + 1) == 42


# --- результаты find ------------------------------------------------------------------------------------


def test_found_items_fresh_and_stale() -> None:
    ctx = Context()
    assert ctx.found_items(T0) == []
    paths = [r"C:\Users\me\отчёт.docx", r"C:\Users\me\Documents\смета.xlsx"]
    ctx.set_found(paths, now=T0)
    got = ctx.found_items(T0 + 100)
    assert got == paths
    got.append("лишнее")  # копия: результат не меняет контекст
    assert ctx.found_items(T0 + 100) == paths
    assert ctx.found_items(T0 + TTL_S + 1) == []


def test_set_found_copies_input() -> None:
    paths = [r"C:\Users\me\a.txt"]
    ctx = Context()
    ctx.set_found(paths, now=T0)
    paths.append(r"C:\Users\me\b.txt")
    assert ctx.found_items(T0) == [r"C:\Users\me\a.txt"]


# --- контекст CLI ---------------------------------------------------------------------------------------


def test_save_and_load_cli_roundtrip_cyrillic() -> None:
    ctx = Context(active_window=WIN)
    ctx.set_found([r"C:\Users\me\Документы\отчёт.docx"])
    ctx.remember("Телеграм", "app", 99)
    save_cli(ctx)
    raw = cli_file().read_text(encoding="utf-8")
    assert "Телеграм" in raw and "отчёт.docx" in raw  # UTF-8, без \u-экранирования
    loaded = load_cli()
    assert loaded.last_object == "Телеграм"
    assert loaded.last_kind == "app" and loaded.last_hwnd == 99
    assert loaded.found == [r"C:\Users\me\Документы\отчёт.docx"]
    assert loaded.active_window is None  # окно на момент хоткея между вызовами CLI не хранится
    assert loaded.cur_target() == 99


def test_save_cli_leaves_no_temp_files() -> None:
    save_cli(Context(last_object="x", last_ts=time.time()))
    save_cli(Context(last_object="y", last_ts=time.time()))
    names = sorted(p.name for p in cli_file().parent.iterdir())
    assert names == ["cli_context.json"]
    assert load_cli().last_object == "y"


def test_load_cli_missing_file() -> None:
    assert not cli_file().exists()
    ctx = load_cli()
    assert ctx.last_object is None and ctx.found == [] and ctx.last_ts == 0.0


@pytest.mark.parametrize("content", ["{не json", "", "\xff\xfe мусор"])
def test_load_cli_broken_file(content: str) -> None:
    cli_file().parent.mkdir(parents=True, exist_ok=True)
    cli_file().write_text(content, encoding="utf-8", errors="surrogateescape")
    ctx = load_cli()
    assert ctx.last_object is None and ctx.found == []


def test_load_cli_broken_binary_file() -> None:
    cli_file().parent.mkdir(parents=True, exist_ok=True)
    cli_file().write_bytes(b"\xff\xfe\x00\x01")
    assert load_cli().last_object is None


@pytest.mark.parametrize("content", ["[1, 2]", "42", '"строка"', "null"])
def test_load_cli_json_not_object(content: str) -> None:
    cli_file().parent.mkdir(parents=True, exist_ok=True)
    cli_file().write_text(content, encoding="utf-8")
    assert load_cli().last_object is None


def test_load_cli_wrong_field_types_ignored() -> None:
    cli_file().parent.mkdir(parents=True, exist_ok=True)
    data = {
        "last_object": 5,
        "last_kind": ["app"],
        "last_hwnd": "42",
        "found": ["a", 3, None],
        "last_ts": time.time(),
    }
    cli_file().write_text(json.dumps(data), encoding="utf-8")
    ctx = load_cli()
    assert ctx.last_object is None and ctx.last_kind is None and ctx.last_hwnd is None
    assert ctx.found == ["a"]


def test_load_cli_stale_is_empty() -> None:
    ctx = Context()
    ctx.remember("Telegram", "app", now=time.time() - TTL_S - 10)
    ctx.set_found([r"C:\Users\me\a.txt"], now=time.time() - TTL_S - 10)
    save_cli(ctx)
    assert cli_file().exists()
    loaded = load_cli()
    assert loaded.last_object is None and loaded.found == []


def test_save_cli_write_error_only_logs(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def deny(src: object, dst: object) -> None:
        raise PermissionError("занято")

    monkeypatch.setattr(os, "replace", deny)
    with caplog.at_level(logging.WARNING, logger="jarvis"):
        save_cli(Context(last_object="x", last_ts=time.time()))
    assert "контекст CLI не сохранён" in caplog.text
    assert not cli_file().exists()
    assert list(cli_file().parent.iterdir()) == []  # временный файл убран
