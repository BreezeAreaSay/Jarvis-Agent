"""Процессы, которые не переживают ядро: Job Object с JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE.

Ядро упало или его убили — Windows сама закрывает его описатели, в том числе описатель Job Object, и завершает
все процессы в нём. Так браузер агента (свой Chrome без окна) не остаётся висеть после ядра.

Процесс запускается приостановленным (CREATE_SUSPENDED), помещается в Job Object и только потом продолжает
работу: его дочерние процессы (у Chrome — GPU, страницы, сеть) рождаются уже внутри и тоже завершатся с ядром.
Поместить не вышло (старая Windows, запрет родительского Job Object) — процесс всё равно продолжает работу,
как раньше, и это пишется в журнал; продолжить не вышло — процесс завершается, а вызывающий получает ошибку.

Job Object один на ядро и не закрывается, пока ядро живо (закрыть описатель — значит завершить процессы).
Структуры — типами фиксированной ширины (их размеры проверяются тестами на Linux), функции kernel32 —
лениво, только в Windows; api подставляют тесты.
"""

import ctypes
import functools
import logging
import subprocess
import sys
import threading
from types import SimpleNamespace
from typing import Any

from jarvis.platform.windows.winapi import BOOL, DWORD, HANDLE, INT, LONG, LPCWSTR, LPVOID, ULONG_PTR

log = logging.getLogger(__name__)

CREATE_SUSPENDED = 0x00000004
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9  # JobObjectExtendedLimitInformation
TH32CS_SNAPTHREAD = 0x00000004
THREAD_SUSPEND_RESUME = 0x0002
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
RESUME_FAILED = 0xFFFFFFFF  # ResumeThread вернул (DWORD)-1

LARGE_INTEGER = ctypes.c_int64
SIZE_T = ctypes.c_size_t
ULONGLONG = ctypes.c_uint64


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", LARGE_INTEGER),
        ("PerJobUserTimeLimit", LARGE_INTEGER),
        ("LimitFlags", DWORD),
        ("MinimumWorkingSetSize", SIZE_T),
        ("MaximumWorkingSetSize", SIZE_T),
        ("ActiveProcessLimit", DWORD),
        ("Affinity", ULONG_PTR),
        ("PriorityClass", DWORD),
        ("SchedulingClass", DWORD),
    ]


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ULONGLONG),
        ("WriteOperationCount", ULONGLONG),
        ("OtherOperationCount", ULONGLONG),
        ("ReadTransferCount", ULONGLONG),
        ("WriteTransferCount", ULONGLONG),
        ("OtherTransferCount", ULONGLONG),
    ]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", SIZE_T),
        ("JobMemoryLimit", SIZE_T),
        ("PeakProcessMemoryUsed", SIZE_T),
        ("PeakJobMemoryUsed", SIZE_T),
    ]


class THREADENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", DWORD),
        ("cntUsage", DWORD),
        ("th32ThreadID", DWORD),
        ("th32OwnerProcessID", DWORD),
        ("tpBasePri", LONG),
        ("tpDeltaPri", LONG),
        ("dwFlags", DWORD),
    ]


@functools.cache
def kernel() -> Any:
    """Функции kernel32 для Job Object и потоков; не Windows — OSError."""
    if sys.platform != "win32":
        raise OSError("Job Object есть только в Windows")
    k = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]

    def fn(name: str, argtypes: list[Any], restype: Any) -> Any:
        f = getattr(k, name)
        f.argtypes, f.restype = argtypes, restype
        return f

    entry = ctypes.POINTER(THREADENTRY32)
    return SimpleNamespace(
        CreateJobObjectW=fn("CreateJobObjectW", [LPVOID, LPCWSTR], HANDLE),
        SetInformationJobObject=fn("SetInformationJobObject", [HANDLE, INT, LPVOID, DWORD], BOOL),
        AssignProcessToJobObject=fn("AssignProcessToJobObject", [HANDLE, HANDLE], BOOL),
        CreateToolhelp32Snapshot=fn("CreateToolhelp32Snapshot", [DWORD, DWORD], HANDLE),
        Thread32First=fn("Thread32First", [HANDLE, entry], BOOL),
        Thread32Next=fn("Thread32Next", [HANDLE, entry], BOOL),
        OpenThread=fn("OpenThread", [DWORD, BOOL, DWORD], HANDLE),
        ResumeThread=fn("ResumeThread", [HANDLE], DWORD),
        CloseHandle=fn("CloseHandle", [HANDLE], BOOL),
        last_error=ctypes.get_last_error,  # type: ignore[attr-defined]
    )


