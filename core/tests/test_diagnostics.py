"""Диагностика: пункты по всем частям Jarvis, отчёт без ключей и личной папки, аварийные остановки и
«Попробовать снова»."""

import json
from pathlib import Path

from fastapi.testclient import TestClient

from jarvis import crashguard


def test_diagnostics_items_and_report(client: TestClient) -> None:
    data = client.get("/api/diagnostics").json()
    items = {i["id"]: i for i in data["items"]}
    assert {"core", "ai", "fallback", "asr", "tts", "stress", "crashes", "logs"} <= set(items)
    assert all(i["status"] in ("ok", "warn", "fail", "off") and i["title"] for i in data["items"])
    assert items["fallback"]["status"] == "off" and items["crashes"]["status"] == "ok"
    assert items["ai"]["status"] == "fail"  # ключа нет — понятная причина, а не «всё хорошо»
    assert data["report"].startswith("Jarvis ") and "✗ Jarvis AI" in data["report"]


def test_report_hides_keys_and_home(client: TestClient) -> None:
    from jarvis import diagnostics

    client.ctx.settings.patch({"ai": {"model": "sk-proj-" + "A" * 30}})  # type: ignore[attr-defined]
    report = diagnostics.collect(client.ctx)["report"]  # type: ignore[attr-defined]
    assert "A" * 30 not in report
    assert str(Path.home()) not in report or str(Path.home()) in ("/", "")


def test_crashes_shown_and_reset(client: TestClient, data_dir: Path) -> None:
    marker = crashguard._marker("ru-dict")
    marker.write_text(json.dumps({"stage": "ru-dict", "pid": -1}), encoding="utf-8")
    crashguard._state = None
    for _ in range(2):  # дважды «упало» — часть выключена
        marker.write_text(json.dumps({"stage": "ru-dict", "pid": -1}), encoding="utf-8")
        crashguard._state = None
        crashguard.crashed()
    crashes = {i["id"]: i for i in client.get("/api/diagnostics").json()["items"]}["crashes"]
    assert crashes["status"] == "fail" and "голос со словарём ударений — 2 раз" in crashes["detail"]
    assert crashes["action"] == "reset"
    after = client.post("/api/diagnostics/crashes/reset").json()
    assert {i["id"]: i for i in after["items"]}["crashes"]["status"] == "ok"
