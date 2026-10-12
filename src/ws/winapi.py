"""Окна и мониторы: единственный модуль, где вызывается win32.

Ни один вызов не должен зависнуть из-за чужого неотвечающего окна, поэтому
используются только асинхронные варианты (ShowWindowAsync, SWP_ASYNCWINDOWPOS,
PostMessage). При отказе доступа методы возвращают False, а не бросают исключение.
"""

from __future__ import annotations

import contextlib
import ctypes
import logging
import os
import threading
import time
from ctypes import wintypes
from pathlib import Path
from typing import Callable, Iterator, Protocol, Sequence

import pythoncom
import pywintypes
import win32api
import win32com.client
import win32con
import win32event
import win32gui
import win32process
import win32security
import winerror

from ws import layout
from ws.errors import SystemFailure
from ws.hotkeys import Hotkey
from ws.models import MonitorInfo, Rect, WindowInfo

log = logging.getLogger(__name__)


class WindowSystem(Protocol):
    def enable_dpi_awareness(self) -> bool: ...
    def monitors(self) -> list[MonitorInfo]: ...
    def list_windows(self) -> list[WindowInfo]: ...  # все top-level, включая скрытые
    def get_window(self, hwnd: int) -> WindowInfo | None: ...
    def move(self, hwnd: int, rect: Rect) -> bool: ...
    def restore(self, hwnd: int) -> bool: ...  # снять минимизацию и максимизацию
    def maximize(self, hwnd: int) -> bool: ...
    def hide(self, hwnd: int) -> bool: ...
    def minimize(self, hwnd: int) -> bool: ...
    def show(self, hwnd: int) -> bool: ...
    def focus(self, hwnd: int) -> bool: ...
    def request_close(self, hwnd: int) -> bool: ...  # WM_CLOSE через PostMessage
    def is_hung(self, hwnd: int) -> bool: ...
    def is_elevated(self, pid: int) -> bool | None: ...  # None = не удалось узнать
    def explorer_folders(self) -> dict[int, Path]: ...  # hwnd окна Проводника -> открытая папка


_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_dwmapi = ctypes.WinDLL("dwmapi")
_shell32 = ctypes.WinDLL("shell32")

_user32.SetProcessDpiAwarenessContext.argtypes = [wintypes.HANDLE]
_user32.SetProcessDpiAwarenessContext.restype = wintypes.BOOL
_user32.GetAwarenessFromDpiAwarenessContext.argtypes = [wintypes.HANDLE]
_user32.GetAwarenessFromDpiAwarenessContext.restype = ctypes.c_int
_user32.GetThreadDpiAwarenessContext.restype = wintypes.HANDLE
_user32.IsHungAppWindow.argtypes = [wintypes.HWND]
_user32.IsHungAppWindow.restype = wintypes.BOOL
_user32.IsZoomed.argtypes = [wintypes.HWND]
_user32.IsZoomed.restype = wintypes.BOOL
_user32.ShowWindowAsync.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.ShowWindowAsync.restype = wintypes.BOOL
_dwmapi.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
_dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.LPWSTR,
    ctypes.POINTER(wintypes.DWORD),
]
_kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL

_DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
_DPI_AWARENESS_PER_MONITOR = 2
_DWMWA_EXTENDED_FRAME_BOUNDS = 9
_DWMWA_CLOAKED = 14
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_MDT_EFFECTIVE_DPI = 0
_SWP_ASYNCWINDOWPOS = 0x4000


def _wait(pred: Callable[[], bool], timeout: float = 1.0, step: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(step)
    return pred()


def _monitor_dpi(hmonitor: int) -> int:
    try:
        shcore = ctypes.WinDLL("shcore")
        shcore.GetDpiForMonitor.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_uint),
            ctypes.POINTER(ctypes.c_uint),
        ]
        dpi_x, dpi_y = ctypes.c_uint(), ctypes.c_uint()
        if shcore.GetDpiForMonitor(hmonitor, _MDT_EFFECTIVE_DPI, ctypes.byref(dpi_x), ctypes.byref(dpi_y)) == 0:
            return int(dpi_x.value)
    except OSError:
        pass
    return 96


