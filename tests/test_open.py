"""Сценарий open на фейках: привязка по снимкам, расстановка, идемпотентность."""

from pathlib import Path

from fakes import make_runtime

from ws import launcher, layout
from ws import state as state_mod
from ws.launcher import is_candidate, pick_new_windows
from ws.models import ExplorerItem, Rect, Space, WindowSpec


def _explorer_space(tmp_path: Path, window: WindowSpec | None = None) -> Space:
    return Space(name="demo", items=(ExplorerItem(path=tmp_path, window=window),))


def _arm(win, procs, exe="explorer.exe", cls="CabinetWClass", delay=1.0, pid=700):
    """При запуске через delay секунд появляется окно нужного приложения."""
    procs.add(pid, exe)
    procs.on_spawn = lambda argv: win.appear_later(delay, win.make_window(exe, cls, pid))


def test_pick_new_windows_ignores_old_owned_and_unsuitable(tmp_path):
    rt, win, procs, clock = make_runtime(tmp_path)
    old = win.add(win.make_window("explorer.exe", "CabinetWClass", 1))
    mine = win.add(win.make_window("explorer.exe", "CabinetWClass", 1))
    hidden = win.add(win.make_window("explorer.exe", "CabinetWClass", 1, visible=False))
    other = win.add(win.make_window("notepad.exe", "Notepad", 2))
    fresh = win.add(win.make_window("explorer.exe", "CabinetWClass", 1))

    found = pick_new_windows({old.hwnd}, win.list_windows(), {mine.hwnd}, lambda w: w.exe == "explorer.exe")
    assert [w.hwnd for w in found] == [fresh.hwnd]
    assert hidden.hwnd not in [w.hwnd for w in found] and other.hwnd not in [w.hwnd for w in found]


def test_is_candidate_filters_tool_windows_and_owned_dialogs(tmp_path):
    rt, win, *_ = make_runtime(tmp_path)
    base = win.make_window("a.exe", "c", 1)
    from dataclasses import replace

    assert is_candidate(base)
    assert not is_candidate(replace(base, tool_window=True))
    assert not is_candidate(replace(base, owner=5))
    assert not is_candidate(replace(base, cloaked=True))
    assert not is_candidate(replace(base, frame_rect=Rect(0, 0, 0, 0)))


def test_open_launches_binds_and_places(tmp_path):
    rt, win, procs, clock = make_runtime(tmp_path)
    _arm(win, procs)
    spec = WindowSpec(monitor=1, x=100, y=50, w=700, h=400)
    state = state_mod.State()

    report = launcher.open_space(rt, _explorer_space(tmp_path, spec), state)

    assert report.warnings == []
    assert report.launched == ["explorer #1"]
    assert procs.spawned[0][0] == ["explorer.exe", str(tmp_path)]
    (ref,) = state.spaces["demo"].windows
    assert ref.item_index == 0 and ref.pid == 700 and ref.pid_created == procs.procs[700].created
    placed = win.windows[ref.hwnd]
    # видимая часть окна (без невидимой рамки) встала ровно на заданное место
    assert placed.frame_rect == layout.resolve(spec, rt.win.monitors()) == Rect(2020, 50, 2720, 450)
    assert state.active == "demo"
    assert state_mod.load(rt.paths).spaces["demo"].windows[0].hwnd == ref.hwnd  # привязка сохранена на диск


def test_second_open_does_not_duplicate(tmp_path):
    rt, win, procs, clock = make_runtime(tmp_path)
    _arm(win, procs)
    state = state_mod.State()
    space = _explorer_space(tmp_path)
    launcher.open_space(rt, space, state)
    hidden = state.spaces["demo"].windows[0].hwnd
    win.hide(hidden)

    report = launcher.open_space(rt, space, state)

    assert report.launched == [] and report.reused == ["explorer #1"]
    assert len(procs.spawned) == 1
    assert len(state.spaces["demo"].windows) == 1
    assert win.windows[hidden].visible  # спрятанное окно показано
    assert ("focus", hidden) in win.calls


