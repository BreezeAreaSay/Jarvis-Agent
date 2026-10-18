"""Голосовые из Telegram: OGG/Opus → float32 моно 16 кГц для распознавания.

Opus без внешних программ в Python не декодируется (miniaudio умеет Vorbis, но не Opus), поэтому нужен
ffmpeg: из PATH или <data>/tools/ffmpeg(.exe). Нет ffmpeg — бот вежливо просит прислать текст.
"""

import shutil
import subprocess
import sys
from typing import Any

from jarvis import paths

RATE = 16000
MAX_SECONDS = 120


class AudioDecodeError(RuntimeError):
    """Звук не декодировался; текст — для журнала."""


def find_ffmpeg() -> str | None:
    found = shutil.which("ffmpeg")
    if found:
        return found
    tools = paths.data_dir() / "tools"
    for name in ("ffmpeg.exe", "ffmpeg"):
        candidate = tools / name
        if candidate.is_file():
            return str(candidate)
    return None


def decode_with_ffmpeg(data: bytes, ffmpeg: str, rate: int = RATE, timeout: float = 60.0) -> Any:
    """Любой звук, который понимает ffmpeg → numpy float32 моно с частотой rate. Блокирующая функция."""
    import numpy as np

    command = [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        "pipe:0",
        "-t",
        str(MAX_SECONDS),
        "-f",
        "f32le",
        "-acodec",
        "pcm_f32le",
        "-ac",
        "1",
        "-ar",
        str(rate),
        "pipe:1",
    ]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    try:
        proc = subprocess.run(  # путь к ffmpeg найден нами, аргументы фиксированы
            command, input=data, capture_output=True, timeout=timeout, check=False, creationflags=flags
        )
    except (OSError, subprocess.SubprocessError) as e:
        raise AudioDecodeError(f"ffmpeg не запустился: {e}") from e
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", errors="replace").strip()[-300:]
        raise AudioDecodeError(f"ffmpeg: код {proc.returncode}: {detail}")
    usable = len(proc.stdout) - len(proc.stdout) % 4
    samples = np.frombuffer(proc.stdout[:usable], dtype=np.float32).copy()
    if samples.size == 0:
        raise AudioDecodeError("в голосовом нет звука")
    return samples
