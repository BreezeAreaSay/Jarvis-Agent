# SPDX-License-Identifier: GPL-3.0-or-later
"""Словарь ударений и «ё» для русских голосов piper в Jarvis → build/voice/ru_dict (espeak-ng в sherpa-onnx).

Голоса piper читают текст через espeak-ng, а его русский словарь ставит ударение иначе, чем словари, примерно
в 6 % форм слов, не знает «ё» в словах, написанных через «е» («еще», «черный»), и пропускает знак ударения
U+0301. Скрипт собирает ru_dict заново (согласие ударений с OpenRussian — 94,2 → 99,6 %, «ё» — 106 тыс. слов):
- исходники словаря — из форка espeak-ng, с которым собран sherpa-onnx (csukuangfj/espeak-ng@ed530aa,
  GPL-3.0);
- правила для знака ударения: «за́мок» и «замо́к» читаются по-разному (accent_rules);
- stress.tsv — ~29,7 тыс. форм, где OpenRussian (CC BY-SA 4.0) и Silero Stress (MIT) согласны, а espeak-ng —
  нет;
- «ё» по словарю eyo-kernel (MIT, только бесспорные слова safe.txt): «еще» читается как «ещё»;
- manual.txt — ручные правки и выбор по умолчанию для частых омографов.
Компилятор — libespeak-ng из колеса PyPI espeakng-loader через ctypes (на Windows — espeak-ng.dll, права
администратора не нужны); фонемные таблицы — espeak-ng-data из релиза sherpa-onnx tts-models (те же файлы,
что у голосов: с чужими таблицами словарь несовместим). Всё скачанное сверяется по SHA-256, загрузки — в кэше
<out>/cache.

    python scripts/ru_dict/build_ru_dict.py [--out build/voice]

Результат: <out>/ru_dict и <out>/ru_dict.json (SHA-256, счётчики и SHA-256 фонемных таблиц, с которыми словарь
собран). Сборка установщика (scripts/build-core-win.sh, scripts/build-win.ps1) кладёт их в
jarvis/resources/voice вместе с LICENSE.md этой папки; ядро ставит словарь в espeak-ng-data голоса при его
загрузке, если фонемные таблицы голоса те же (settings.tts.stress_dict, jarvis/voice/stress.py).
Только стандартная библиотека Python; путь <out> — латиницей (espeak-ng на Windows иные пути не открывает).

Лицензия этой папки — LICENSE.md рядом (не PolyForm Noncommercial остального Jarvis).
"""

import argparse
import ctypes
import hashlib
import json
import os
import re
import shutil
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

DATA = Path(__file__).resolve().parent
ROOT = DATA.parents[1]
PHONEME_TABLES = ("phontab", "phonindex", "phondata")  # с ними собран словарь: у голоса должны быть те же

ESPEAK_COMMIT = "ed530aa113046142eb5115cf2fc9157854d0ffe1"  # sherpa-onnx cmake/espeak-ng-for-piper.cmake
DICTSOURCE_URL = f"https://raw.githubusercontent.com/csukuangfj/espeak-ng/{ESPEAK_COMMIT}/dictsource/"
DICTSOURCE = {  # путь в dictsource форка → SHA-256
    "ru_rules": "28883df5cf8ab6a8a7b4d8fd1704be623b1c313a9a8f02a5cbdc6afcd763b64e",
    "ru_list": "26e414406834b830bd5e65891a9e61ca941aa6c2e56fe2f6b5c02e541d3b1880",
    "extra/ru_listx": "0e443987ba5ce0fb8c7336420028eadbc8a03e10d2c77a0fc895bf459e307d79",
    "ru_emoji": "1e82a6dd526d3762274a3b5935decfa3c3db528bc1dea5b8d6ea017f13b4af35",
}
ESPEAK_DATA = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/espeak-ng-data.tar.bz2",
    "4135ccf82e1f40613491c0874d4945ae9e9c7840933d8e25a6f9e003d9ebf533",
)
EYO_COMMIT = "95577dfb33127956d1f5bd70d898e2af0df8cd97"  # eyo-kernel 4.1.3
EYO_SAFE = (
    f"https://raw.githubusercontent.com/hcodes/eyo-kernel/{EYO_COMMIT}/dictionary/safe.txt",
    "11fc6d9c3cc6d0fff6a21cd642fa6aecbf3f9eb784f84ccd78141f32e51e0693",
)
_PYPI = "https://files.pythonhosted.org/packages/"
WHEELS = {  # espeakng-loader 0.2.4: (адрес, SHA-256 с PyPI, библиотека внутри колеса)
    "linux": (
        _PYPI + "de/1e/25ec5ab07528c0fbb215a61800a38eca05c8a99445515a02d7fa5debcb32/"
        "espeakng_loader-0.2.4-py3-none-manylinux_2_17_x86_64.manylinux2014_x86_64.whl",
        "08721baf27d13d461f6be6eed9a65277e70d68234ff484fd8b9897b222cdcb6d",
        "espeakng_loader/libespeak-ng.so",
    ),
    "win32": (
        _PYPI + "9d/ed/a3d872fbad4f3a3f3db0e8c31768ab14e77cd77306de16b8b20b1e1df7ea/"
        "espeakng_loader-0.2.4-py3-none-win_amd64.whl",
        "41f1e08ac9deda2efd1ea9de0b81dab9f5ae3c4b24284f76533d0a7b1dd7abd7",
        "espeakng_loader/espeak-ng.dll",
    ),
}