def test_closed_window_is_relaunched_alone(tmp_path):
    rt, win, procs, clock = make_runtime(tmp_path)
    _arm(win, procs)
    state = state_mod.State()
    space = _explorer_space(tmp_path)
    launcher.open_space(rt, space, state)
    win.windows.pop(state.spaces["demo"].windows[0].hwnd)  # пользователь закрыл окно

    pruned = state_mod.prune(
        state, {w.hwnd: w for w in win.list_windows()}, lambda pid: procs.procs[pid].created if pid in procs.procs else None
    )
    report = launcher.open_space(rt, space, pruned)

    assert report.launched == ["explorer #1"] and len(procs.spawned) == 2


def test_window_never_appears_gives_warning_not_crash(tmp_path):
    rt, win, procs, clock = make_runtime(tmp_path, bind_timeout=2.0)
    state = state_mod.State()
    report = launcher.open_space(rt, _explorer_space(tmp_path), state)
    assert report.launched == []
    assert "не появилось" in report.warnings[0]
    assert state.spaces["demo"].windows == []


def test_app_resets_position_placement_retries(tmp_path):
    rt, win, procs, clock = make_runtime(tmp_path)
    procs.add(700, "explorer.exe")

    def spawn(argv):
        info = win.appear_later(0.5, win.make_window("explorer.exe", "CabinetWClass", 700))
        win.ignore_moves[info.hwnd] = 2  # приложение дважды возвращает свою позицию

    procs.on_spawn = spawn
    spec = WindowSpec(monitor=0, x=0, y=0, w=800, h=600)
    state = state_mod.State()
    report = launcher.open_space(rt, _explorer_space(tmp_path, spec), state)
    assert report.warnings == []
    hwnd = state.spaces["demo"].windows[0].hwnd
    assert win.windows[hwnd].frame_rect == Rect(0, 0, 800, 600)


def test_app_that_always_resets_position_warns(tmp_path):
    rt, win, procs, clock = make_runtime(tmp_path)
    procs.add(700, "explorer.exe")

    def spawn(argv):
        info = win.appear_later(0.5, win.make_window("explorer.exe", "CabinetWClass", 700))
        win.ignore_moves[info.hwnd] = 99

    procs.on_spawn = spawn
    state = state_mod.State()
    spec = WindowSpec(monitor=0, x=0, y=0, w=800, h=600)
    report = launcher.open_space(rt, _explorer_space(tmp_path, spec), state)
    assert report.launched == ["explorer #1"]
    assert "не встало" in report.warnings[0]


def test_elevated_window_is_marked_uncontrollable_and_not_moved(tmp_path):
    rt, win, procs, clock = make_runtime(tmp_path)
    _arm(win, procs)
    win.elevated.add(700)
    state = state_mod.State()
    spec = WindowSpec(monitor=0, x=0, y=0, w=800, h=600)
    report = launcher.open_space(rt, _explorer_space(tmp_path, spec), state)
    assert state.spaces["demo"].windows[0].uncontrollable
    assert "администратора" in report.warnings[0]
    assert not [c for c in win.calls if c[0] == "move"]


def test_one_failing_item_does_not_break_the_others(tmp_path):
    rt, win, procs, clock = make_runtime(tmp_path)
    _arm(win, procs)
    missing = tmp_path / "does-not-exist"
    space = Space(name="demo", items=(ExplorerItem(path=missing), ExplorerItem(path=tmp_path)))
    state = state_mod.State()
    report = launcher.open_space(rt, space, state)
    assert report.launched == ["explorer #2"]
    assert "папка не найдена" in report.warnings[0]


def test_space_env_is_passed_to_new_processes(tmp_path):
    rt, win, procs, clock = make_runtime(tmp_path)
    _arm(win, procs)
    space = Space(name="demo", env={"GIT_AUTHOR_EMAIL": "me@work.com"}, items=(ExplorerItem(path=tmp_path),))
    launcher.open_space(rt, space, state_mod.State())
    assert procs.spawned[0][1] == {"GIT_AUTHOR_EMAIL": "me@work.com", "WS_SPACE": "demo"}


def test_generic_app_loose_binding_takes_single_new_window(tmp_path):
    from ws.models import AppItem

    rt, win, procs, clock = make_runtime(tmp_path)
    exe = tmp_path / "launcher.exe"
    # лаунчер запускает окно из другого exe
    procs.add(701, "real-app.exe")
    procs.on_spawn = lambda argv: win.appear_later(0.5, win.make_window("real-app.exe", "Main", 701))
    space = Space(name="demo", items=(AppItem(exe=exe),))
    report = launcher.open_space(rt, space, state_mod.State())
    assert report.launched == ["app #1"]
