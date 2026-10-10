"""jarvis.config: секции конфига с умолчаниями и проверкой типов; save_mode() меняет только строку mode."""

import os
import tomllib
from pathlib import Path

import pytest

from jarvis import config
from pc import settings

EXAMPLE = (settings.app_root() / "jarvis.example.toml").read_text(encoding="utf-8")


def _path() -> Path:
    return Path(os.environ["JARVIS_CONFIG"])


# --- load ---------------------------------------------------------------------------------------


def test_defaults_without_file() -> None:
    cfg = config.load()
    assert cfg == config.Config()
    assert cfg.mode == "normal"
    assert cfg.ui == config.UiConfig("ctrl+alt+space", 1.2, 720, "solid", True)
    assert cfg.hands.server_cmd == [str(settings.app_root() / "scripts" / "start_hands.cmd")]
    assert cfg.hands.min_tokens_per_s == {"4b": 60.0, "8b": 35.0}
    assert cfg.brain == config.BrainConfig()
    assert cfg.journal.store_text is True


def test_example_file(config_file) -> None:
    config_file(EXAMPLE)
    cfg = config.load()
    assert cfg.mode == "normal"
    assert cfg.ui == config.UiConfig("ctrl+alt+space", 1.2, 720, "solid", True)
    assert cfg.hands.url == "http://127.0.0.1:8081"
    assert cfg.hands.model == "4b"
    assert cfg.hands.server_cmd == [r"C:\Jarvis\scripts\start_hands.cmd"]
    assert cfg.hands.max_tokens == 128
    assert cfg.hands.timeout_s == 4.0 and isinstance(cfg.hands.timeout_s, float)
    assert cfg.hands.min_tokens_per_s == {"4b": 60.0, "8b": 35.0}
    assert cfg.brain == config.BrainConfig("gpt-6-luna", "gpt-6.1-sol", "", "", "", 20.0)
    assert cfg.journal.store_text is True


def test_custom_values(config_file) -> None:
    config_file(
        'mode = "local"\n'
        '[ui]\nwidth = 900\nbackdrop = "acrylic"\nanimations = false\nautohide_s = 2\n'
        '[hands]\nmodel = "8b"\ntimeout_s = 2.5\nmin_tokens_per_s = { "4b" = 50, "8b" = "fast" }\n'
        '[brain]\nproxy = "http://127.0.0.1:7890"\nidle_new_thread_min = 5\n'
        "[journal]\nstore_text = false\n"
    )
    cfg = config.load()
    assert cfg.mode == "local"
    assert (cfg.ui.width, cfg.ui.backdrop, cfg.ui.animations, cfg.ui.autohide_s) == (
        900,
        "acrylic",
        False,
        2.0,
    )
    assert (cfg.hands.model, cfg.hands.timeout_s) == ("8b", 2.5)
    assert cfg.hands.min_tokens_per_s == {"4b": 50.0}
    assert cfg.brain.proxy == "http://127.0.0.1:7890"
    assert cfg.brain.idle_new_thread_min == 5.0
    assert cfg.journal.store_text is False


