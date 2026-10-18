"""Голоса Windows: WinRT Windows.Media.SpeechSynthesis (голоса OneCore: Irina, Pavel…) → WAV; запасной путь —
SAPI 5 через comtypes (SAPI.SpVoice в WAV-файл). Только на Windows; всё — лениво и в своём потоке
(у потоков ядра COM в режиме STA, а WinRT удобнее звать из потока без него, с собственным циклом asyncio).

Тон и скорость: WinRT — SpeechSynthesizerOptions.AudioPitch / SpeakingRate (Windows 10 1803+), SAPI — Rate,
тон — частотой воспроизведения.
"""

import asyncio
import contextlib
import logging
import os
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any

import numpy as np

from jarvis.voice.dsp import decode_wav
from jarvis.voice.engines.base import SynthOptions, TtsEngineError, clamp, gender_name

if TYPE_CHECKING:
    from jarvis.settings import Settings

log = logging.getLogger(__name__)

SSFM_CREATE_FOR_WRITE = 3
LCIDS = {"419": "ru-RU", "409": "en-US", "809": "en-GB", "407": "de-DE", "40c": "fr-FR", "422": "uk-UA"}
RU_HINTS = ("irina", "pavel", "svetlana", "dmitry", "ekaterina", "ирина", "павел", "ru-ru", "russian")


def _pick_winrt_voice(voices: Any, wanted: str) -> Any:
    items = list(voices)
    query = wanted.strip().casefold()
    if query:
        for v in items:
            if query in str(v.display_name).casefold() or query == str(v.id).casefold():
                return v
    for v in items:
        if str(v.language).casefold().startswith("ru"):
            return v
    return None


def _winrt_synth(text: str, voice: str, speed: float, pitch: float) -> tuple[bytes, bool]:
    """WAV и признак «тон применён самим синтезатором»."""
    from winrt.windows.media.speechsynthesis import SpeechSynthesizer  # type: ignore[import-not-found]
    from winrt.windows.storage.streams import DataReader  # type: ignore[import-not-found]

    async def run() -> tuple[bytes, bool]:
        synth = SpeechSynthesizer()
        chosen = _pick_winrt_voice(SpeechSynthesizer.all_voices, voice)
        if chosen is not None:
            synth.voice = chosen
        native_pitch = False
        try:
            synth.options.speaking_rate = clamp(speed, 0.5, 6.0)
            synth.options.audio_pitch = clamp(pitch, 0.0, 2.0)
            native_pitch = True
        except Exception:
            log.debug("голос: SpeechSynthesizerOptions недоступны (старая Windows)")
        stream = await synth.synthesize_text_to_stream_async(text)
        size = int(stream.size)
        reader = DataReader(stream.get_input_stream_at(0))
        await reader.load_async(size)
        try:
            data = bytes(memoryview(reader.read_buffer(size)))
        except Exception:
            buf = bytearray(size)
            reader.read_bytes(buf)
            data = bytes(buf)
        return data, native_pitch

    return asyncio.run(run())


def _sapi_voice(sp: Any, wanted: str) -> None:
    query = wanted.strip().casefold()
    tokens = list(sp.GetVoices())
    if query:
        for token in tokens:
            if query in str(token.GetDescription()).casefold():
                sp.Voice = token
                return
    for token in tokens:
        with contextlib.suppress(Exception):
            if "419" in str(token.GetAttribute("Language")).casefold():
                sp.Voice = token
                return


def _sapi_synth(text: str, voice: str, speed: float) -> tuple[np.ndarray, int]:
    import comtypes  # type: ignore[import-not-found]
    import comtypes.client  # type: ignore[import-not-found]

    with contextlib.suppress(Exception):
        comtypes.CoInitialize()
    sp = comtypes.client.CreateObject("SAPI.SpVoice")
    _sapi_voice(sp, voice)
    sp.Rate = int(clamp(round((speed - 1.0) * 10), -10, 10))
    fd, path = tempfile.mkstemp(prefix="jarvis-sapi-", suffix=".wav")
    os.close(fd)
    try:
        stream = comtypes.client.CreateObject("SAPI.SpFileStream")
        stream.Open(path, SSFM_CREATE_FOR_WRITE, False)
        try:
            sp.AudioOutputStream = stream
            sp.Speak(text, 0)
        finally:
            stream.Close()
        with open(path, "rb") as f:
            data = f.read()
    finally:
        with contextlib.suppress(OSError):
            os.remove(path)
    return decode_wav(data)


