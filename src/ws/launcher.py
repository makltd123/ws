"""Запуск элементов пространства, привязка новых окон и расстановка."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Collection, Iterable

from ws import layout
from ws import state as state_mod
from ws.adapters import adapter_for
from ws.config import Runtime
from ws.errors import SystemFailure, WsError
from ws.models import (
    Item,
    MonitorInfo,
    ProcInfo,
    ProcRef,
    Space,
    SpaceState,
    State,
    TerminalItem,
    WindowInfo,
    WindowRef,
    WindowSpec,
)

log = logging.getLogger(__name__)

# Эти процессы не привязываются по запасному правилу «единственное новое окно».
_NEVER_LOOSE = {"explorer.exe", "applicationframehost.exe", "searchhost.exe", "startmenuexperiencehost.exe"}
_PLACE_ATTEMPTS = 4
_PLACE_SETTLE = 0.15
_PLACE_FINAL_GRACE = 2.0
_SHELL_WAIT = 3.0
_CONSOLE_HOSTS = {"openconsole.exe", "conhost.exe"}


@dataclass
class OpenReport:
    launched: list[str] = field(default_factory=list)
    reused: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def is_candidate(w: WindowInfo) -> bool:
    """Окно, которое пользователь видит как самостоятельное."""
    return (
        w.visible
        and not w.cloaked
        and not w.tool_window
        and w.owner == 0
        and w.frame_rect.width > 1
        and w.frame_rect.height > 1
    )


def pick_new_windows(
    before: Collection[int],
    after: Iterable[WindowInfo],
    owned: Collection[int],
    matcher: Callable[[WindowInfo], bool],
) -> list[WindowInfo]:
    """Окна, которых не было в снимке до запуска, свободные и подходящие под ожидание."""
    return [w for w in after if w.hwnd not in before and w.hwnd not in owned and is_candidate(w) and matcher(w)]


def open_space(rt: Runtime, space: Space, state: State) -> OpenReport:
    report = OpenReport()
    sp = state.spaces.setdefault(space.name, SpaceState())
    monitors = rt.win.monitors()
    for index, item in enumerate(space.items):
        label = f"{item.type} #{index + 1}"
        try:
            ref = next((w for w in sp.windows if w.item_index == index), None)
            if ref is not None:
                _bring_forward(rt, ref)
                report.reused.append(label)
            elif _launch_item(rt, space, state, sp, index, item, label, monitors, report):
                report.launched.append(label)
        except WsError as exc:
            report.warnings.append(f"{label}: {exc}")
        except Exception as exc:  # падение одного адаптера не ломает остальные
            log.exception("элемент %s пространства %s", label, space.name)
            report.warnings.append(f"{label}: {type(exc).__name__}: {exc} (подробности в ws.log)")
    sp.last_active = rt.clock.now()
    state.active = space.name
    return report


def _bring_forward(rt: Runtime, ref: WindowRef) -> None:
    if ref.uncontrollable:
        return
    rt.win.show(ref.hwnd)
    rt.win.focus(ref.hwnd)


def _launch_item(
    rt: Runtime,
    space: Space,
    state: State,
    sp: SpaceState,
    index: int,
    item: Item,
    label: str,
    monitors: list[MonitorInfo],
    report: OpenReport,
) -> bool:
    adapter = adapter_for(item)
    cmd = adapter.launch(item, space, rt)
    before = {w.hwnd for w in rt.win.list_windows()}
    owned = state_mod.owned_hwnds(state)
    shells_before = set(_tab_shells(rt)) if isinstance(item, TerminalItem) else set()
    try:
        # WS_SPACE читает PowerShell-хук (ws hook), чтобы записывать заметки в нужное пространство
        rt.procs.spawn(cmd.argv, {**space.env, "WS_SPACE": space.name, **cmd.env}, cmd.cwd, cmd.env_remove)
    except OSError as exc:
        raise SystemFailure(f"не удалось запустить {cmd.argv[0]}: {exc}") from exc

    window = _await_window(rt, adapter, item, before, owned)
    if window is None:
        report.warnings.append(
            f"{label}: окно не появилось за {rt.config.bind_timeout:g} с, элемент не привязан к пространству"
        )
        return False

    proc = rt.procs.info(window.pid)
    ref = WindowRef(
        hwnd=window.hwnd,
        pid=window.pid,
        pid_created=proc.created if proc else 0.0,
        exe=window.exe,
        title=window.title,
        item_index=index,
    )
    elevated = rt.win.is_elevated(window.pid)
    if elevated:
        ref.uncontrollable = True
        report.warnings.append(f"{label}: окно запущено от администратора, ws не может им управлять")
    if window.class_name == "ApplicationFrameWindow":
        report.warnings.append(f"{label}: UWP-приложение, управление окном может работать неполно")
    sp.windows.append(ref)
    state_mod.save(rt.paths, state)  # привязка сохраняется сразу: сбой дальше её не потеряет

    if item.window is not None and not elevated:
        if not place(rt, window.hwnd, item.window, monitors):
            report.warnings.append(f"{label}: окно не встало на заданное место (приложение возвращает свою позицию)")
    if isinstance(item, TerminalItem) and not elevated:
        # Сам Windows Terminal один на все окна и не замораживается; замораживаются оболочки наших вкладок.
        sp.shell_procs += _collect_shells(rt, shells_before, expected=max(1, len(item.tabs)))
        state_mod.save(rt.paths, state)
    return True


def _tab_shells(rt: Runtime) -> dict[tuple[int, float], ProcInfo]:
    """Оболочки вкладок Windows Terminal: прямые потомки WindowsTerminal.exe, кроме хостов консоли.

    Структура проверена на Windows Terminal 1.24: WindowsTerminal.exe -> {OpenConsole.exe, powershell.exe, ...}.
    """
    procs = rt.procs.find(lambda p: True)
    exe_by_pid = {p.pid: p.exe.lower() for p in procs}
    return {
        (p.pid, p.created): p
        for p in procs
        if exe_by_pid.get(p.parent or -1) == "windowsterminal.exe" and p.exe.lower() not in _CONSOLE_HOSTS
    }


def _collect_shells(rt: Runtime, before: Collection[tuple[int, float]], expected: int) -> list[ProcRef]:
    """Новые оболочки вкладок после запуска; они появляются чуть позже окна, поэтому ждём."""
    deadline = rt.clock.now() + _SHELL_WAIT
    while True:
        new = [p for key, p in _tab_shells(rt).items() if key not in before]
        if len(new) >= expected or rt.clock.now() >= deadline:
            return [ProcRef(p.pid, p.created) for p in new]
        rt.clock.sleep(rt.config.poll_interval)


def _await_window(
    rt: Runtime, adapter, item: Item, before: Collection[int], owned: Collection[int]
) -> WindowInfo | None:
    started = rt.clock.now()
    deadline = started + rt.config.bind_timeout
    while True:
        after = rt.win.list_windows()
        found = pick_new_windows(before, after, owned, lambda w: adapter.matches(item, w))
        if found:
            return found[0]
        if adapter.loose and rt.clock.now() - started >= rt.config.loose_grace:
            loose = pick_new_windows(before, after, owned, lambda w: w.exe.lower() not in _NEVER_LOOSE)
            if len(loose) == 1:
                return loose[0]
        if rt.clock.now() >= deadline:
            return None
        rt.clock.sleep(rt.config.poll_interval)


def place(rt: Runtime, hwnd: int, spec: WindowSpec, monitors: list[MonitorInfo]) -> bool:
    """Поставить видимую часть окна на место. Приложения при старте двигают окна сами, поэтому повторяем."""
    target = layout.resolve(spec, monitors)
    for _ in range(_PLACE_ATTEMPTS):
        info = rt.win.get_window(hwnd)
        if info is None:
            return False
        if info.minimized or info.maximized:
            rt.win.restore(hwnd)
            info = rt.win.get_window(hwnd)
            if info is None:
                return False
        ins = layout.insets(info.rect, info.frame_rect)
        rt.win.move(hwnd, layout.outer_rect(target, ins))
        rt.clock.sleep(_PLACE_SETTLE)
        info = rt.win.get_window(hwnd)
        if info is None:
            return False
        if spec.maximized:
            rt.win.maximize(hwnd)  # окно уже на нужном мониторе, максимизация займёт его
            return True
        if layout.close_enough(info.frame_rect, target):
            return True
    # Асинхронное перемещение и «ленивые» приложения (Проводник встаёт на место через ~1-2 с) доходят позже.
    deadline = rt.clock.now() + _PLACE_FINAL_GRACE
    while rt.clock.now() < deadline:
        rt.clock.sleep(0.2)
        info = rt.win.get_window(hwnd)
        if info is None:
            return False
        if layout.close_enough(info.frame_rect, target):
            return True
    return False