class CoreJob:
    """Job Object ядра: процессы в нём завершаются вместе с ядром."""

    def __init__(self, api: Any = None) -> None:
        self._api = api
        self._job: Any = None
        self._lock = threading.Lock()

    def _k(self) -> Any:
        return self._api if self._api is not None else kernel()

    def handle(self) -> Any:
        """Описатель Job Object (создаётся один раз); не вышло — OSError."""
        with self._lock:
            if self._job is not None:
                return self._job
            k = self._k()
            job = k.CreateJobObjectW(None, None)
            if not job:
                raise OSError(f"CreateJobObjectW: ошибка {k.last_error()}")
            info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            ok = k.SetInformationJobObject(
                job,
                JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
                ctypes.byref(info),
                ctypes.sizeof(info),
            )
            if not ok:
                error = k.last_error()
                k.CloseHandle(job)
                raise OSError(f"SetInformationJobObject: ошибка {error}")
            self._job = job  # не закрываем: закрыть — значит завершить процессы в нём
            return job

    def add(self, process_handle: int) -> bool:
        """Поместить процесс в Job Object; не вышло — False (и запись в журнал)."""
        try:
            job = self.handle()
            k = self._k()
            if k.AssignProcessToJobObject(job, ctypes.c_void_p(int(process_handle))):
                return True
            log.warning("процесс не помещён в Job Object ядра: ошибка %s", k.last_error())
        except OSError as e:
            log.warning("Job Object ядра недоступен: %s", e)
        return False

    def resume(self, pid: int) -> int:
        """Продолжить приостановленный процесс: все его потоки (у только что созданного — один). Сколько
        потоков продолжено."""
        k = self._k()
        snapshot = k.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
        if not snapshot or snapshot == INVALID_HANDLE_VALUE:
            raise OSError(f"CreateToolhelp32Snapshot: ошибка {k.last_error()}")
        resumed = 0
        try:
            entry = THREADENTRY32()
            entry.dwSize = ctypes.sizeof(THREADENTRY32)
            more = k.Thread32First(snapshot, ctypes.byref(entry))
            while more:
                if entry.th32OwnerProcessID == pid:
                    thread = k.OpenThread(THREAD_SUSPEND_RESUME, False, entry.th32ThreadID)
                    if thread:
                        try:
                            if k.ResumeThread(thread) != RESUME_FAILED:
                                resumed += 1
                        finally:
                            k.CloseHandle(thread)
                more = k.Thread32Next(snapshot, ctypes.byref(entry))
        finally:
            k.CloseHandle(snapshot)
        return resumed

    def spawn(self, args: list[str], *, popen: Any = subprocess.Popen, **kwargs: Any) -> Any:
        """subprocess.Popen, но процесс — в Job Object ядра с самого начала (см. описание модуля)."""
        flags = int(kwargs.pop("creationflags", 0) or 0)
        proc = popen(args, creationflags=flags | CREATE_SUSPENDED, **kwargs)
        handle = getattr(proc, "_handle", None)
        joined = handle is not None and self.add(int(handle))
        try:
            resumed = self.resume(int(proc.pid))
        except OSError as e:
            log.warning("процесс %s не продолжен: %s", proc.pid, e)
            resumed = 0
        if not resumed:
            proc.kill()
            raise OSError(f"процесс {args[0]} не запустился: не удалось продолжить его после создания")
        if joined:
            log.info("процесс %s (%s) закроется вместе с ядром", proc.pid, args[0])
        return proc


_core_job = CoreJob()


def spawn_with_core(args: list[str], **kwargs: Any) -> Any:
    """Процесс, который Windows завершит вместе с ядром (браузер агента)."""
    return _core_job.spawn(args, **kwargs)