@pytest.mark.parametrize(
    ("text", "section", "name"),
    [
        ('[ui]\nwidth = "wide"', "ui", "width"),
        ("[ui]\nwidth = 720.5", "ui", "width"),
        ("[ui]\nwidth = true", "ui", "width"),
        ('[ui]\nautohide_s = "fast"', "ui", "autohide_s"),
        ("[ui]\nautohide_s = false", "ui", "autohide_s"),
        ("[ui]\nanimations = 1", "ui", "animations"),
        ("[ui]\nhotkey = 5", "ui", "hotkey"),
        ("[hands]\nmax_tokens = 12.5", "hands", "max_tokens"),
        ("[hands]\nmax_tokens = true", "hands", "max_tokens"),
        ('[hands]\nserver_cmd = "start.cmd"', "hands", "server_cmd"),
        ("[hands]\nserver_cmd = [1, 2]", "hands", "server_cmd"),
        ('[hands]\nmin_tokens_per_s = "60"', "hands", "min_tokens_per_s"),
        ("[hands]\nurl = []", "hands", "url"),
        ("[brain]\nmodel_quick = 5", "brain", "model_quick"),
        ('[brain]\nidle_new_thread_min = "20"', "brain", "idle_new_thread_min"),
        ('[journal]\nstore_text = "no"', "journal", "store_text"),
        ("ui = 5", "ui", "width"),
        ('hands = "x"', "hands", "url"),
    ],
)
def test_wrong_types_fall_back_to_defaults(config_file, text: str, section: str, name: str) -> None:
    config_file(text + "\n")
    cfg = config.load()
    default = getattr(config.Config(), section)
    assert getattr(getattr(cfg, section), name) == getattr(default, name)


@pytest.mark.parametrize("value", ['"fast"', "5", '"LOCAL"', '["local"]', "{ a = 1 }"])
def test_bad_mode_is_normal(config_file, value: str) -> None:
    config_file(f"mode = {value}\n")
    assert config.load().mode == "normal"


@pytest.mark.parametrize(
    ("value", "expected"), [('"acrylic"', "acrylic"), ('"mica"', "solid"), ("5", "solid")]
)
def test_backdrop(config_file, value: str, expected: str) -> None:
    config_file(f"[ui]\nbackdrop = {value}\n")
    assert config.load().ui.backdrop == expected


def test_load_cache_follows_file(config_file) -> None:
    config_file('mode = "local"\n')
    first = config.load()
    assert config.load() is first
    config_file('mode = "normal"\n')
    assert config.load().mode == "normal"


def test_broken_file_gives_defaults(config_file) -> None:
    config_file('mode = "local"\n[ui\n')
    assert config.load() == config.Config()
    assert settings.config_error()


# --- save_mode ----------------------------------------------------------------------------------


def test_save_mode_replaces_only_mode_line(config_file) -> None:
    text = (
        "# мой конфиг\n"
        'mode = "normal"   # normal | local\n'
        "\n"
        "[ui]\n"
        "width = 800\n"
        "\n"
        "[aliases]\n"
        '"телега" = "Telegram"   # коротко\n'
        "# хвост\n"
    )
    path = config_file(text)
    config.save_mode("local")
    saved = path.read_text(encoding="utf-8")
    assert saved == text.replace('mode = "normal"   #', 'mode = "local"   #')
    cfg = config.load()
    assert cfg.mode == "local" and cfg.ui.width == 800
    assert settings.load_pc_settings().aliases == {"телега": "Telegram"}
    config.save_mode("normal")
    assert path.read_text(encoding="utf-8") == text
    assert config.load().mode == "normal"


def test_save_mode_single_quotes_and_spacing(config_file) -> None:
    path = config_file("  mode='normal'\n[ui]\n")
    config.save_mode("local")
    assert path.read_text(encoding="utf-8") == "  mode='normal'\n[ui]\n".replace("'normal'", '"local"')


def test_save_mode_inserts_first_line_before_section(config_file) -> None:
    path = config_file('[ui]\nwidth = 800\nmode = "normal"\n')  # mode в секции — не тот ключ
    config.save_mode("local")
    assert path.read_text(encoding="utf-8") == 'mode = "local"\n[ui]\nwidth = 800\nmode = "normal"\n'
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    assert data["mode"] == "local" and data["ui"]["mode"] == "normal"
    assert config.load().mode == "local"


def test_save_mode_inserts_into_comment_only_file(config_file) -> None:
    path = config_file("# пусто\n")
    config.save_mode("local")
    assert path.read_text(encoding="utf-8") == 'mode = "local"\n# пусто\n'


