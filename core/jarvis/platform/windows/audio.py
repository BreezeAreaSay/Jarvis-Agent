"""Громкость: общая (IAudioEndpointVolume устройства вывода по умолчанию) и программ (сессии микшера).

pycaw — только как описание COM-интерфейсов: AudioUtilities.GetSpeakers() на каждый вызов читает все свойства
устройства (медленно), поэтому устройство берётся напрямую через IMMDeviceEnumerator. Интерфейс громкости
кэшируется для потока (COM-объекты не переходят между потоками) и раз в DEVICE_CHECK_S сверяется с устройством
по умолчанию: подключили наушники — громкость меняется у них. Ошибка COM — интерфейс берётся заново.
"""

import logging
import threading
import time
from collections.abc import Callable
from typing import Any, TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")

DEVICE_CHECK_S = 2.0
E_NOTFOUND = 0x80070490  # HRESULT_FROM_WIN32(ERROR_NOT_FOUND): нет устройства вывода
E_RENDER = 0  # EDataFlow.eRender
ROLE_MULTIMEDIA = 1  # ERole.eMultimedia
DEVICE_STATE_ACTIVE = 0x1
CLSCTX_ALL = 0x7  # comtypes.CLSCTX_ALL, как в pycaw


class NoAudioDevice(Exception):
    """Нет активного устройства вывода звука."""


def level_to_scalar(level: int) -> float:
    return max(0, min(100, int(level))) / 100.0


def scalar_to_level(value: float) -> int:
    return max(0, min(100, round(float(value) * 100)))


def volume_key_presses(target: int, steps_per_key: int = 2) -> tuple[int, int]:
    """Запасной путь без COM: (нажатий «тише», нажатий «громче») — сначала до нуля, потом до target.

    Одно нажатие клавиши громкости в Windows — 2 %.
    """
    target = max(0, min(100, int(target)))
    return 100 // steps_per_key, round(target / steps_per_key)


def _hresult(error: BaseException) -> int:
    value = getattr(error, "hresult", None)
    if value is None and error.args and isinstance(error.args[0], int):
        value = error.args[0]
    return int(value or 0) & 0xFFFFFFFF


class Audio:
    """COM-слой громкости. Вызывать в потоке с CoInitialize (поток платформы)."""

    def __init__(self) -> None:
        self._local = threading.local()

    # --- устройство

    def _enumerator(self) -> Any:
        st = self._local
        if getattr(st, "enumerator", None) is None:
            import comtypes  # type: ignore[import-not-found]
            from pycaw.api.mmdeviceapi import IMMDeviceEnumerator  # type: ignore[import-not-found]
            from pycaw.constants import CLSID_MMDeviceEnumerator  # type: ignore[import-not-found]

            st.enumerator = comtypes.CoCreateInstance(
                CLSID_MMDeviceEnumerator, IMMDeviceEnumerator, comtypes.CLSCTX_INPROC_SERVER
            )
        return st.enumerator

    def _default_device(self) -> Any:
        from _ctypes import COMError  # type: ignore[import-not-found]

        try:
            device = self._enumerator().GetDefaultAudioEndpoint(E_RENDER, ROLE_MULTIMEDIA)
        except COMError as e:
            if _hresult(e) == E_NOTFOUND:
                raise NoAudioDevice() from e
            raise
        if not device:
            raise NoAudioDevice()
        return device

    def _endpoint(self, force: bool = False) -> Any:
        """IAudioEndpointVolume устройства по умолчанию (кэш потока, проверка смены устройства)."""
        st = self._local
        now = time.monotonic()
        volume = getattr(st, "volume", None)
        if not force and volume is not None and now - getattr(st, "checked", 0.0) < DEVICE_CHECK_S:
            return volume
        from pycaw.api.endpointvolume import IAudioEndpointVolume  # type: ignore[import-not-found]

        device = self._default_device()
        device_id = str(device.GetId())
        if force or volume is None or device_id != getattr(st, "device_id", None):
            iface = device.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
            st.volume = iface.QueryInterface(IAudioEndpointVolume)
            st.device_id = device_id
            log.debug("устройство вывода: %s", device_id)
        st.checked = now
        return st.volume

    def _call(self, fn: Callable[[Any], T]) -> T:
        """fn(IAudioEndpointVolume); ошибка COM — один повтор со свежим интерфейсом (устройство сменилось)."""
        try:
            return fn(self._endpoint())
        except NoAudioDevice:
            raise
        except Exception as e:
            log.info("громкость: повтор после ошибки COM: %r", e)
            self._local.volume = None
            self._local.enumerator = None
            return fn(self._endpoint(force=True))

    # --- общая громкость

    def get_level(self) -> int:
        return self._call(lambda ev: scalar_to_level(ev.GetMasterVolumeLevelScalar()))

    def set_level(self, level: int, unmute: bool) -> int:
        def apply(ev: Any) -> int:
            ev.SetMasterVolumeLevelScalar(level_to_scalar(level), None)
            if unmute:
                ev.SetMute(0, None)
            return scalar_to_level(ev.GetMasterVolumeLevelScalar())

        return self._call(apply)

    def change_level(self, delta: int) -> int:
        def apply(ev: Any) -> int:
            current = scalar_to_level(ev.GetMasterVolumeLevelScalar())
            ev.SetMasterVolumeLevelScalar(level_to_scalar(current + delta), None)
            if delta > 0:
                ev.SetMute(0, None)
            return scalar_to_level(ev.GetMasterVolumeLevelScalar())

        return self._call(apply)

    def is_muted(self) -> bool:
        return self._call(lambda ev: bool(ev.GetMute()))

    def set_muted(self, muted: bool | None) -> bool:
        def apply(ev: Any) -> bool:
            value = (not bool(ev.GetMute())) if muted is None else bool(muted)
            ev.SetMute(int(value), None)
            return bool(ev.GetMute())

        return self._call(apply)

    # --- программы

    def sessions(self) -> list[tuple[int, Any]]:
        """(pid, IAudioSessionControl2) всех сессий на всех активных устройствах вывода."""
        from pycaw.api.audiopolicy import (  # type: ignore[import-not-found]
            IAudioSessionControl2,
            IAudioSessionManager2,
        )

        out: list[tuple[int, Any]] = []
        collection = self._enumerator().EnumAudioEndpoints(E_RENDER, DEVICE_STATE_ACTIVE)
        for i in range(int(collection.GetCount())):
            try:
                device = collection.Item(i)
                manager = device.Activate(IAudioSessionManager2._iid_, CLSCTX_ALL, None).QueryInterface(
                    IAudioSessionManager2
                )
                sessions = manager.GetSessionEnumerator()
                for j in range(int(sessions.GetCount())):
                    control = sessions.GetSession(j)
                    if not control:
                        continue
                    control2 = control.QueryInterface(IAudioSessionControl2)
                    out.append((int(control2.GetProcessId()), control2))
            except Exception as e:
                log.debug("сессии устройства %d не прочитаны: %r", i, e)
        return out

    def set_session_level(self, control2: Any, level: int) -> None:
        from pycaw.api.audioclient import ISimpleAudioVolume  # type: ignore[import-not-found]

        volume = control2.QueryInterface(ISimpleAudioVolume)
        volume.SetMasterVolume(level_to_scalar(level), None)
        if level > 0:
            volume.SetMute(0, None)