ACCENT = "́"  # знак ударения: «за́мок»
VOWELS = "аеёиоуыэюя"
CLASSES = {"A": VOWELS, "Y": "еёиюя"}  # классы гласных ru_rules, которые бывают перед образцом
# строка правила: [условие] [левый контекст)] образец [(правый контекст] [фонемы] [// комментарий]
RULE = re.compile(r"^(\s*)(\?\d+\s+)?(?:(\S+)\)\s+)?(\S+)(?:\s+\((\S+))?(?:(\s+)([^/\s]\S*))?(\s*(?://.*)?)$")
EYO_LINE = re.compile(r"^([^()]*)\(([^()]*)\)([^()]*)$")
STRESS_LINE = re.compile(r"[а-яё-]+\t\$[1-9]")


# --- загрузки -------------------------------------------------------------------------------------------


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def fetch(url: str, digest: str, dest: Path) -> Path:
    """Файл из кэша или из сети; SHA-256 не сошёлся — ошибка (подмена или битая загрузка)."""
    if dest.is_file() and sha256(dest) == digest:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    print(f"  скачиваю {url}")
    with urllib.request.urlopen(url, timeout=120) as r, tmp.open("wb") as f:  # только https; SHA-256 ниже
        shutil.copyfileobj(r, f)
    got = sha256(tmp)
    if got != digest:
        tmp.unlink()
        raise SystemExit(f"SHA-256 не совпал: {url}\n  ждали {digest}\n  пришло {got}")
    tmp.replace(dest)
    return dest


def espeak_library(cache: Path) -> Path:
    """libespeak-ng (Linux) или espeak-ng.dll (Windows) из колеса espeakng-loader."""
    key = "win32" if sys.platform == "win32" else "linux"
    url, digest, member = WHEELS[key]
    wheel = fetch(url, digest, cache / url.rsplit("/", 1)[1])
    lib = cache / f"espeakng-{key}" / Path(member).name
    if not lib.is_file():
        lib.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(wheel) as z:
            lib.write_bytes(z.read(member))
    return lib


def espeak_data(cache: Path) -> Path:
    """espeak-ng-data релиза sherpa-onnx (фонемные таблицы голосов piper) — распакованная в кэш."""
    url, digest = ESPEAK_DATA
    archive = fetch(url, digest, cache / "espeak-ng-data.tar.bz2")
    root = cache / f"espeak-ng-data-{digest[:12]}"
    data = root / "espeak-ng-data"
    if not (data / "phontab").is_file():
        shutil.rmtree(root, ignore_errors=True)
        root.mkdir(parents=True)
        with tarfile.open(archive, "r:bz2") as t:
            t.extractall(root, filter="data")
    return data


# --- исходники словаря ----------------------------------------------------------------------------------


def _accent_variants(ctx: str, before: bool) -> list[str]:
    """Контекст с ударной гласной: «Cе» → «Cе́»; класс гласных прямо перед образцом → каждая «гласная + ́»."""
    out = []
    for i, c in enumerate(ctx):
        if c in VOWELS:
            out.append(ctx[: i + 1] + ACCENT + ctx[i + 1 :])
        elif before and c in CLASSES and i == len(ctx) - 1:
            out += [ctx[:i] + v + ACCENT + ctx[i + 1 :] for v in CLASSES[c]]
    return out


