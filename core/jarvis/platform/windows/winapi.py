"""Win32 через ctypes: структуры, константы и прототипы функций с явными argtypes/restype.

Структуры объявлены типами фиксированной ширины, а не ctypes.wintypes (на Linux DWORD там 8 байт), поэтому их
размеры совпадают с Windows x64 на любой ОС и проверяются тестами на Linux. Функции берутся из собственных
экземпляров WinDLL, а не из общего ctypes.windll: argtypes, которые ставят другие библиотеки (mss, pynput),
нашим не мешают. HANDLE/HWND — c_void_p (64 бита), LRESULT/LPARAM — c_ssize_t, HRESULT — c_int32 (проверяем
сами, без исключений ctypes). Загрузка — лениво, при первом вызове api() и только в Windows.
"""

import ctypes
import functools
import logging
import sys
import uuid
from types import SimpleNamespace
from typing import Any

log = logging.getLogger(__name__)

# --- типы фиксированной ширины ---------------------------------------------------------------------

BYTE = ctypes.c_uint8
WORD = ctypes.c_uint16
DWORD = ctypes.c_uint32
LONG = ctypes.c_int32
INT = ctypes.c_int32
UINT = ctypes.c_uint32
BOOL = ctypes.c_int32
BOOLEAN = ctypes.c_uint8
HRESULT = ctypes.c_int32
ULONG_PTR = ctypes.c_size_t
DWORD_PTR = ctypes.c_size_t
WPARAM = ctypes.c_size_t
LPARAM = ctypes.c_ssize_t
LRESULT = ctypes.c_ssize_t
LONG_PTR = ctypes.c_ssize_t
HANDLE = ctypes.c_void_p
HWND = ctypes.c_void_p
HDC = ctypes.c_void_p
HMONITOR = ctypes.c_void_p
HGDIOBJ = ctypes.c_void_p
HBITMAP = ctypes.c_void_p
LPVOID = ctypes.c_void_p
LPCWSTR = ctypes.c_wchar_p
LPWSTR = ctypes.c_wchar_p

# --- константы ---------------------------------------------------------------------------------------

GWL_STYLE = -16
GWL_EXSTYLE = -20
GW_OWNER = 4
WS_CAPTION = 0x00C00000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000
WS_EX_NOACTIVATE = 0x08000000

SW_SHOWNORMAL = 1
SW_MAXIMIZE = 3
SW_MINIMIZE = 6
SW_RESTORE = 9

WM_CLOSE = 0x0010
WM_COMMAND = 0x0111
WM_SYSCOMMAND = 0x0112
SC_MONITORPOWER = 0xF170
HWND_BROADCAST = 0xFFFF
TRAY_MINIMIZE_ALL = 419  # WM_COMMAND панели задач: «свернуть все окна»

DWMWA_EXTENDED_FRAME_BOUNDS = 9
DWMWA_CLOAKED = 14

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
ASFW_ANY = 0xFFFFFFFF
MONITOR_DEFAULTTONEAREST = 0x00000002
FLASHW_ALL = 0x3
FLASHW_TIMERNOFG = 0xC

SM_CXSCREEN = 0
SM_CYSCREEN = 1
SM_SWAPBUTTON = 23
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

PW_RENDERFULLCONTENT = 0x00000002
DIB_RGB_COLORS = 0
BI_RGB = 0

SHERB_NOCONFIRMATION = 0x00000001
SHERB_NOPROGRESSUI = 0x00000002
SHERB_NOSOUND = 0x00000004

QUNS_RUNNING_D3D_FULL_SCREEN = 3
QUNS_PRESENTATION_MODE = 4

# DPI_AWARENESS_CONTEXT — псевдо-дескрипторы (-4 — per-monitor v2, -3 — per-monitor)
DPI_PER_MONITOR_AWARE_V2 = -4
DPI_PER_MONITOR_AWARE = -3

S_OK = 0
S_FALSE = 1
E_UNEXPECTED = 0x8000FFFF

# --- структуры ---------------------------------------------------------------------------------------


class RECT(ctypes.Structure):
    _fields_ = [("left", LONG), ("top", LONG), ("right", LONG), ("bottom", LONG)]

    def as_tuple(self) -> tuple[int, int, int, int]:
        return (int(self.left), int(self.top), int(self.right), int(self.bottom))