def _exe_name(pid: int) -> str:
    handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(1024)
        buffer = ctypes.create_unicode_buffer(size.value)
        if _kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return os.path.basename(buffer.value)
        return ""
    finally:
        _kernel32.CloseHandle(handle)


def _frame_rect(hwnd: int, fallback: Rect) -> Rect:
    rect = wintypes.RECT()
    hr = _dwmapi.DwmGetWindowAttribute(hwnd, _DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(rect), ctypes.sizeof(rect))
    if hr != 0:
        return fallback
    return Rect(rect.left, rect.top, rect.right, rect.bottom)


def _cloaked(hwnd: int) -> bool:
    value = wintypes.DWORD(0)
    hr = _dwmapi.DwmGetWindowAttribute(hwnd, _DWMWA_CLOAKED, ctypes.byref(value), ctypes.sizeof(value))
    return hr == 0 and value.value != 0


class Win32WindowSystem:
    def __init__(self) -> None:
        self._self_elevated = bool(_shell32.IsUserAnAdmin())

    def enable_dpi_awareness(self) -> bool:
        """Per-monitor DPI awareness. Вызывать до любых операций с окнами."""
        _user32.SetProcessDpiAwarenessContext(_DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
        # Вызов мог не сработать, если awareness уже задан; важно итоговое состояние.
        context = _user32.GetThreadDpiAwarenessContext()
        return _user32.GetAwarenessFromDpiAwarenessContext(context) == _DPI_AWARENESS_PER_MONITOR

    def monitors(self) -> list[MonitorInfo]:
        found = []
        for hmonitor, _hdc, _rect in win32api.EnumDisplayMonitors(None, None):
            info = win32api.GetMonitorInfo(hmonitor)
            found.append(
                (
                    bool(info["Flags"] & win32con.MONITORINFOF_PRIMARY),
                    Rect(*info["Monitor"]),
                    Rect(*info["Work"]),
                    _monitor_dpi(int(hmonitor)),
                )
            )
        return layout.order_monitors(found)

    def list_windows(self) -> list[WindowInfo]:
        hwnds: list[int] = []
        win32gui.EnumWindows(lambda hwnd, _: hwnds.append(hwnd) or True, None)
        exe_cache: dict[int, str] = {}
        result = []
        for hwnd in hwnds:
            info = self._describe(hwnd, exe_cache)
            if info is not None:
                result.append(info)
        return result

    def get_window(self, hwnd: int) -> WindowInfo | None:
        if not win32gui.IsWindow(hwnd):
            return None
        return self._describe(hwnd, {})

    def _describe(self, hwnd: int, exe_cache: dict[int, str]) -> WindowInfo | None:
        try:
            _tid, pid = win32process.GetWindowThreadProcessId(hwnd)
            if pid not in exe_cache:
                exe_cache[pid] = _exe_name(pid)
            rect = Rect(*win32gui.GetWindowRect(hwnd))
            return WindowInfo(
                hwnd=hwnd,
                pid=pid,
                exe=exe_cache[pid],
                title=win32gui.GetWindowText(hwnd),
                class_name=win32gui.GetClassName(hwnd),
                visible=bool(win32gui.IsWindowVisible(hwnd)),
                minimized=bool(win32gui.IsIconic(hwnd)),
                maximized=bool(_user32.IsZoomed(hwnd)),
                cloaked=_cloaked(hwnd),
                tool_window=bool(win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE) & win32con.WS_EX_TOOLWINDOW),
                owner=win32gui.GetWindow(hwnd, win32con.GW_OWNER) or 0,
                rect=rect,
                frame_rect=_frame_rect(hwnd, rect),
            )
        except pywintypes.error:
            return None  # окно исчезло, пока мы его описывали

    def move(self, hwnd: int, rect: Rect) -> bool:
        flags = win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE | _SWP_ASYNCWINDOWPOS
        try:
            win32gui.SetWindowPos(hwnd, 0, rect.left, rect.top, rect.width, rect.height, flags)
            return True
        except pywintypes.error:
            return False

    def _show_async(self, hwnd: int, command: int) -> bool:
        if not win32gui.IsWindow(hwnd):
            return False
        return bool(_user32.ShowWindowAsync(hwnd, command))

    def restore(self, hwnd: int) -> bool:
        if not self._show_async(hwnd, win32con.SW_RESTORE):
            return False
        return _wait(lambda: not win32gui.IsIconic(hwnd) and not _user32.IsZoomed(hwnd))

    def maximize(self, hwnd: int) -> bool:
        return self._show_async(hwnd, win32con.SW_MAXIMIZE)

    def hide(self, hwnd: int) -> bool:
        if not self._show_async(hwnd, win32con.SW_HIDE):
            return False
        return _wait(lambda: not win32gui.IsWindowVisible(hwnd))

    def minimize(self, hwnd: int) -> bool:
        return self._show_async(hwnd, win32con.SW_SHOWMINNOACTIVE)

    def show(self, hwnd: int) -> bool:
        if not win32gui.IsWindow(hwnd):
            return False
        if win32gui.IsIconic(hwnd):
            return self._show_async(hwnd, win32con.SW_RESTORE)
        if win32gui.IsWindowVisible(hwnd):
            return True
        return self._show_async(hwnd, win32con.SW_SHOWNA)

    def focus(self, hwnd: int) -> bool:
        try:
            win32gui.SetForegroundWindow(hwnd)
            return True
        except pywintypes.error:
            pass
        try:
            win32gui.BringWindowToTop(hwnd)
        except pywintypes.error:
            pass
        return False

    def request_close(self, hwnd: int) -> bool:
        try:
            win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
            return True
        except pywintypes.error:
            return False

    def is_hung(self, hwnd: int) -> bool:
        return bool(_user32.IsHungAppWindow(hwnd))

    def is_elevated(self, pid: int) -> bool | None:
        """True, если процесс запущен с правами выше наших и управлять им нельзя."""
        if self._self_elevated:
            return False
        try:
            process = win32api.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        except pywintypes.error:
            return None
        try:
            token = win32security.OpenProcessToken(process, win32con.TOKEN_QUERY)
            try:
                return bool(win32security.GetTokenInformation(token, win32security.TokenElevation))
            finally:
                token.Close()
        except pywintypes.error:
            return None
        finally:
            process.Close()

    def explorer_folders(self) -> dict[int, Path]:
        """Папки открытых окон Проводника через Shell.Application (виртуальные папки пропускаются)."""
        pythoncom.CoInitialize()
        try:
            shell = win32com.client.Dispatch("Shell.Application")
            found: dict[int, Path] = {}
            for window in shell.Windows():
                try:
                    hwnd = int(window.HWND)
                    folder = window.Document.Folder.Self.Path
                except Exception:  # COM бросает разные ошибки для окон не-Проводника
                    continue
                if isinstance(folder, str) and os.path.isdir(folder):
                    found.setdefault(hwnd, Path(folder))
            return found
        except Exception:
            log.exception("не удалось получить список окон Проводника")
            return {}
        finally:
            pythoncom.CoUninitialize()

    @property
    def self_elevated(self) -> bool:
        return self._self_elevated