def test_save_mode_keeps_line_endings(config_file) -> None:
    # оба случая в одном тесте: на каждой ОС ломается ровно один из них
    path = config_file("")
    for nl in (b"\r\n", b"\n"):
        path.write_bytes(b'mode = "normal"' + nl + b"[ui]" + nl + b"width = 800" + nl)
        config.save_mode("local")
        assert path.read_bytes() == b'mode = "local"' + nl + b"[ui]" + nl + b"width = 800" + nl


def test_save_mode_drops_bom(config_file) -> None:
    path = config_file("")
    path.write_bytes(b'\xef\xbb\xbfmode = "normal"\n')
    config.save_mode("local")
    assert not path.read_bytes().startswith(b"\xef\xbb\xbf")
    assert path.read_text(encoding="utf-8") == 'mode = "local"\n'  # перевод строки на Windows — CRLF


def test_save_mode_creates_from_example() -> None:
    path = _path()
    assert not path.exists()
    config.save_mode("local")
    assert path.read_text(encoding="utf-8") == EXAMPLE.replace('mode = "normal"', 'mode = "local"', 1)
    assert config.load().mode == "local"
    assert settings.load_pc_settings().aliases == {"телега": "Telegram", "хром": "Google Chrome"}


def test_save_mode_without_example(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    empty = tmp_path / "bundle"
    empty.mkdir()
    monkeypatch.setattr(settings, "app_root", lambda: empty)
    config.save_mode("local")
    assert _path().read_text(encoding="utf-8") == 'mode = "local"\n'


def test_save_mode_creates_parent_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "new" / "dir" / "jarvis.toml"
    monkeypatch.setenv("JARVIS_CONFIG", str(target))
    config.save_mode("local")
    assert tomllib.loads(target.read_text(encoding="utf-8"))["mode"] == "local"


def test_save_mode_rejects_unknown_mode(config_file) -> None:
    path = config_file('mode = "normal"\n')
    with pytest.raises(ValueError):
        config.save_mode("fast")  # type: ignore[arg-type]
    assert path.read_text(encoding="utf-8") == 'mode = "normal"\n'


def test_save_mode_is_atomic(config_file, monkeypatch: pytest.MonkeyPatch) -> None:
    path = config_file('mode = "normal"\n')
    calls: list[tuple[str, str]] = []
    real_replace = os.replace

    def replace(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        calls.append((str(src), str(dst)))
        real_replace(src, dst)

    monkeypatch.setattr(config.os, "replace", replace)
    config.save_mode("local")
    assert len(calls) == 1
    src, dst = calls[0]
    assert dst == str(path) and src != dst and Path(src).parent == path.parent
    assert sorted(p.name for p in path.parent.glob("jarvis.toml*")) == ["jarvis.toml"]


def test_save_mode_failure_keeps_old_file(config_file, monkeypatch: pytest.MonkeyPatch) -> None:
    path = config_file('mode = "normal"\n')

    def replace(src: str, dst: str) -> None:
        raise OSError("диск занят")

    monkeypatch.setattr(config.os, "replace", replace)
    with pytest.raises(OSError):
        config.save_mode("local")
    assert path.read_text(encoding="utf-8") == 'mode = "normal"\n'


def test_save_mode_failure_leaves_no_tmp(config_file, monkeypatch: pytest.MonkeyPatch) -> None:
    path = config_file('mode = "normal"\n')

    def replace(src: str, dst: str) -> None:
        raise OSError("диск занят")

    monkeypatch.setattr(config.os, "replace", replace)
    with pytest.raises(OSError):
        config.save_mode("local")
    assert sorted(p.name for p in path.parent.glob("jarvis.toml*")) == ["jarvis.toml"]


def test_save_mode_with_non_string_mode_line(config_file) -> None:
    path = config_file("mode = 5\n[ui]\nwidth = 800\n")
    config.save_mode("local")
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    assert data["mode"] == "local" and data["ui"]["width"] == 800
