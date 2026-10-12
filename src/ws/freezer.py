"""Заморозка и разморозка пространства.

Главный принцип: худший исход при любой ошибке это «не заморозили», а не «заморозили чужое».
Процесс замораживается, только если ни одного видимого окна вне замораживаемого пространства
у него (и у его потомков) нет, и только если окна успешно спрятаны.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Collection, Mapping

from ws import layout, notes
from ws import state as state_mod
from ws.config import Runtime
from ws.launcher import is_candidate, place
from ws.models import ProcInfo, ProcRef, SpaceState, State, WindowInfo, WindowRef

log = logging.getLogger(__name__)

# Эти процессы не замораживаются никогда. Explorer это ещё и панель задач, остальное системные хосты.
# Сам Windows Terminal тоже здесь: он один на все окна, замораживаются только оболочки вкладок.
NEVER_FREEZE = frozenset(
    {
        "explorer.exe",
        "dwm.exe",
        "applicationframehost.exe",
        "csrss.exe",
        "winlogon.exe",
        "wininit.exe",
        "services.exe",
        "lsass.exe",
        "smss.exe",
        "svchost.exe",
        "system",
        "registry",
        "sihost.exe",
        "ctfmon.exe",
        "fontdrvhost.exe",
        "runtimebroker.exe",
        "taskhostw.exe",
        "searchhost.exe",
        "searchapp.exe",
        "shellexperiencehost.exe",
        "startmenuexperiencehost.exe",
        "textinputhost.exe",
        "securityhealthsystray.exe",
        "windowsterminal.exe",
        "openconsole.exe",
        "conhost.exe",
    }
)


@dataclass
class Root:
    """Корень заморозки: процесс окна пространства или оболочка вкладки терминала."""

    pid: int
    windows: list[WindowRef] = field(default_factory=list)
    procs: list[ProcInfo] = field(default_factory=list)  # что заморозить (после фильтров)
    excluded: bool = False  # приложение из freeze.exclude: окно сворачивается, процесс работает
    skip_reason: str | None = None  # почему процесс не замораживается (для вывода пользователю)


@dataclass
class FreezePlan:
    roots: list[Root] = field(default_factory=list)

    @property
    def suspend(self) -> list[ProcInfo]:
        seen: set[int] = set()
        out = []
        for root in self.roots:
            for p in root.procs:
                if p.pid not in seen:
                    seen.add(p.pid)
                    out.append(p)
        return out


@dataclass
class FreezeReport:
    suspended: int = 0
    hidden: int = 0
    minimized: int = 0
    warnings: list[str] = field(default_factory=list)


@dataclass
class ThawReport:
    resumed: int = 0
    shown: int = 0
    warnings: list[str] = field(default_factory=list)
    note: str | None = None  # «где я остановился» для показа пользователю


def plan(
    sp: SpaceState,
    windows: Mapping[int, WindowInfo],
    other_hwnds: Collection[int],
    tree: Callable[[int], list[ProcInfo]],
    protected: Collection[int],
    excludes: Collection[str],
) -> FreezePlan:
    """Что заморозить. Чистая функция: все данные о системе приходят аргументами."""
    excludes = {e.lower() for e in excludes}
    space_hwnds = {w.hwnd for w in sp.windows}
    result = FreezePlan()

    by_pid: dict[int, Root] = {}
    for ref in sp.windows:
        if ref.uncontrollable or ref.hwnd not in windows:
            continue
        root = by_pid.setdefault(ref.pid, Root(pid=ref.pid))
        root.windows.append(ref)
    for root in by_pid.values():
        exe = root.windows[0].exe.lower()
        if exe in excludes:
            root.excluded = True
        elif exe in NEVER_FREEZE:
            root.skip_reason = None  # системное или общее приложение: окно прячем молча
        else:
            _evaluate(root, tree(root.pid), windows, space_hwnds, other_hwnds, protected, excludes, check_windows=True)
        result.roots.append(root)

    for shell in sp.shell_procs:
        root = Root(pid=shell.pid)
        _evaluate(root, tree(shell.pid), windows, space_hwnds, other_hwnds, protected, excludes, check_windows=False)
        result.roots.append(root)
    return result


def _evaluate(
    root: Root,
    procs: list[ProcInfo],
    windows: Mapping[int, WindowInfo],
    space_hwnds: Collection[int],
    other_hwnds: Collection[int],
    protected: Collection[int],
    excludes: Collection[str],
    check_windows: bool,
) -> None:
    if not procs:
        return  # процесса уже нет
    pids = {p.pid for p in procs}
    if pids & set(protected):
        root.skip_reason = "ws запущен изнутри этого приложения, его процессы не замораживаются"
        return
    if check_windows:
        foreign = [
            w
            for w in windows.values()
            if w.pid in pids and w.hwnd not in space_hwnds and (w.hwnd in other_hwnds or is_candidate(w))
        ]
        if foreign:
            root.skip_reason = f"в процессе есть окна вне этого пространства ({foreign[0].title[:40]!r})"
            return
    root.procs = [p for p in procs if p.exe.lower() not in NEVER_FREEZE and p.exe.lower() not in excludes]


def freeze(rt: Runtime, name: str, state: State, excludes: Collection[str] = ()) -> FreezeReport:
    report = FreezeReport()
    sp = state.spaces.get(name)
    if sp is None or not sp.windows:
        report.warnings.append(f"у пространства '{name}' нет запущенных окон")
        return report
    if sp.frozen:
        report.warnings.append(f"пространство '{name}' уже заморожено")
        return report

    windows = {w.hwnd: w for w in rt.win.list_windows()}
    other = {w.hwnd for n, s in state.spaces.items() if n != name for w in s.windows}
    all_excludes = {e.lower() for e in excludes} | {e.lower() for e in rt.config.freeze_exclude}
    fplan = plan(sp, windows, other, lambda pid: rt.procs.tree(pid), rt.procs.self_and_ancestors(), all_excludes)

    for ref in sp.windows:
        info = windows.get(ref.hwnd)
        if info is not None and not info.minimized:
            ref.last_rect = info.frame_rect

    # «Где я остановился»: до того как окна спрятаны, пока видно, какое из них было верхним.
    # Заметка вторична, её сбой не должен мешать заморозке.
    try:
        note = notes.capture(
            rt.paths, name, sp, list(windows.values()), rt.clock.now(), lambda pid: rt.procs.info(pid) is not None
        )
        notes.save(rt.paths, name, note)
    except Exception:
        log.exception("не удалось сохранить заметку пространства %s", name)

    # Намерение записывается до действий: если ws упадёт посередине, ws thaw знает, что возвращать.
    sp.frozen = True
    sp.frozen_procs = [ProcRef(p.pid, p.created) for p in fplan.suspend]
    state_mod.save(rt.paths, state)

    failed: set[int] = set()  # корни, чьи окна спрятать не удалось: такие процессы не замораживаем
    for root in fplan.roots:
        for ref in root.windows:
            if root.excluded:
                ok = rt.win.minimize(ref.hwnd)
                report.minimized += int(ok)
            else:
                ok = rt.win.hide(ref.hwnd)
                report.hidden += int(ok)
            if not ok:
                failed.add(root.pid)
                report.warnings.append(f"{ref.exe}: окно не удалось спрятать, процесс не замораживается")
        if root.skip_reason and root.windows:
            report.warnings.append(f"{root.windows[0].exe}: {root.skip_reason}")
        elif root.skip_reason:
            report.warnings.append(f"оболочка вкладки терминала (pid {root.pid}): {root.skip_reason}")

    done: list[ProcRef] = []
    for root in fplan.roots:
        if root.pid in failed:
            continue
        for p in root.procs:
            ref = ProcRef(p.pid, p.created)
            if ref in done:
                continue
            if rt.procs.suspend(ref):
                done.append(ref)
            else:
                report.warnings.append(f"{p.exe} (pid {p.pid}): не удалось заморозить, продолжаю без него")
    sp.frozen_procs = done
    report.suspended = len(done)
    state_mod.save(rt.paths, state)
    return report


def thaw(rt: Runtime, name: str, state: State) -> ThawReport:
    report = ThawReport()
    sp = state.spaces.get(name)
    if sp is None:
        report.warnings.append(f"у пространства '{name}' нет запущенных окон")
        return report
    if not sp.frozen:
        report.warnings.append(f"пространство '{name}' не заморожено")
        return report

    note = notes.load(rt.paths, name)
    if note is not None:
        report.note = notes.describe(note, rt.clock.now())

    # Сначала процессы, потом окна: размороженное приложение сразу готово перерисоваться.
    for ref in reversed(sp.frozen_procs):
        if rt.procs.resume(ref):
            report.resumed += 1
    monitors = rt.win.monitors()
    for ref in sp.windows:
        if ref.uncontrollable:
            continue
        if not rt.win.show(ref.hwnd):
            continue
        report.shown += 1
        info = rt.win.get_window(ref.hwnd)
        if info is not None and not info.minimized and not layout.on_screen(info.frame_rect, monitors):
            # монитор отключили, пока пространство спало: возвращаем окно на видимое место
            place(rt, ref.hwnd, layout.to_spec(ref.last_rect or info.frame_rect, monitors), monitors)
    for ref in reversed(sp.windows):  # первое окно пространства оказывается сверху
        if not ref.uncontrollable:
            rt.win.focus(ref.hwnd)

    sp.frozen = False
    sp.frozen_procs = []
    state_mod.save(rt.paths, state)
    return report