_WM_HOTKEY = 0x0312
_WM_APP = 0x8000
_WM_QUIT = 0x0012
_WM_USER = 0x0400
_PM_NOREMOVE = 0
_MOD_NOREPEAT = 0x4000  # удержание клавиши не повторяет срабатывание
_ERROR_HOTKEY_ALREADY_REGISTERED = 1409

_user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
_user32.RegisterHotKey.restype = wintypes.BOOL
_user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.UnregisterHotKey.restype = wintypes.BOOL
_user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
_user32.GetMessageW.restype = ctypes.c_int
_user32.PeekMessageW.argtypes = [
    ctypes.POINTER(wintypes.MSG),
    wintypes.HWND,
    wintypes.UINT,
    wintypes.UINT,
    wintypes.UINT,
]
_user32.PeekMessageW.restype = wintypes.BOOL
_user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
_user32.PostThreadMessageW.restype = wintypes.BOOL
_kernel32.GetCurrentThreadId.restype = wintypes.DWORD


class HotkeyService(Protocol):
    def start(self) -> None: ...
    def set_bindings(self, bindings: Sequence[tuple[int, Hotkey]]) -> None: ...  # (id, комбинация)
    def stop(self) -> None: ...


class Win32Hotkeys:
    """Глобальные горячие клавиши через RegisterHotKey.

    Регистрация и приём сообщений идут в одном служебном потоке (так требует Windows);
    on_hotkey вызывается из него и должен возвращаться быстро.
    """

    def __init__(self, on_hotkey: Callable[[int], None], on_error: Callable[[str], None]) -> None:
        self._on_hotkey = on_hotkey
        self._on_error = on_error
        self._lock = threading.Lock()
        self._wanted: list[tuple[int, Hotkey]] = []
        self._registered: list[int] = []
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._tid = 0

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="ws-hotkeys", daemon=True)
        self._thread.start()
        if not self._ready.wait(5):
            raise SystemFailure("служебный поток горячих клавиш не запустился")

    def set_bindings(self, bindings: Sequence[tuple[int, Hotkey]]) -> None:
        with self._lock:
            self._wanted = list(bindings)
        _user32.PostThreadMessageW(self._tid, _WM_APP, 0, 0)

    def stop(self) -> None:
        if self._thread is None:
            return
        _user32.PostThreadMessageW(self._tid, _WM_QUIT, 0, 0)
        self._thread.join(3)

    def _run(self) -> None:
        self._tid = _kernel32.GetCurrentThreadId()
        msg = wintypes.MSG()
        _user32.PeekMessageW(ctypes.byref(msg), None, _WM_USER, _WM_USER, _PM_NOREMOVE)  # создаёт очередь сообщений
        self._ready.set()
        while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == _WM_HOTKEY:
                try:
                    self._on_hotkey(int(msg.wParam))
                except Exception:
                    log.exception("обработчик горячей клавиши")
            elif msg.message == _WM_APP:
                self._apply()
        self._unregister_all()

    def _unregister_all(self) -> None:
        for hotkey_id in self._registered:
            _user32.UnregisterHotKey(None, hotkey_id)
        self._registered = []

    def _apply(self) -> None:
        self._unregister_all()
        with self._lock:
            wanted = list(self._wanted)
        for hotkey_id, hotkey in wanted:
            if _user32.RegisterHotKey(None, hotkey_id, hotkey.modifiers | _MOD_NOREPEAT, hotkey.vk):
                self._registered.append(hotkey_id)
            elif ctypes.get_last_error() == _ERROR_HOTKEY_ALREADY_REGISTERED:
                self._on_error(f"{hotkey.label}: комбинация занята другим приложением")
            else:
                self._on_error(f"{hotkey.label}: не удалось зарегистрировать (код {ctypes.get_last_error()})")


