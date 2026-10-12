from fakes import FakeClock, FakeProcessSystem, FakeWindowSystem

from ws import state as state_mod
from ws.config import Paths
from ws.models import ProcRef, Rect, SpaceState, State, WindowRef


def _state() -> State:
    return State(
        active="a",
        spaces={
            "a": SpaceState(
                windows=[
                    WindowRef(101, 10, 510.0, "Code.exe", "t", 0, Rect(1, 2, 3, 4)),
                    WindowRef(102, 11, 511.0, "wt.exe", "t", 1),
                ],
                frozen_procs=[ProcRef(10, 510.0)],
                last_active=5.0,
            )
        },
    )


def test_roundtrip(tmp_path):
    paths = Paths(tmp_path)
    paths.ensure()
    state_mod.save(paths, _state())
    assert state_mod.load(paths) == _state()


def test_corrupt_state_is_set_aside(tmp_path):
    paths = Paths(tmp_path)
    paths.ensure()
    paths.state_file.write_text("{not json", encoding="utf-8")
    assert state_mod.load(paths) == State()
    assert (tmp_path / "state.json.bad").exists()


def test_prune_drops_dead_windows_and_reused_pids():
    clock = FakeClock()
    win, procs = FakeWindowSystem(clock), FakeProcessSystem(clock)
    live = win.add(win.make_window("Code.exe", "X", 10))
    # hwnd 102 исчез; pid 10 жив
    procs.add(10, "Code.exe")
    state = _state()
    state.spaces["a"].windows[0] = WindowRef(live.hwnd, 10, procs.procs[10].created, "Code.exe", "t", 0)
    state.spaces["a"].frozen_procs = [ProcRef(10, procs.procs[10].created)]

    windows = {w.hwnd: w for w in win.list_windows()}
    pruned = state_mod.prune(state, windows, lambda pid: procs.procs[pid].created if pid in procs.procs else None)
    assert [w.hwnd for w in pruned.spaces["a"].windows] == [live.hwnd]
    assert len(pruned.spaces["a"].frozen_procs) == 1


def test_prune_pid_reuse_is_not_the_same_process():
    state = _state()
    # hwnd жив, pid тот же, но процесс другой (время старта не совпало)
    from ws.models import WindowInfo

    info = WindowInfo(101, 10, "x", "t", "c", True, False, False, False, False, 0, Rect(0, 0, 1, 1), Rect(0, 0, 1, 1))
    pruned = state_mod.prune(state, {101: info}, lambda pid: 999.0)
    assert pruned.spaces["a"].windows == []
    assert pruned.spaces["a"].frozen_procs == []
    assert pruned.active is None  # активное пространство без окон перестаёт быть активным


def test_unknown_creation_time_in_record_matches_any_live_process():
    assert state_mod.same_process(123.0, 0.0)
    assert not state_mod.same_process(None, 0.0)
