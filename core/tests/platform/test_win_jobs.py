"""Job Object ядра: браузер агента закрывается вместе с ядром. Порядок вызовов kernel32 — на поддельном API
(на Linux его нет); размеры структур — как у Windows x64."""

import ctypes
import subprocess
import sys
from typing import Any

import pytest

from jarvis.platform.windows import jobs

JOB, SNAPSHOT, PROCESS = 101, 202, 303


class FakeKernel:
    """kernel32 по сценарию: потоки процессов (pid → id потоков) и что не удаётся."""

    def __init__(self, threads: dict[int, list[int]], *, assign: bool = True, resume: bool = True) -> None:
        self.threads = threads
        self.assign_ok = assign
        self.resume_ok = resume
        self.calls: list[tuple[Any, ...]] = []
        self.limit_flags = 0
        self.info_size = 0
        self._rows: list[tuple[int, int]] = []

    def last_error(self) -> int:
        return 5

    def CreateJobObjectW(self, attrs: Any, name: Any) -> int:
        self.calls.append(("CreateJobObjectW",))
        return JOB

    def SetInformationJobObject(self, job: int, kind: int, info: Any, size: int) -> bool:
        self.calls.append(("SetInformationJobObject", job, kind))
        self.limit_flags = info._obj.BasicLimitInformation.LimitFlags
        self.info_size = size
        return True

    def AssignProcessToJobObject(self, job: int, process: Any) -> bool:
        self.calls.append(("AssignProcessToJobObject", job, process.value))
        return self.assign_ok

    def CreateToolhelp32Snapshot(self, flags: int, pid: int) -> int:
        self.calls.append(("CreateToolhelp32Snapshot", flags))
        self._rows = [(owner, tid) for owner, ids in self.threads.items() for tid in ids]
        return SNAPSHOT

    def _row(self, entry: Any) -> bool:
        if not self._rows:
            return False
        owner, tid = self._rows.pop(0)
        entry._obj.th32OwnerProcessID, entry._obj.th32ThreadID = owner, tid
        return True

    def Thread32First(self, snapshot: int, entry: Any) -> bool:
        assert entry._obj.dwSize == ctypes.sizeof(jobs.THREADENTRY32)
        return self._row(entry)

    def Thread32Next(self, snapshot: int, entry: Any) -> bool:
        return self._row(entry)

    def OpenThread(self, access: int, inherit: bool, tid: int) -> int:
        self.calls.append(("OpenThread", access, tid))
        return 1000 + tid

    def ResumeThread(self, thread: int) -> int:
        self.calls.append(("ResumeThread", thread))
        return 1 if self.resume_ok else jobs.RESUME_FAILED

    def CloseHandle(self, handle: int) -> bool:
        self.calls.append(("CloseHandle", handle))
        return True


class FakeProc:
    def __init__(self, args: list[str], **kwargs: Any) -> None:
        self.args, self.kwargs = args, kwargs
        self.pid = 4242
        self._handle = PROCESS
        self.killed = False

    def kill(self) -> None:
        self.killed = True


def test_structures_have_windows_x64_sizes() -> None:
    assert ctypes.sizeof(jobs.JOBOBJECT_BASIC_LIMIT_INFORMATION) == 64
    assert ctypes.sizeof(jobs.IO_COUNTERS) == 48
    assert ctypes.sizeof(jobs.JOBOBJECT_EXTENDED_LIMIT_INFORMATION) == 144
    assert ctypes.sizeof(jobs.THREADENTRY32) == 28


def test_process_starts_suspended_joins_the_job_then_resumes() -> None:
    kernel = FakeKernel({4242: [7], 999: [8]})  # чужой процесс 999 не трогаем
    core = jobs.CoreJob(kernel)
    proc = core.spawn(["chrome.exe", "--headless"], popen=FakeProc, creationflags=0x08000000, stdin=-3)
    # создан приостановленным, остальные флаги и параметры — как просили
    assert proc.kwargs == {"creationflags": 0x08000000 | jobs.CREATE_SUSPENDED, "stdin": -3}
    names = [c[0] for c in kernel.calls]
    # сначала Job Object (один, «завершить всё при закрытии»), процесс — в него, и только потом — продолжить
    assert names.index("AssignProcessToJobObject") < names.index("ResumeThread")
    assert ("SetInformationJobObject", JOB, jobs.JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS) in kernel.calls
    assert kernel.limit_flags == jobs.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE and kernel.info_size == 144
    assert ("AssignProcessToJobObject", JOB, PROCESS) in kernel.calls
    assert [c for c in kernel.calls if c[0] == "OpenThread"] == [
        ("OpenThread", jobs.THREAD_SUSPEND_RESUME, 7)
    ]
    assert ("CloseHandle", 1007) in kernel.calls and ("CloseHandle", SNAPSHOT) in kernel.calls
    assert ("CloseHandle", JOB) not in kernel.calls  # закрыть — значит завершить процессы в нём
    assert not proc.killed
    # второй процесс — в тот же Job Object
    core.spawn(["chrome.exe"], popen=FakeProc)
    assert [c[0] for c in kernel.calls].count("CreateJobObjectW") == 1


def test_not_joined_still_runs_and_not_resumed_is_killed(caplog: pytest.LogCaptureFixture) -> None:
    kernel = FakeKernel({4242: [7]}, assign=False)
    proc = jobs.CoreJob(kernel).spawn(["chrome.exe"], popen=FakeProc)
    assert not proc.killed and ("ResumeThread", 1007) in kernel.calls  # работает, как раньше
    assert "не помещён в Job Object" in caplog.text

    stuck = FakeKernel({4242: [7]}, resume=False)
    procs: list[FakeProc] = []

    def popen(args: list[str], **kwargs: Any) -> FakeProc:
        procs.append(FakeProc(args, **kwargs))
        return procs[-1]

    with pytest.raises(OSError, match="не удалось продолжить"):
        jobs.CoreJob(stuck).spawn(["chrome.exe"], popen=popen)
    assert procs[0].killed  # приостановленным не оставляем


def test_kernel32_only_on_windows() -> None:
    if sys.platform == "win32":
        assert callable(jobs.kernel().AssignProcessToJobObject)
    else:
        with pytest.raises(OSError):
            jobs.kernel.__wrapped__()


@pytest.mark.skipif(sys.platform != "win32", reason="Job Object есть только в Windows")
def test_real_process_runs_inside_the_core_job() -> None:
    """Windows (CI, ПК): процесс продолжен (печатает) и сидит в Job Object ядра."""
    core = jobs.CoreJob()
    code = "import sys, time; print('ok', flush=True); time.sleep(30)"
    proc = core.spawn([sys.executable, "-c", code], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE)
    try:
        assert proc.stdout is not None and proc.stdout.readline().strip() == b"ok"
        kernel32 = ctypes.WinDLL("kernel32")  # type: ignore[attr-defined]
        kernel32.IsProcessInJob.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int32)]
        inside = ctypes.c_int32()
        handle = ctypes.c_void_p(int(proc._handle))  # type: ignore[attr-defined]
        assert kernel32.IsProcessInJob(handle, ctypes.c_void_p(core.handle()), ctypes.byref(inside))
        assert inside.value
    finally:
        proc.kill()
        proc.wait(10)