class POINT(ctypes.Structure):
    _fields_ = [("x", LONG), ("y", LONG)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", DWORD), ("rcMonitor", RECT), ("rcWork", RECT), ("dwFlags", DWORD)]


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", UINT), ("dwTime", DWORD)]


class GUID(ctypes.Structure):
    _fields_ = [("Data1", DWORD), ("Data2", WORD), ("Data3", WORD), ("Data4", BYTE * 8)]

    @classmethod
    def parse(cls, text: str) -> "GUID":
        """«{374DE290-123F-4565-9164-39C4925E467B}» → GUID (порядок байт как в Windows)."""
        return cls.from_buffer_copy(uuid.UUID(text.strip().strip("{}")).bytes_le)


class SHQUERYRBINFO(ctypes.Structure):
    # shellapi.h упаковывает структуры по 1 байту только в 32-битной сборке
    _pack_ = 1 if ctypes.sizeof(ctypes.c_void_p) == 4 else 8
    _fields_ = [("cbSize", DWORD), ("i64Size", ctypes.c_int64), ("i64NumItems", ctypes.c_int64)]


class FLASHWINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", UINT),
        ("hwnd", HWND),
        ("dwFlags", DWORD),
        ("uCount", UINT),
        ("dwTimeout", DWORD),
    ]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", DWORD),
        ("biWidth", LONG),
        ("biHeight", LONG),
        ("biPlanes", WORD),
        ("biBitCount", WORD),
        ("biCompression", DWORD),
        ("biSizeImage", DWORD),
        ("biXPelsPerMeter", LONG),
        ("biYPelsPerMeter", LONG),
        ("biClrUsed", DWORD),
        ("biClrImportant", DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", DWORD * 3)]


def bitmap_info(width: int, height: int) -> BITMAPINFO:
    """32 бита на точку без сжатия; отрицательная высота — строки сверху вниз."""
    info = BITMAPINFO()
    header = info.bmiHeader
    header.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    header.biWidth = width
    header.biHeight = -height
    header.biPlanes = 1
    header.biBitCount = 32
    header.biCompression = BI_RGB
    return info


def hresult_ok(hr: int) -> bool:
    return int(hr) >= 0


def last_error() -> int:
    get = getattr(ctypes, "get_last_error", None)
    return int(get()) if get is not None else 0


# --- функции ---------------------------------------------------------------------------------------


def _dll(name: str, optional: bool = False) -> Any:
    try:
        return ctypes.WinDLL(name, use_last_error=True)  # type: ignore[attr-defined]
    except OSError:
        if optional:
            log.info("библиотека %s недоступна", name)
            return None
        raise


def _fn(dll: Any, name: str, argtypes: list[Any], restype: Any, optional: bool = False) -> Any:
    """Функция DLL с прототипом; нет функции (старая Windows) и optional — None."""
    if dll is None:
        if optional:
            return None
        raise OSError(f"{name}: библиотека не загружена")
    try:
        fn = getattr(dll, name)
    except AttributeError:
        if optional:
            return None
        raise
    fn.argtypes = argtypes
    fn.restype = restype
    return fn


@functools.cache
def api() -> Any:
    """Таблица функций Win32. Не Windows — OSError (вызывающий превращает её в PlatformError)."""
    if sys.platform != "win32":
        raise OSError("Win32 API есть только в Windows")
    u = _dll("user32")
    k = _dll("kernel32")
    g = _dll("gdi32")
    s = _dll("shell32")
    o = _dll("ole32")
    p = _dll("powrprof", optional=True)
    d = _dll("dwmapi", optional=True)
    sc = _dll("shcore", optional=True)

    ns = SimpleNamespace()
    ns.WNDENUMPROC = ctypes.WINFUNCTYPE(BOOL, HWND, LPARAM)  # type: ignore[attr-defined]
    pdword = ctypes.POINTER(DWORD)

    # окна
    ns.EnumWindows = _fn(u, "EnumWindows", [ns.WNDENUMPROC, LPARAM], BOOL)
    ns.EnumChildWindows = _fn(u, "EnumChildWindows", [HWND, ns.WNDENUMPROC, LPARAM], BOOL)
    ns.GetWindowTextLengthW = _fn(u, "GetWindowTextLengthW", [HWND], INT)
    ns.GetWindowTextW = _fn(u, "GetWindowTextW", [HWND, LPWSTR, INT], INT)
    ns.GetClassNameW = _fn(u, "GetClassNameW", [HWND, LPWSTR, INT], INT)
    ns.IsWindow = _fn(u, "IsWindow", [HWND], BOOL)
    ns.IsWindowVisible = _fn(u, "IsWindowVisible", [HWND], BOOL)
    ns.IsIconic = _fn(u, "IsIconic", [HWND], BOOL)
    ns.IsZoomed = _fn(u, "IsZoomed", [HWND], BOOL)
    ns.IsHungAppWindow = _fn(u, "IsHungAppWindow", [HWND], BOOL)
    get_long = "GetWindowLongPtrW" if hasattr(u, "GetWindowLongPtrW") else "GetWindowLongW"
    ns.GetWindowLongPtrW = _fn(u, get_long, [HWND, INT], LONG_PTR)
    ns.GetWindow = _fn(u, "GetWindow", [HWND, UINT], HWND)
    ns.GetWindowThreadProcessId = _fn(u, "GetWindowThreadProcessId", [HWND, pdword], DWORD)
    ns.GetForegroundWindow = _fn(u, "GetForegroundWindow", [], HWND)
    ns.SetForegroundWindow = _fn(u, "SetForegroundWindow", [HWND], BOOL)
    ns.BringWindowToTop = _fn(u, "BringWindowToTop", [HWND], BOOL)
    ns.ShowWindow = _fn(u, "ShowWindow", [HWND, INT], BOOL)
    ns.ShowWindowAsync = _fn(u, "ShowWindowAsync", [HWND, INT], BOOL)
    ns.PostMessageW = _fn(u, "PostMessageW", [HWND, UINT, WPARAM, LPARAM], BOOL)
    ns.AttachThreadInput = _fn(u, "AttachThreadInput", [DWORD, DWORD, BOOL], BOOL)
    ns.AllowSetForegroundWindow = _fn(u, "AllowSetForegroundWindow", [DWORD], BOOL)
    ns.SwitchToThisWindow = _fn(u, "SwitchToThisWindow", [HWND, BOOL], None, optional=True)
    ns.FlashWindowEx = _fn(u, "FlashWindowEx", [ctypes.POINTER(FLASHWINFO)], BOOL)
    ns.GetWindowRect = _fn(u, "GetWindowRect", [HWND, ctypes.POINTER(RECT)], BOOL)
    ns.MonitorFromWindow = _fn(u, "MonitorFromWindow", [HWND, DWORD], HMONITOR)
    ns.GetMonitorInfoW = _fn(u, "GetMonitorInfoW", [HMONITOR, ctypes.POINTER(MONITORINFO)], BOOL)
    ns.GetShellWindow = _fn(u, "GetShellWindow", [], HWND)
    ns.GetDesktopWindow = _fn(u, "GetDesktopWindow", [], HWND)
    ns.FindWindowW = _fn(u, "FindWindowW", [LPCWSTR, LPCWSTR], HWND)
    ns.LockWorkStation = _fn(u, "LockWorkStation", [], BOOL)
    ns.GetLastInputInfo = _fn(u, "GetLastInputInfo", [ctypes.POINTER(LASTINPUTINFO)], BOOL)
    ns.GetClipboardSequenceNumber = _fn(u, "GetClipboardSequenceNumber", [], DWORD)

    # ввод и экран (SendInput объявлен в inputs: там структура INPUT)
    ns.MapVirtualKeyW = _fn(u, "MapVirtualKeyW", [UINT, UINT], UINT)
    ns.GetAsyncKeyState = _fn(u, "GetAsyncKeyState", [INT], ctypes.c_int16)
    ns.GetSystemMetrics = _fn(u, "GetSystemMetrics", [INT], INT)
    ns.GetCursorPos = _fn(u, "GetCursorPos", [ctypes.POINTER(POINT)], BOOL)
    ns.SetCursorPos = _fn(u, "SetCursorPos", [INT, INT], BOOL)

    # DPI (Windows 10 1607+/1703+; на старых — None)
    ns.SetProcessDpiAwarenessContext = _fn(u, "SetProcessDpiAwarenessContext", [HANDLE], BOOL, True)
    ns.SetThreadDpiAwarenessContext = _fn(u, "SetThreadDpiAwarenessContext", [HANDLE], HANDLE, True)
    ns.GetWindowDpiAwarenessContext = _fn(u, "GetWindowDpiAwarenessContext", [HWND], HANDLE, True)
    ns.SetProcessDPIAware = _fn(u, "SetProcessDPIAware", [], BOOL, True)
    ns.SetProcessDpiAwareness = _fn(sc, "SetProcessDpiAwareness", [INT], HRESULT, True)

    # снимки окон
    ns.GetDC = _fn(u, "GetDC", [HWND], HDC)
    ns.ReleaseDC = _fn(u, "ReleaseDC", [HWND, HDC], INT)
    ns.PrintWindow = _fn(u, "PrintWindow", [HWND, HDC, UINT], BOOL)
    ns.CreateCompatibleDC = _fn(g, "CreateCompatibleDC", [HDC], HDC)
    ns.DeleteDC = _fn(g, "DeleteDC", [HDC], BOOL)
    ns.CreateDIBSection = _fn(
        g,
        "CreateDIBSection",
        [HDC, ctypes.POINTER(BITMAPINFO), UINT, ctypes.POINTER(LPVOID), HANDLE, DWORD],
        HBITMAP,
    )
    ns.SelectObject = _fn(g, "SelectObject", [HDC, HGDIOBJ], HGDIOBJ)
    ns.DeleteObject = _fn(g, "DeleteObject", [HGDIOBJ], BOOL)
    ns.GdiFlush = _fn(g, "GdiFlush", [], BOOL)

    # процессы и время
    ns.GetCurrentThreadId = _fn(k, "GetCurrentThreadId", [], DWORD)
    ns.OpenProcess = _fn(k, "OpenProcess", [DWORD, BOOL, DWORD], HANDLE)
    ns.CloseHandle = _fn(k, "CloseHandle", [HANDLE], BOOL)
    ns.QueryFullProcessImageNameW = _fn(
        k, "QueryFullProcessImageNameW", [HANDLE, DWORD, LPWSTR, pdword], BOOL
    )
    ns.GetTickCount64 = _fn(k, "GetTickCount64", [], ctypes.c_uint64)

    # оболочка
    ns.ShellExecuteW = _fn(s, "ShellExecuteW", [HWND, LPCWSTR, LPCWSTR, LPCWSTR, LPCWSTR, INT], HANDLE)
    ns.SHQueryRecycleBinW = _fn(s, "SHQueryRecycleBinW", [LPCWSTR, ctypes.POINTER(SHQUERYRBINFO)], HRESULT)
    ns.SHEmptyRecycleBinW = _fn(s, "SHEmptyRecycleBinW", [HWND, LPCWSTR, DWORD], HRESULT)
    ns.SHGetKnownFolderPath = _fn(
        s, "SHGetKnownFolderPath", [ctypes.POINTER(GUID), DWORD, HANDLE, ctypes.POINTER(LPVOID)], HRESULT
    )
    ns.SHQueryUserNotificationState = _fn(
        s, "SHQueryUserNotificationState", [ctypes.POINTER(INT)], HRESULT, True
    )
    ns.CoTaskMemFree = _fn(o, "CoTaskMemFree", [LPVOID], None)

    # питание
    ns.SetSuspendState = _fn(p, "SetSuspendState", [BOOLEAN, BOOLEAN, BOOLEAN], BOOLEAN, True)
    ns.IsPwrHibernateAllowed = _fn(p, "IsPwrHibernateAllowed", [], BOOLEAN, True)
    ns.IsPwrSuspendAllowed = _fn(p, "IsPwrSuspendAllowed", [], BOOLEAN, True)

    # DWM
    ns.DwmGetWindowAttribute = _fn(d, "DwmGetWindowAttribute", [HWND, DWORD, LPVOID, DWORD], HRESULT, True)
    return ns


def known_folder_path(folder_id: str) -> str | None:
    """SHGetKnownFolderPath по строке GUID; нет папки или ошибка — None."""
    w = api()
    guid = GUID.parse(folder_id)
    out = LPVOID()
    hr = w.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(out))
    try:
        if not hresult_ok(hr) or not out.value:
            log.debug("SHGetKnownFolderPath(%s): 0x%08X", folder_id, hr & 0xFFFFFFFF)
            return None
        return ctypes.wstring_at(out.value)
    finally:
        if out.value:
            w.CoTaskMemFree(out)


def set_process_dpi_aware() -> None:
    """Процесс — per-monitor DPI aware: координаты окон, мыши и снимков — в физических точках.

    Уже задано (манифест, mss, вызов раньше) — Windows вернёт ошибку доступа; это не страшно.
    """
    try:
        w = api()
    except OSError:
        return
    for context in (DPI_PER_MONITOR_AWARE_V2, DPI_PER_MONITOR_AWARE):
        if w.SetProcessDpiAwarenessContext is not None and w.SetProcessDpiAwarenessContext(
            ctypes.c_void_p(context)
        ):
            return
    if w.SetProcessDpiAwareness is not None and hresult_ok(w.SetProcessDpiAwareness(2)):
        return
    if w.SetProcessDPIAware is not None:
        w.SetProcessDPIAware()