def acquire_single_instance(name: str):
    """Мьютекс на время жизни процесса; None, если такой процесс уже есть."""
    handle = win32event.CreateMutex(None, False, name)
    if win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS:
        handle.Close()
        return None
    return handle


def single_instance_exists(name: str) -> bool:
    try:
        win32event.OpenMutex(win32event.SYNCHRONIZE, False, name).Close()
        return True
    except pywintypes.error:
        return False


def create_event(name: str):
    return win32event.CreateEvent(None, True, False, name)


def signal_event(name: str) -> bool:
    """Подать сигнал процессу, ждущему именованное событие; False, если ждущих нет."""
    try:
        handle = win32event.OpenEvent(win32event.EVENT_MODIFY_STATE, False, name)
    except pywintypes.error:
        return False
    try:
        win32event.SetEvent(handle)
        return True
    finally:
        handle.Close()


def wait_event(handle, timeout: float) -> bool:
    return win32event.WaitForSingleObject(handle, int(timeout * 1000)) == win32event.WAIT_OBJECT_0


@contextlib.contextmanager
def named_mutex(name: str, timeout: float = 30.0) -> Iterator[None]:
    """Не даёт двум командам ws (CLI и трей) одновременно менять состояние."""
    handle = win32event.CreateMutex(None, False, name)
    try:
        result = win32event.WaitForSingleObject(handle, int(timeout * 1000))
        if result not in (win32event.WAIT_OBJECT_0, win32event.WAIT_ABANDONED):
            raise SystemFailure("Другая команда ws ещё выполняется, попробуй позже")
        try:
            yield
        finally:
            win32event.ReleaseMutex(handle)
    finally:
        win32api.CloseHandle(handle)
