"""scripts/ru_dict/build_ru_dict.py: правила знака ударения, «ё» из eyo-kernel, сборка ru_extra, загрузки
с SHA-256, данные в репозитории и лицензия папки.

Сама компиляция (libespeak-ng, сеть) здесь не идёт — её делает сборка установщика (scripts/build-core-win.sh,
scripts/build-win.ps1).
"""

import hashlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[3]


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "build_ru_dict", ROOT / "scripts" / "ru_dict" / "build_ru_dict.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["build_ru_dict"] = mod
    spec.loader.exec_module(mod)
    return mod


B = _load()
A = B.ACCENT

RULES = """// фрагмент ru_rules
.group а
       а       a
  A) а (_      a
.group г
       г (о_   v
       г       g
"""


def test_accent_rules_add_copies_only() -> None:
    text, added = B.accent_rules(RULES)
    lines = text.splitlines()
    assert lines[: len(RULES.splitlines())] != [] and all(line in lines for line in RULES.splitlines())
    assert f"       а{A}       'a" in lines  # ударная «а́»: образец длиннее, фонемы с «'»
    assert f"  A) а{A} (_      'a" in lines
    assert all(f"  {v}{A}) а (_      a" in lines for v in B.VOWELS)  # класс гласных перед образцом
    assert f"       г (о{A}_   v" in lines  # «его́»: знак в правом контексте не ломает правило
    assert added == len(lines) - len(RULES.splitlines())
    assert B.accent_rules(text)[1] == added  # строки со знаком повторно не копируются


def test_yo_words() -> None:
    safe = "ещё\nчёрн(ый|ая)\nПётр\nвсё-таки\nёлк(а|и)\nмед\n"
    assert B.yo_words(safe) == {
        "еще": "ещё",
        "черный": "чёрный",
        "черная": "чёрная",
        "все-таки": "всё-таки",
        "елка": "ёлка",
        "елки": "ёлки",
    }


def test_stress_lines_and_extra_text() -> None:
    lines = B.stress_lines("# комментарий\nзамок\t$2\n\nутра\t$2\n")
    assert lines == ["замок\t$2", "утра\t$2"]
    with pytest.raises(SystemExit, match="слово<TAB>"):
        B.stress_lines("замок $2\n")
    extra = B.extra_text(lines, {"еще": "ещё"}, "// ручные\nуже     $2\n").splitlines()
    assert extra[1:3] == lines
    assert "еще\tещё\t$text" in extra and extra[-1] == "уже     $2"
    assert extra[0].startswith("//") and extra[3].startswith("//")


def test_committed_data_is_valid() -> None:
    stress = B.stress_lines((B.DATA / "stress.tsv").read_text(encoding="utf-8"))
    words = [line.split("\t")[0] for line in stress]
    assert len(stress) == 29700 and len(set(words)) == len(words) and words == sorted(words)
    manual = (B.DATA / "manual.txt").read_text(encoding="utf-8").splitlines()
    assert "утра\t$2" in manual and any(line.startswith("уже ") for line in manual)
    head = (B.DATA / "stress.tsv").read_text(encoding="utf-8").splitlines()[:5]
    assert any("CC BY-SA 4.0" in line for line in head)  # атрибуция OpenRussian


def test_fetch_checks_sha256(tmp_path: Path) -> None:
    src = tmp_path / "src.txt"
    src.write_bytes(b"ru_rules")
    good = hashlib.sha256(b"ru_rules").hexdigest()
    dest = tmp_path / "cache" / "ru_rules"
    assert B.fetch(src.as_uri(), good, dest).read_bytes() == b"ru_rules"
    src.write_bytes(b"other")  # в кэше то же — сеть не нужна
    assert B.fetch(src.as_uri(), good, dest).read_bytes() == b"ru_rules"
    with pytest.raises(SystemExit, match="SHA-256"):
        B.fetch(src.as_uri(), good, tmp_path / "cache" / "fresh")
    assert not (tmp_path / "cache" / "fresh").exists() and not (tmp_path / "cache" / "fresh.part").exists()


def test_downloads_are_pinned() -> None:
    urls = [B.ESPEAK_DATA[0], B.EYO_SAFE[0], *(w[0] for w in B.WHEELS.values())]
    urls += [B.DICTSOURCE_URL + name for name in B.DICTSOURCE]
    assert all(u.startswith("https://") for u in urls)
    assert B.ESPEAK_COMMIT in B.DICTSOURCE_URL and B.EYO_COMMIT in B.EYO_SAFE[0]
    digests = [B.ESPEAK_DATA[1], B.EYO_SAFE[1], *B.DICTSOURCE.values(), *(w[1] for w in B.WHEELS.values())]
    assert all(len(d) == 64 and int(d, 16) >= 0 for d in digests)


def test_folder_has_its_own_license() -> None:
    text = (B.DATA / "LICENSE.md").read_text(encoding="utf-8")
    assert "GPL" in text and "CC BY-SA 4.0" in text and "OpenRussian" in text and "eyo-kernel" in text
    assert (
        (B.DATA / "build_ru_dict.py")
        .read_text(encoding="utf-8")
        .startswith("# SPDX-License-Identifier: GPL-3.0-or-later")
    )
    assert "scripts/ru_dict" in (ROOT / "LICENSE.md").read_text(encoding="utf-8")  # исключение из PolyForm
    assert set(B.PHONEME_TABLES) == {"phontab", "phonindex", "phondata"}