def _thread_init() -> None:
    return None


class WindowsEngine:
    name = "windows"
    title = "Голоса Windows"

    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(1, thread_name_prefix="win-tts", initializer=_thread_init)
        self._langs: dict[str, str] = {}

    def available(self) -> str:
        return "" if sys.platform == "win32" else "голоса Windows есть только в Windows"

    def voice(self, settings: "Settings") -> str:
        return settings.tts.windows_voice

    def russian(self, voice: str) -> bool:
        if not voice:
            return True  # голос по умолчанию у русского пользователя — русский
        lang = self._langs.get(voice)
        if lang:
            return lang.casefold().startswith("ru")
        return any(h in voice.casefold() for h in RU_HINTS)

    async def synthesize(self, text: str, opts: SynthOptions) -> tuple[np.ndarray, int]:
        if sys.platform != "win32":
            raise TtsEngineError("голоса Windows есть только в Windows")
        loop = asyncio.get_running_loop()
        args = (text, opts.voice, opts.speed, opts.pitch)
        return await loop.run_in_executor(self._executor, self._synth, *args)

    def _synth(self, text: str, voice: str, speed: float, pitch: float) -> tuple[np.ndarray, int]:
        errors: list[str] = []
        try:
            data, native_pitch = _winrt_synth(text, voice, speed, pitch)
            samples, rate = decode_wav(data)
            if not native_pitch:
                rate = max(8000, round(rate * clamp(pitch, 0.8, 1.2)))
            return samples, rate
        except Exception as e:
            errors.append(f"WinRT: {e}")
            log.info("голос: WinRT не сработал (%s) — пробую SAPI", e)
        try:
            samples, rate = _sapi_synth(text, voice, speed)
            return samples, max(8000, round(rate * clamp(pitch, 0.8, 1.2)))
        except Exception as e:
            errors.append(f"SAPI: {e}")
        raise TtsEngineError("голос Windows не сработал: " + "; ".join(errors))

    async def voices(self) -> list[dict[str, Any]]:
        if sys.platform != "win32":
            return []
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, self._list_voices)

    def _list_voices(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        try:
            from winrt.windows.media.speechsynthesis import SpeechSynthesizer  # type: ignore[import-not-found]

            for v in SpeechSynthesizer.all_voices:
                name = str(v.display_name)
                items.append(
                    {"id": name, "name": name, "lang": str(v.language), "gender": gender_name(int(v.gender))}
                )
        except Exception as e:
            log.info("голос: голоса WinRT не получены: %s", e)
        if not items:
            try:
                import comtypes  # type: ignore[import-not-found]
                import comtypes.client  # type: ignore[import-not-found]

                with contextlib.suppress(Exception):
                    comtypes.CoInitialize()
                sp = comtypes.client.CreateObject("SAPI.SpVoice")
                for token in sp.GetVoices():
                    name = str(token.GetDescription())
                    lcid = ""
                    gender = ""
                    with contextlib.suppress(Exception):
                        lcid = str(token.GetAttribute("Language")).split(";")[0].strip().casefold()
                    with contextlib.suppress(Exception):
                        gender = gender_name(token.GetAttribute("Gender"))
                    items.append({"id": name, "name": name, "lang": LCIDS.get(lcid, lcid), "gender": gender})
            except Exception as e:
                log.info("голос: голоса SAPI не получены: %s", e)
        items.sort(key=lambda i: (not str(i["lang"]).casefold().startswith("ru"), i["name"]))
        self._langs = {i["id"]: str(i["lang"]) for i in items}
        return items

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
