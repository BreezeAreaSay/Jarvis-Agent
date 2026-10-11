"""Замер голоса без микрофона: 20 голосовых команд → речь (TTS) или свои записи → распознавание → роутер.

    uv run python scripts/voice_bench.py                     # синтез голосом [voice] tts_voice, потоки 2 и 4
    uv run python scripts/voice_bench.py --wav-dir D:\\voice  # свои записи 01.wav … 20.wav (16 кГц, моно)
    uv run python scripts/voice_bench.py --model vosk-small --threads 2

Нужны модели в <data>\\models (scripts\\get_voice_models.ps1). Действия ПК не выполняются: проверяется только,
что роутер и грамматика выбрали бы для распознанного текста то же, что для набранной команды. Инвентарь
приложений — tests/fixtures/apps.json (как у `jarvis bench`). Печать — только фразы из списка ниже и числа.
"""

import argparse
import array
import json
import random
import statistics
import sys
import time
import wave
from datetime import datetime
from pathlib import Path
from typing import Any

# 20 голосовых команд для S9 (их же владелец произносит руками — docs/manual-checks.md, раздел S9)
COMMANDS = [
    "открой телегу",
    "громкость тридцать",
    "сделай погромче",
    "выключи звук",
    "следующий трек",
    "поставь на паузу",
    "сверни все окна",
    "закрой блокнот",
    "открой загрузки",
    "найди отчёт за сентябрь",
    "который час",
    "заблокируй компьютер",
    "напомни через десять минут проверить почту",
    "что ты умеешь",
    "запусти дискорд",
    "переключись на хром",
    "посмотри на экран",
    "почему компьютер тормозит",
    "напиши письмо начальнику что я заболел",
    "какая погода в москве завтра",
]
PREFIXED = {0, 13}  # эти фразы говорятся с обращением «Джарвис, …» (его срезает asr.normalize)
RATE = 16000


def _utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None and not stream.isatty():
                stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, OSError, ValueError):
            pass


def to_pcm(samples: list[float], rate: int, noise: float, rng: random.Random) -> bytes:
    """float → 16 кГц int16 с тишиной по краям (как в записи PTT) и лёгким шумом микрофона."""
    from jarvis import asr

    s = asr.resample(samples, rate, RATE)
    s = [0.0] * int(0.35 * RATE) + s + [0.0] * int(0.5 * RATE)
    out = array.array("h")
    for x in s:
        v = x + rng.gauss(0.0, noise) if noise else x
        out.append(max(-32768, min(32767, int(v * 32767))))
    return out.tobytes()


def synth_all(voice: str, speed: float, noise: float) -> list[bytes]:
    from jarvis import tts

    sp = tts.Speaker(voice, speed=speed, threads=2)
    sp.load()
    rng = random.Random(9)
    clips = []
    for i, text in enumerate(COMMANDS):
        spoken = f"Джарвис, {text}." if i in PREFIXED else f"{text.capitalize()}."
        pcm, rate = sp._synth(spoken)
        floats = array.array("f")
        floats.frombytes(pcm)
        clips.append(to_pcm(list(floats), rate, noise, rng))
    return clips


def read_wavs(folder: Path) -> list[bytes]:
    clips = []
    for i in range(1, len(COMMANDS) + 1):
        path = folder / f"{i:02d}.wav"
        with wave.open(str(path), "rb") as w:
            if w.getframerate() != RATE or w.getnchannels() != 1 or w.getsampwidth() != 2:
                raise SystemExit(f"{path.name}: нужен WAV 16 кГц, моно, 16 бит")
            clips.append(w.readframes(w.getnframes()))
    return clips


def route_key(text: str) -> tuple[str, str, dict[str, Any]]:
    """(уровень, действие грамматики, аргументы) — что выбрал бы роутер для текста; время напоминания
    (due) не сравнивается — оно считается от «сейчас»."""
    from jarvis import router
    from jarvis.context import Context

    r = router.route(text, Context(), "normal")
    if r.hit is not None:
        return r.level, r.hit.action, {k: v for k, v in r.hit.args.items() if k != "due"}
    return r.level, "", {}


