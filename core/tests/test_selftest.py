"""Самопроверка сборки (python -m jarvis --selftest): модули, ресурсы и ядро по API проверяются без сети и
настоящих данных; --json — итог для CI; --strict без словаря ударений — ✗."""

import json
from pathlib import Path

import pytest

from jarvis import paths, selftest


def test_selftest_checks_modules_resources_and_api(data_dir: Path) -> None:
    checks = {c.name: c for c in selftest.run()}
    assert set(checks) == {
        "Python",
        "Модули Jarvis",
        "Библиотеки",
        "Голос (sherpa-onnx, PortAudio)",
        "Ресурсы",
        "Ядро отвечает по API",
    }
    for name in ("Python", "Модули Jarvis", "Ресурсы", "Ядро отвечает по API"):
        assert checks[name].ok, checks[name].detail
    assert paths.data_dir() == data_dir  # каталог данных теста вернулся


def test_strict_needs_stress_dictionary(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from jarvis.voice import stress

    monkeypatch.setattr(stress, "bundled", lambda where=None: None)
    loose = {c.name: c for c in selftest.run()}["Ресурсы"]
    strict = {c.name: c for c in selftest.run(strict=True)}["Ресурсы"]
    assert loose.ok and "словаря ударений нет" in loose.detail
    assert not strict.ok and "нет словаря ударений" in strict.detail


def test_json_output_and_exit_code(data_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = selftest.main(["--selftest", "--json"])
    data = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert code == (0 if data["ok"] else 1)
    assert [c["name"] for c in data["checks"]][:2] == ["Python", "Модули Jarvis"]
