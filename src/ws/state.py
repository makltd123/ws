"""state.json: какие окна и процессы каким пространствам принадлежат."""

from __future__ import annotations

import contextlib
import dataclasses
import json
import logging
import os
from typing import Any, Callable, ContextManager, Mapping

from ws.config import Paths, Runtime
from ws.models import ProcRef, Rect, SpaceState, State, WindowInfo, WindowRef

log = logging.getLogger(__name__)

# Допуск при сравнении времени старта процесса (float переживает JSON без потерь,
# но разные вызовы psutil могут расходиться в последних знаках).
_CREATED_TOLERANCE = 0.05


def load(paths: Paths) -> State:
    if not paths.state_file.exists():
        return State()
    try:
        raw = json.loads(paths.state_file.read_text(encoding="utf-8"))
        return _from_json(raw)
    except (ValueError, KeyError, TypeError) as exc:
        backup = paths.state_file.with_suffix(".json.bad")
        log.error("state.json повреждён (%s), копия сохранена в %s", exc, backup)
        os.replace(paths.state_file, backup)
        return State()


def save(paths: Paths, state: State) -> None:
    tmp = paths.state_file.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(dataclasses.asdict(state), ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, paths.state_file)


def same_process(actual_created: float | None, recorded: float) -> bool:
    if actual_created is None:
        return False
    return recorded == 0.0 or abs(actual_created - recorded) <= _CREATED_TOLERANCE


def prune(state: State, windows: Mapping[int, WindowInfo], created: Callable[[int], float | None]) -> State:
    """Молча выкинуть записи об окнах и процессах, которых больше нет."""

    def window_alive(ref: WindowRef) -> bool:
        info = windows.get(ref.hwnd)
        return info is not None and info.pid == ref.pid and same_process(created(ref.pid), ref.pid_created)

    def proc_alive(ref: ProcRef) -> bool:
        return same_process(created(ref.pid), ref.created)

    for space in state.spaces.values():
        space.windows = [w for w in space.windows if window_alive(w)]
        space.frozen_procs = [p for p in space.frozen_procs if proc_alive(p)]
        space.shell_procs = [p for p in space.shell_procs if proc_alive(p)]
        if not space.windows and not space.frozen_procs:
            space.frozen = False
    if state.active is not None:
        active = state.spaces.get(state.active)
        if active is None or not active.windows:
            state.active = None
    return state


def load_pruned(rt: Runtime) -> State:
    """Прочитать состояние и сверить его с тем, что реально запущено."""
    state = load(rt.paths)
    windows = {w.hwnd: w for w in rt.win.list_windows()}

    def created(pid: int) -> float | None:
        info = rt.procs.info(pid)
        return info.created if info is not None else None

    return prune(state, windows, created)


def locked(rt: Runtime) -> ContextManager[None]:
    """Взаимная блокировка команд ws (CLI и трей); в тестах lock не задан."""
    return rt.lock() if rt.lock is not None else contextlib.nullcontext()


def owned_hwnds(state: State) -> set[int]:
    return {w.hwnd for space in state.spaces.values() for w in space.windows}


def _rect(raw: Any) -> Rect | None:
    return Rect(**raw) if raw else None


def _from_json(raw: dict[str, Any]) -> State:
    spaces: dict[str, SpaceState] = {}
    for name, sp in (raw.get("spaces") or {}).items():
        spaces[name] = SpaceState(
            windows=[
                WindowRef(
                    hwnd=int(w["hwnd"]),
                    pid=int(w["pid"]),
                    pid_created=float(w.get("pid_created", 0.0)),
                    exe=str(w.get("exe", "")),
                    title=str(w.get("title", "")),
                    item_index=w.get("item_index"),
                    last_rect=_rect(w.get("last_rect")),
                    uncontrollable=bool(w.get("uncontrollable", False)),
                )
                for w in sp.get("windows") or []
            ],
            frozen=bool(sp.get("frozen", False)),
            frozen_procs=[ProcRef(int(p["pid"]), float(p["created"])) for p in sp.get("frozen_procs") or []],
            shell_procs=[ProcRef(int(p["pid"]), float(p["created"])) for p in sp.get("shell_procs") or []],
            last_active=float(sp.get("last_active", 0.0)),
        )
    return State(active=raw.get("active"), spaces=spaces)