def _rule(
    indent: str, cond: str, pre: str | None, match: str, post: str | None, sp: str, ph: str, tail: str
) -> str:
    s = f"{indent}{cond}" + (f"{pre}) " if pre else "") + match + (f" ({post}" if post else "")
    return s + (f"{sp or '    '}{ph}" if ph else "") + tail


def _accent_copies(line: str, group: str | None) -> list[str]:
    """Копии правила для слов со знаком ударения (см. accent_rules)."""
    m = RULE.match(line)
    if not m or not line.strip() or line.lstrip().startswith(("//", ".")) or ACCENT in line:
        return []
    indent, cond, pre, match, post, sp, ph, tail = m.groups()
    cond, ph, tail = cond or "", ph or "", tail or ""
    out = []
    if group is not None and group in VOWELS and match == group and ph:
        out.append(_rule(indent, cond, pre, match + ACCENT, post, sp, ph if "'" in ph else "'" + ph, tail))
    for p in _accent_variants(pre, True) if pre else []:
        out.append(_rule(indent, cond, p, match, post, sp, ph, tail))
    for q in _accent_variants(post, False) if post else []:
        out.append(_rule(indent, cond, pre, match, q, sp, ph, tail))
    return out


def accent_rules(rules: str) -> tuple[str, int]:
    """ru_rules + знак ударения U+0301. Без правки espeak-ng знак пропускает, а слово со знаком не находит
    в словаре — читает по правилам, ударение где придётся. Правка — только добавление правил:
    1) правило группы гласной (образец — сама гласная) копируется с образцом «гласная + ́» и знаком
       ударения «'» перед фонемами: образец длиннее — копия выигрывает, остальные гласные редуцируются как
       обычно;
    2) знак внутри контекста ломает правила вроде «г (о_» (его́) или «A) е» (игра́ет): правилу с гласной или
       классом гласных в контексте добавляются копии с «гласная + ́».
    Возвращает (текст, сколько правил добавлено)."""
    out: list[str] = []
    added = 0
    group: str | None = None
    for line in rules.splitlines():
        out.append(line)
        parts = line.split()
        if parts[:1] == [".group"]:
            group = parts[1] if len(parts) > 1 else None
            continue
        if group is not None:
            extra = _accent_copies(line, group)
            out += extra
            added += len(extra)
    return "\n".join(out) + "\n", added


def yo_words(safe: str) -> dict[str, str]:
    """eyo-kernel safe.txt («основа(оконч1|оконч2)» или слово) → {написание через «е»: написание с «ё»};
    имена собственные (с заглавной) пропускаются."""
    out: dict[str, str] = {}
    for raw in safe.splitlines():
        line = raw.strip()
        if not line or line[0].isupper():
            continue
        m = EYO_LINE.match(line)
        words = [m.group(1) + alt + m.group(3) for alt in m.group(2).split("|")] if m else [line]
        for w in words:
            if "ё" in w and re.fullmatch(r"[а-яё-]+", w):
                out[w.replace("ё", "е")] = w
    return out


def stress_lines(text: str) -> list[str]:
    """stress.tsv без комментариев «#»; строка не «слово<TAB>$N» — ошибка (словарь собрался бы с мусором)."""
    lines = [s for s in text.splitlines() if s.strip() and not s.startswith("#")]
    bad = [s for s in lines if not STRESS_LINE.fullmatch(s)]
    if bad:
        raise SystemExit(f"stress.tsv: строки не в формате «слово<TAB>$N»: {bad[:3]}")
    return lines


def extra_text(stress: list[str], yo: dict[str, str], manual: str) -> str:
    """ru_extra: ударения, затем «ё» ($text — читать как написание с «ё»), затем ручные правки."""
    parts = [
        "// Jarvis: ударения, где OpenRussian (CC BY-SA 4.0) и Silero Stress (MIT) согласны, а espeak-ng нет"
    ]
    parts += stress
    parts.append(
        "// Jarvis: «ё» по словарю eyo-kernel (MIT, только бесспорные слова): «еще» читается как «ещё»"
    )
    parts += [f"{e}\t{yo[e]}\t$text" for e in sorted(yo)]
    return "\n".join(parts) + "\n" + manual


# --- компиляция -----------------------------------------------------------------------------------------


