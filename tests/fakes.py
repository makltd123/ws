"""Фейковые Windows и процессы: сценарии проверяются без реального GUI."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Mapping, Sequence

from ws.config import Config, Paths, Runtime
from ws.models import MonitorInfo, ProcInfo, ProcRef, Rect, WindowInfo

INSET = (7, 0, 7, 7)  # невидимая рамка Win10/11: left, top, right, bottom


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


def monitor(index: int, left: int, top: int, w: int, h: int, primary: bool = False, taskbar: int = 40) -> MonitorInfo:
    bounds = Rect(left, top, left + w, top + h)
    work = Rect(left, top, left + w, top + h - taskbar)
    return MonitorInfo(index=index, primary=primary, bounds=bounds, work=work, dpi=96)


MONITORS = [monitor(0, 0, 0, 1920, 1080, primary=True), monitor(1, 1920, 0, 1920, 1080)]


@dataclass
class _Pending:
    at: float
    info: WindowInfo


class FakeWindowSystem:
    def __init__(self, clock: FakeClock, monitors: Sequence[MonitorInfo] = tuple(MONITORS)) -> None:
        self.clock = clock
        self._monitors = list(monitors)
        self.windows: dict[int, WindowInfo] = {}
        self.pending: list[_Pending] = []
        self.ignore_moves: dict[int, int] = {}  # hwnd -> сколько первых move приложение «сбрасывает»
        self.stubborn: set[int] = set()  # hwnd, которые игнорируют WM_CLOSE (диалог «сохранить?»)
        self.elevated: set[int] = set()  # pid
        self.explorer_folder_map: dict[int, Path] = {}  # hwnd -> папка
        self.calls: list[tuple[str, int]] = []
        self._next = 100

    # --- настройка сценария
    def make_window(self, exe: str, cls: str, pid: int, visible: bool = True, rect: Rect | None = None) -> WindowInfo:
        self._next += 1
        outer = rect or Rect(100, 100, 900, 700)
        frame = Rect(outer.left + INSET[0], outer.top + INSET[1], outer.right - INSET[2], outer.bottom - INSET[3])
        return WindowInfo(
            hwnd=self._next, pid=pid, exe=exe, title=exe, class_name=cls, visible=visible, minimized=False,
            maximized=False, cloaked=False, tool_window=False, owner=0, rect=outer, frame_rect=frame,
        )

    def add(self, info: WindowInfo) -> WindowInfo:
        self.windows[info.hwnd] = info
        return info

    def appear_later(self, delay: float, info: WindowInfo) -> WindowInfo:
        self.pending.append(_Pending(self.clock.now() + delay, info))
        return info

    # --- WindowSystem
    def enable_dpi_awareness(self) -> bool:
        return True

    def monitors(self) -> list[MonitorInfo]:
        return list(self._monitors)

    def _reveal(self) -> None:
        for p in [p for p in self.pending if p.at <= self.clock.now()]:
            self.windows[p.info.hwnd] = p.info
            self.pending.remove(p)

    def list_windows(self) -> list[WindowInfo]:
        self._reveal()
        return list(self.windows.values())

    def get_window(self, hwnd: int) -> WindowInfo | None:
        self._reveal()
        return self.windows.get(hwnd)

    def move(self, hwnd: int, rect: Rect) -> bool:
        self.calls.append(("move", hwnd))
        if self.ignore_moves.get(hwnd, 0) > 0:
            self.ignore_moves[hwnd] -= 1
            return True
        info = self.windows[hwnd]
        frame = Rect(rect.left + INSET[0], rect.top + INSET[1], rect.right - INSET[2], rect.bottom - INSET[3])
        self.windows[hwnd] = replace(info, rect=rect, frame_rect=frame)
        return True

    def _set(self, hwnd: int, **changes) -> bool:
        if hwnd not in self.windows:
            return False
        self.windows[hwnd] = replace(self.windows[hwnd], **changes)
        return True

    def restore(self, hwnd: int) -> bool:
        self.calls.append(("restore", hwnd))
        return self._set(hwnd, minimized=False, maximized=False, visible=True)

    def maximize(self, hwnd: int) -> bool:
        self.calls.append(("maximize", hwnd))
        return self._set(hwnd, maximized=True)

    def hide(self, hwnd: int) -> bool:
        self.calls.append(("hide", hwnd))
        return self._set(hwnd, visible=False)

    def minimize(self, hwnd: int) -> bool:
        self.calls.append(("minimize", hwnd))
        return self._set(hwnd, minimized=True)

    def show(self, hwnd: int) -> bool:
        self.calls.append(("show", hwnd))
        return self._set(hwnd, visible=True, minimized=False)

    def focus(self, hwnd: int) -> bool:
        self.calls.append(("focus", hwnd))
        return hwnd in self.windows

    def request_close(self, hwnd: int) -> bool:
        self.calls.append(("close", hwnd))
        if hwnd in self.stubborn:
            return True
        return self.windows.pop(hwnd, None) is not None

    def is_hung(self, hwnd: int) -> bool:
        return False

    def is_elevated(self, pid: int) -> bool | None:
        return pid in self.elevated

    def explorer_folders(self) -> dict[int, Path]:
        return dict(self.explorer_folder_map)


class FakeProcessSystem:
    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.procs: dict[int, ProcInfo] = {}
        self.spawned: list[tuple[list[str], dict[str, str], Path | None, tuple[str, ...]]] = []
        self.on_spawn: Callable[[list[str]], None] = lambda argv: None
        self.suspended: set[int] = set()
        self.terminated: list[int] = []
        self.suspend_refuses: set[int] = set()  # pid, которые не удаётся заморозить (нет доступа)

    def add(
        self, pid: int, exe: str, parent: int | None = None, cmdline: Sequence[str] = (), path: str = ""
    ) -> ProcInfo:
        info = ProcInfo(
            pid=pid, exe=exe, cmdline=tuple(cmdline), created=500.0 + pid, parent=parent, status="running", path=path
        )
        self.procs[pid] = info
        return info

    def info(self, pid: int) -> ProcInfo | None:
        return self.procs.get(pid)

    def tree(self, pid: int) -> list[ProcInfo]:
        if pid not in self.procs:
            return []
        out, todo = [], [pid]
        while todo:
            current = todo.pop()
            out.append(self.procs[current])
            todo += [p.pid for p in self.procs.values() if p.parent == current]
        return out

    def find(self, pred: Callable[[ProcInfo], bool]) -> list[ProcInfo]:
        return [p for p in self.procs.values() if pred(p)]

    def suspend(self, ref: ProcRef) -> bool:
        if ref.pid not in self.procs or ref.pid in self.suspend_refuses:
            return False
        self.suspended.add(ref.pid)
        return True

    def resume(self, ref: ProcRef) -> bool:
        self.suspended.discard(ref.pid)
        return ref.pid in self.procs

    def terminate(self, ref: ProcRef) -> bool:
        self.terminated.append(ref.pid)
        self.suspended.discard(ref.pid)
        return self.procs.pop(ref.pid, None) is not None

    def spawn(
        self,
        argv: Sequence[str],
        env: Mapping[str, str],
        cwd: Path | None,
        env_remove: Sequence[str] = (),
    ) -> int:
        self.spawned.append((list(argv), dict(env), cwd, tuple(env_remove)))
        self.on_spawn(list(argv))
        return 9000 + len(self.spawned)

    def self_and_ancestors(self) -> set[int]:
        return {1}


def make_runtime(tmp_path: Path, **config_kwargs) -> tuple[Runtime, FakeWindowSystem, FakeProcessSystem, FakeClock]:
    clock = FakeClock()
    win = FakeWindowSystem(clock)
    procs = FakeProcessSystem(clock)
    paths = Paths(tmp_path)
    paths.ensure()
    rt = Runtime(paths=paths, config=Config(**config_kwargs), win=win, procs=procs, clock=clock)
    return rt, win, procs, clock
