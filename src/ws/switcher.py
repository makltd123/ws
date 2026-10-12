"""Сценарии верхнего уровня: switch, activate (open с разморозкой), close."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable

from ws import freezer, launcher, manifest
from ws import state as state_mod
from ws.config import Runtime
from ws.errors import UserError, WsError
from ws.freezer import FreezeReport, ThawReport, plan
from ws.models import ProcRef, Space, SpaceState, State, WindowRef

log = logging.getLogger(__name__)

CLOSE_TIMEOUT = 10.0


@dataclass
class SwitchReport:
    name: str = ""  # имя открытого пространства (аргумент команды может быть путём)
    frozen: FreezeReport | None = None
    thawed: ThawReport | None = None
    opened: launcher.OpenReport = field(default_factory=launcher.OpenReport)
    warnings: list[str] = field(default_factory=list)

    @property
    def note(self) -> str | None:
        """«Где я остановился» в пространстве, на которое переключились."""
        return self.thawed.note if self.thawed is not None else None

    @property
    def all_warnings(self) -> list[str]:
        out = list(self.warnings)
        for part in (self.frozen, self.thawed):
            if part is not None:
                out += part.warnings
        return out + self.opened.warnings


@dataclass
class CloseReport:
    closed: int = 0
    not_closed: list[str] = field(default_factory=list)
    terminated: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def excludes_for(rt: Runtime, name: str) -> tuple[frozenset[str], str | None]:
    """freeze.exclude из манифеста пространства. Сломанный манифест не должен мешать заморозке."""
    try:
        return manifest.load(rt.paths, name).freeze_exclude, None
    except WsError as exc:
        return frozenset(), f"манифест '{name}' не прочитан ({exc}); использую только глобальный список исключений"


def activate(rt: Runtime, space: Space, state: State) -> SwitchReport:
    """Показать пространство: разморозить, если заморожено, и запустить недостающее."""
    report = SwitchReport(name=space.name)
    sp = state.spaces.get(space.name)
    if sp is not None and sp.frozen:
        report.thawed = freezer.thaw(rt, space.name, state)
    report.opened = launcher.open_space(rt, space, state)
    return report


def switch(rt: Runtime, target: Space, state: State) -> SwitchReport:
    """Заморозить активное пространство и открыть целевое."""
    current = state.active
    frozen: FreezeReport | None = None
    extra: list[str] = []
    if current and current != target.name:
        sp = state.spaces.get(current)
        if sp is not None and sp.windows and not sp.frozen:
            excludes, warning = excludes_for(rt, current)
            if warning:
                extra.append(warning)
            frozen = freezer.freeze(rt, current, state, excludes)
    report = activate(rt, target, state)
    report.frozen = frozen
    report.warnings += extra
    return report


def require_running(state: State, name: str) -> None:
    sp = state.spaces.get(manifest.validate_name(name))
    if sp is None or not sp.windows:
        raise UserError(f"У пространства '{name}' нет запущенных окон (ws list покажет статусы)")


# Сценарии «целиком»: блокировка, чтение состояния, действие, сохранение. Их вызывают CLI, трей и хоткеи.


def activate_named(rt: Runtime, name: str) -> SwitchReport:
    """name: имя пространства или путь к репозиторию с .workspace.yml (ws open .)."""
    space = manifest.resolve(rt.paths, name)
    with state_mod.locked(rt):
        state = state_mod.load_pruned(rt)
        report = activate(rt, space, state)
        state_mod.save(rt.paths, state)
    return report


def switch_named(rt: Runtime, name: str) -> SwitchReport:
    space = manifest.resolve(rt.paths, name)
    with state_mod.locked(rt):
        state = state_mod.load_pruned(rt)
        report = switch(rt, space, state)
        state_mod.save(rt.paths, state)
    return report


@dataclass
class FreezeResult:
    name: str
    report: FreezeReport
    warnings: list[str]


def freeze_named(rt: Runtime, name: str | None = None) -> FreezeResult:
    """Заморозить указанное пространство или активное, если имя не задано."""
    with state_mod.locked(rt):
        state = state_mod.load_pruned(rt)
        target = name or state.active
        if target is None:
            raise UserError("Нет активного пространства; укажи имя: ws freeze <имя>")
        require_running(state, target)
        excludes, warning = excludes_for(rt, target)
        report = freezer.freeze(rt, target, state, excludes)
        if state.active == target:
            state.active = None  # после заморозки ничего не показано
        state_mod.save(rt.paths, state)
    return FreezeResult(target, report, ([warning] if warning else []) + report.warnings)


def thaw_named(rt: Runtime, name: str) -> ThawReport:
    with state_mod.locked(rt):
        state = state_mod.load_pruned(rt)
        require_running(state, name)
        report = freezer.thaw(rt, name, state)
        state_mod.save(rt.paths, state)
    return report


def close(
    rt: Runtime,
    name: str,
    state: State,
    confirm_kill: Callable[[list[str]], bool] | None = None,
    timeout: float = CLOSE_TIMEOUT,
) -> CloseReport:
    """Закрыть окна пространства через WM_CLOSE. Процессы завершаются только с подтверждением."""
    report = CloseReport()
    sp = state.spaces.get(name)
    if sp is None or not sp.windows:
        report.warnings.append(f"у пространства '{name}' нет запущенных окон")
        return report
    if sp.frozen:
        freezer.thaw(rt, name, state)  # замороженное приложение WM_CLOSE не обработает

    targets = [w for w in sp.windows if not w.uncontrollable]
    for ref in sp.windows:
        if ref.uncontrollable:
            report.not_closed.append(f"{ref.exe}: запущено от администратора, закрой вручную")
    for ref in targets:
        rt.win.request_close(ref.hwnd)

    deadline = rt.clock.now() + timeout
    remaining = [w for w in targets if rt.win.get_window(w.hwnd) is not None]
    while remaining and rt.clock.now() < deadline:
        rt.clock.sleep(0.3)
        remaining = [w for w in targets if rt.win.get_window(w.hwnd) is not None]
    report.closed = len(targets) - len(remaining)

    if remaining and confirm_kill is not None:
        _terminate(rt, name, state, remaining, confirm_kill, report)
        remaining = [w for w in remaining if rt.win.get_window(w.hwnd) is not None]
    for ref in remaining:
        report.not_closed.append(
            f"{ref.exe}: окно не закрылось (возможно, спрашивает о сохранении; закрой его в самом окне)"
        )

    sp.windows = [w for w in sp.windows if w in remaining or w.uncontrollable]
    if not sp.windows:
        del state.spaces[name]
    if state.active == name and name not in state.spaces:
        state.active = None
    state_mod.save(rt.paths, state)
    return report


def _terminate(
    rt: Runtime,
    name: str,
    state: State,
    remaining: list[WindowRef],
    confirm_kill: Callable[[list[str]], bool],
    report: CloseReport,
) -> None:
    """Принудительное завершение, только для процессов, целиком принадлежащих этому пространству."""
    sub = SpaceState(windows=remaining)  # оболочки вкладок не нужны: принудительно завершаются только окна
    windows = {w.hwnd: w for w in rt.win.list_windows()}
    other = {w.hwnd for n, s in state.spaces.items() if n != name for w in s.windows}
    fplan = plan(sub, windows, other, lambda pid: rt.procs.tree(pid), rt.procs.self_and_ancestors(), ())
    victims = []
    for root in fplan.roots:
        if root.skip_reason:
            report.warnings.append(f"{root.windows[0].exe}: {root.skip_reason}; принудительно не завершается")
            continue
        victims += root.procs
    if not victims:
        return
    labels = [f"{p.exe} (pid {p.pid})" for p in victims]
    if not confirm_kill(labels):
        return
    # потомки раньше корня
    for p in reversed(victims):
        if rt.procs.terminate(ProcRef(p.pid, p.created)):
            report.terminated.append(f"{p.exe} (pid {p.pid})")
    rt.clock.sleep(0.3)