def compile_dict(lib_path: Path, data: Path, src: Path) -> None:
    """espeak_ng_CompileDictionary: исходники src/ru_* → data/ru_dict (фонемы — таблицы из data)."""
    for p in (lib_path, data, src):
        if not str(p).isascii():
            raise SystemExit(f"путь не латиницей — espeak-ng его не откроет: {p}")
    lib = ctypes.CDLL(str(lib_path))
    lib.espeak_ng_InitializePath.argtypes = [ctypes.c_char_p]
    lib.espeak_ng_InitializePath.restype = None
    lib.espeak_ng_Initialize.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
    lib.espeak_ng_Initialize.restype = ctypes.c_int
    lib.espeak_ng_SetVoiceByName.argtypes = [ctypes.c_char_p]
    lib.espeak_ng_SetVoiceByName.restype = ctypes.c_int
    lib.espeak_ng_CompileDictionary.argtypes = [
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    lib.espeak_ng_CompileDictionary.restype = ctypes.c_int
    ctx = ctypes.c_void_p()
    lib.espeak_ng_InitializePath(os.fsencode(data))
    steps = (
        ("espeak_ng_Initialize", lambda: lib.espeak_ng_Initialize(ctypes.byref(ctx))),
        ("espeak_ng_SetVoiceByName", lambda: lib.espeak_ng_SetVoiceByName(b"ru")),
        (
            "espeak_ng_CompileDictionary",
            lambda: lib.espeak_ng_CompileDictionary(
                os.fsencode(src) + os.fsencode(os.sep), b"ru", None, 0, ctypes.byref(ctx)
            ),
        ),
    )
    for name, call in steps:
        status = call()
        if status != 0:
            raise SystemExit(f"{name}: код 0x{status & 0xFFFFFFFF:08x}")


def build(out: Path) -> dict[str, object]:
    cache = out / "cache"
    lib = espeak_library(cache)
    data = espeak_data(cache)
    for name, digest in DICTSOURCE.items():
        fetch(DICTSOURCE_URL + name, digest, cache / "dictsource" / Path(name).name)
    safe = fetch(*EYO_SAFE, cache / "eyo-safe.txt").read_text(encoding="utf-8")
    stress = stress_lines((DATA / "stress.tsv").read_text(encoding="utf-8"))
    yo = yo_words(safe)
    manual = (DATA / "manual.txt").read_text(encoding="utf-8")
    rules, added = accent_rules((cache / "dictsource" / "ru_rules").read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(prefix="ru-dict-", dir=out) as tmp:
        src, work = Path(tmp) / "src", Path(tmp) / "espeak-ng-data"
        src.mkdir()
        for name in ("ru_list", "ru_listx", "ru_emoji"):
            shutil.copyfile(cache / "dictsource" / name, src / name)
        (src / "ru_rules").write_text(rules, encoding="utf-8")
        (src / "ru_extra").write_text(extra_text(stress, yo, manual), encoding="utf-8")
        shutil.copytree(data, work)
        compile_dict(lib, work, src)
        built = work / "ru_dict"
        if built.stat().st_size <= (data / "ru_dict").stat().st_size:
            raise SystemExit("ru_dict не вырос — дополнения не попали в словарь")
        shutil.copyfile(built, out / "ru_dict")
    info: dict[str, object] = {
        "sha256": sha256(out / "ru_dict"),
        "size": (out / "ru_dict").stat().st_size,
        "stress": len(stress),
        "yo": len(yo),
        "accent_rules": added,
        "espeak_ng": ESPEAK_COMMIT,
        "eyo_kernel": EYO_COMMIT,
        "phoneme_tables": {name: sha256(data / name) for name in PHONEME_TABLES},
        "license": "GPL-3.0-or-later; данные ударений — CC BY-SA 4.0 (OpenRussian), см. LICENSE.md",
    }
    shutil.copyfile(DATA / "LICENSE.md", out / "LICENSE.md")
    (out / "ru_dict.json").write_text(json.dumps(info, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return info


def main() -> None:
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=ROOT / "build" / "voice", help="куда положить ru_dict")
    out = ap.parse_args().out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    info = build(out)
    n = {k: f"{v:,}".replace(",", " ") for k, v in info.items() if isinstance(v, int)}
    print(
        f"ru_dict: {n['size']} байт; ударений {n['stress']}, «ё» {n['yo']}, правил знака ударения "
        f"{n['accent_rules']} → {out / 'ru_dict'}"
    )


if __name__ == "__main__":
    main()