def same_route(typed: str, heard: str) -> bool:
    from jarvis import bench

    a, b = route_key(typed), route_key(heard)
    if a[0] != b[0]:
        return False
    if a[0] != "grammar":
        return True
    return bench.same_tool(a[1], b[1], a[2], b[2]) and not bench.bad_args(a[2], b[2])


def run(clips: list[bytes], model: str, threads: int, vad: bool) -> dict[str, Any]:
    from jarvis import asr
    from jarvis.mic import Recording

    rec = asr.Recognizer(model, threads=threads, vad=vad)
    rec.load()
    rows, times = [], []
    for text, pcm in zip(COMMANDS, clips, strict=True):
        result = rec.recognize(Recording(pcm))
        times.append(result.asr_ms)
        exact = result.text.replace("ё", "е").rstrip("?") == text.replace("ё", "е")
        rows.append(
            {
                "typed": text,
                "heard": result.text,
                "asr_ms": result.asr_ms,
                "audio_s": result.audio_s,
                "speech_s": result.speech_s,
                "exact": exact,
                "route_ok": same_route(text, result.text),
            }
        )
    return {
        "model": model,
        "threads": threads,
        "vad": vad,
        "load_ms": rec.load_ms,
        "route_ok": sum(r["route_ok"] for r in rows),
        "exact": sum(r["exact"] for r in rows),
        "p50": statistics.median(times),
        "p95": sorted(times)[max(0, int(len(times) * 0.95 + 0.5) - 1)],
        "audio_avg": statistics.mean(r["audio_s"] for r in rows),
        "rows": rows,
    }


def main() -> int:
    _utf8()
    from jarvis import bench, config

    cfg = config.load().voice
    ap = argparse.ArgumentParser(description="Замер распознавания на 20 голосовых командах")
    ap.add_argument("--model", default=cfg.model)
    ap.add_argument("--threads", type=int, nargs="+", default=[2, 4])
    ap.add_argument("--voice", default=cfg.tts_voice, help="голос синтеза команд")
    ap.add_argument("--speed", type=float, default=1.0)
    ap.add_argument("--noise", type=float, default=0.003, help="шум микрофона (СКО, доля полной шкалы)")
    ap.add_argument("--no-vad", action="store_true")
    ap.add_argument("--wav-dir", type=Path, help="свои записи 01.wav … 20.wav вместо синтеза")
    ap.add_argument("--save", action="store_true", help="итог в bench/results/voice-<время>.json")
    args = ap.parse_args()

    bench.fixture_inventory()
    t = time.perf_counter()
    clips = read_wavs(args.wav_dir) if args.wav_dir else synth_all(args.voice, args.speed, args.noise)
    source = f"записи {args.wav_dir}" if args.wav_dir else f"синтез {args.voice} ×{args.speed}"
    print(f"источник: {source}, {len(clips)} фраз за {time.perf_counter() - t:.1f} с")
    reports = []
    for threads in args.threads:
        rep = run(clips, args.model, threads, not args.no_vad)
        reports.append(rep)
        print(
            f"\n{rep['model']}, потоков {threads}, VAD {'да' if rep['vad'] else 'нет'}: "
            f"роутер выбрал то же {rep['route_ok']}/{len(COMMANDS)}, "
            f"дословно {rep['exact']}/{len(COMMANDS)}; "
            f"распознавание p50 {rep['p50']:.0f} мс, p95 {rep['p95']:.0f} мс "
            f"(средняя запись {rep['audio_avg']:.1f} с; загрузка {rep['load_ms']:.0f} мс)"
        )
        for row in rep["rows"]:
            mark = "✓" if row["route_ok"] else "✗"
            same = "" if row["exact"] else f" → «{row['heard']}»"
            print(f"  {mark} {row['asr_ms']:5.0f} мс  {row['typed']}{same}")
    if args.save:
        out = Path(__file__).resolve().parents[1] / "bench" / "results"
        out.mkdir(parents=True, exist_ok=True)
        path = out / f"voice-{datetime.now():%Y%m%d-%H%M%S}.json"
        path.write_text(json.dumps(reports, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\nсохранено: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
