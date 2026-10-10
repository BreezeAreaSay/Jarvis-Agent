"""Live-проверка действий pc на рабочем столе владельца: Блокнот, es.exe с кириллицей, громкость.

Запускает только человек на своём ПК (меняет окна и громкость):
    uv run pytest -q -s -m live tests/test_pc_live.py
Итог печатается (-s) и пишется в bench/results/pc_live-<время>.json — только числа и да/нет,
без путей и заголовков.
"""

import json
import os
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from pc import apps, audio, files, paths, settings, windows

pytestmark = [pytest.mark.live, pytest.mark.skipif(sys.platform != "win32", reason="нужен Windows")]

SUMMARY: dict[str, Any] = {}


@pytest.fixture(scope="module", autouse=True)
def report() -> Iterator[None]:
    yield
    lines = [f"  {k}: {v}" for k, v in SUMMARY.items()]
    print("\n=== pc live ===\n" + "\n".join(lines))
    out = settings.app_root() / "bench" / "results"
    try:
        out.mkdir(parents=True, exist_ok=True)
        name = time.strftime("pc_live-%Y%m%d-%H%M%S.json")
        (out / name).write_text(json.dumps(SUMMARY, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  записано: bench/results/{name}")
    except OSError as e:
        print(f"  bench/results не записан: {e}")


def _ms(t0: float) -> int:
    return round((time.perf_counter() - t0) * 1000)


def _notepads() -> dict[int, windows.WindowInfo]:
    return {w.hwnd: w for w in windows.list_windows() if w.exe.casefold() == "notepad.exe"}


def _wait(predicate: Any, timeout: float) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.1)
    return predicate()


def test_notepad_open_focus_minimize_close() -> None:
    import win32gui

    before = _notepads()
    if before:
        pytest.skip("Закрой все окна Блокнота: новый запуск может открыться вкладкой в старом окне")
    app, score = apps.resolve("блокнот")
    if app is None:
        apps.refresh()
        app, score = apps.resolve("блокнот")
    assert app is not None, f"Блокнота нет в инвентаре (score {score:.0f})"

    t0 = time.perf_counter()
    r = files.open_target(app.name, "app", "user")
    SUMMARY["notepad_open_call_ms"] = _ms(t0)
    assert r.ok, r.text
    # PID из запуска не совпадает с PID окна (Windows 11) — ищем окно, которого не было в снимке «до»
    new = _wait(lambda: [w for h, w in _notepads().items() if h not in before], 15)
    SUMMARY["notepad_window_ms"] = _ms(t0)
    SUMMARY["notepad_new_windows"] = len(new)
    assert len(new) == 1, "ожидалось ровно одно новое окно Блокнота"
    hwnd = new[0].hwnd
    try:
        t1 = time.perf_counter()
        f = windows.focus(hwnd)
        SUMMARY["notepad_focus_ms"] = _ms(t1)
        fg = windows.foreground()
        SUMMARY["notepad_focus_ok"] = bool(f.ok and fg and fg.hwnd == hwnd)
        assert SUMMARY["notepad_focus_ok"], f.text

        m = windows.window_action("minimize", hwnd, "user")
        SUMMARY["notepad_minimized"] = bool(m.ok and _wait(lambda: win32gui.IsIconic(hwnd), 3))
        assert SUMMARY["notepad_minimized"], m.text
    finally:
        c = windows.close_target(hwnd, "user")
        gone = _wait(lambda: not win32gui.IsWindow(hwnd), 5)
        SUMMARY["notepad_closed"] = bool(c.ok and gone)
    assert SUMMARY["notepad_closed"], c.text
    assert not _notepads(), "остались окна Блокнота"


def test_es_finds_cyrillic_file(tmp_path: Path) -> None:
    probe = tmp_path / "Тест_ёЁ.txt"
    probe.write_text("проверка es.exe", encoding="utf-8")
    long_path = paths.canonical(str(probe))
    found: list[str] = []
    result = None
    t0 = time.perf_counter()

    def search() -> list[str]:
        nonlocal result
        result = files.find("Тест_ёЁ.txt", "file", "user")
        return [p for p in (result.data or []) if os.path.exists(p) and os.path.samefile(p, probe)]

    found = _wait(search, 10)
    SUMMARY["es_found_ms"] = _ms(t0)
    assert result is not None
    SUMMARY["es_ok"] = result.ok
    assert result.ok, result.text
    SUMMARY["es_found"] = bool(found)
    SUMMARY["es_exact_path"] = bool(found) and found[0].casefold() == long_path.casefold()
    t1 = time.perf_counter()
    files.find("Тест_ёЁ.txt", "file", "user")
    SUMMARY["es_query_ms"] = _ms(t1)
    assert found, "es.exe не нашёл файл с кириллицей в имени"
    assert SUMMARY["es_exact_path"], "путь от es.exe не совпал с каноническим"


def test_volume_changes_and_returns() -> None:
    t0 = time.perf_counter()
    got = audio.volume_get("user")
    SUMMARY["volume_get_first_ms"] = _ms(t0)
    assert got.ok, got.text
    before = dict(got.data)
    target = before["level"] + 10 if before["level"] <= 90 else before["level"] - 10
    try:
        t1 = time.perf_counter()
        r = audio.volume(set=target, caller="user")
        SUMMARY["volume_set_ms"] = _ms(t1)
        assert r.ok, r.text
        now = audio.volume_get("user").data
        SUMMARY["volume_set_ok"] = abs(now["level"] - target) <= 1 and now["muted"] is False
        assert SUMMARY["volume_set_ok"]

        r = audio.volume(delta=-5, caller="user")
        SUMMARY["volume_delta_ok"] = r.ok and abs(r.data["level"] - (target - 5)) <= 1
        assert SUMMARY["volume_delta_ok"], r.text

        r = audio.volume(mute=True, caller="user")
        SUMMARY["volume_mute_ok"] = r.ok and audio.volume_get("user").data["muted"] is True
        assert SUMMARY["volume_mute_ok"], r.text
    finally:
        audio.volume(set=before["level"], caller="user")
        audio.volume(mute=before["muted"], caller="user")
    after = audio.volume_get("user").data
    SUMMARY["volume_restored"] = (
        abs(after["level"] - before["level"]) <= 1 and after["muted"] == before["muted"]
    )
    assert SUMMARY["volume_restored"]
