"""switch / freeze / thaw / close на фейках: полные сценарии со state.json."""

import json
from dataclasses import replace
from pathlib import Path

import pytest
from fakes import make_runtime

from ws import freezer, launcher, switcher
from ws import state as state_mod
from ws.models import ExplorerItem, ProcRef, Rect, Space, SpaceState, State, WindowRef


def _manifest(rt, name, exclude=()):
    items = "[" + ", ".join(exclude) + "]"
    (rt.paths.spaces / f"{name}.yml").write_text(f"name: {name}\nitems: []\nfreeze:\n  exclude: {items}\n", encoding="utf-8")


def _space(tmp_path, name, n_items=1):
    return Space(name=name, items=tuple(ExplorerItem(path=tmp_path) for _ in range(n_items)))


def _running(rt, win, procs, name, exe, pid, children=(), index=0, cls="Chrome_WidgetWin_1"):
    """Пространство уже запущено: окно, процесс и (опционально) потомки."""
    procs.add(pid, exe)
    for child in children:
        procs.add(child, exe, parent=pid)
    info = win.add(replace(win.make_window(exe, cls, pid), title=exe))
    ref = WindowRef(info.hwnd, pid, procs.procs[pid].created, exe, exe, index)
    state = rt_state(rt)
    sp = state.spaces.setdefault(name, SpaceState())
    sp.windows.append(ref)
    return info, state


_STATES: dict[int, State] = {}


def rt_state(rt) -> State:
    return _STATES.setdefault(id(rt), State())


@pytest.fixture(autouse=True)
def _reset():
    _STATES.clear()


def _arm_explorer(win, procs, pid=700):
    procs.add(pid, "explorer.exe")
    procs.on_spawn = lambda argv: win.appear_later(0.5, win.make_window("explorer.exe", "CabinetWClass", pid))


def test_switch_freezes_active_and_opens_target(tmp_path):
    rt, win, procs, clock = make_runtime(tmp_path)
    _manifest(rt, "a")
    a_win, state = _running(rt, win, procs, "a", "app.exe", 10, children=(11,))
    state.active = "a"
    _arm_explorer(win, procs)

    report = switcher.switch(rt, _space(tmp_path, "b"), state)

    assert report.all_warnings == []
    assert procs.suspended == {10, 11}
    assert not win.windows[a_win.hwnd].visible
    sa = state.spaces["a"]
    assert sa.frozen and {p.pid for p in sa.frozen_procs} == {10, 11}
    assert sa.windows[0].last_rect == a_win.frame_rect
    assert report.opened.launched == ["explorer #1"]
    assert state.active == "b" and not state.spaces["b"].frozen
    # состояние на диске совпадает: после падения ws thaw знал бы, что возвращать
    on_disk = state_mod.load(rt.paths)
    assert on_disk.spaces["a"].frozen and len(on_disk.spaces["a"].frozen_procs) == 2


def test_switch_back_resumes_without_restarting(tmp_path):
    rt, win, procs, clock = make_runtime(tmp_path)
    _manifest(rt, "a")
    a_win, state = _running(rt, win, procs, "a", "app.exe", 10, children=(11,))
    state.active = "a"
    _arm_explorer(win, procs)
    switcher.switch(rt, _space(tmp_path, "b"), state)
    spawned = len(procs.spawned)

    report = switcher.switch(rt, _space(tmp_path, "a"), state)

    assert procs.suspended == set()
    assert win.windows[a_win.hwnd].visible
    assert ("focus", a_win.hwnd) in win.calls
    assert report.thawed.resumed == 2 and report.opened.launched == []
    assert len(procs.spawned) == spawned  # приложения не перезапускались
    assert not state.spaces["a"].frozen and state.spaces["a"].frozen_procs == []
    assert state.spaces["b"].frozen  # b ушёл в заморозку: его окно спрятано
    assert state.active == "a"


def test_switch_to_the_active_space_does_not_freeze_it(tmp_path):
    rt, win, procs, clock = make_runtime(tmp_path)
    a_win, state = _running(rt, win, procs, "a", "app.exe", 10)
    state.active = "a"
    report = switcher.switch(rt, _space(tmp_path, "a"), state)
    assert report.frozen is None and procs.suspended == set() and win.windows[a_win.hwnd].visible


def test_process_that_fails_to_freeze_gives_warning_and_switch_continues(tmp_path):
    rt, win, procs, clock = make_runtime(tmp_path)
    _manifest(rt, "a")
    a_win, state = _running(rt, win, procs, "a", "app.exe", 10, children=(11,))
    state.active = "a"
    procs.suspend_refuses.add(11)
    _arm_explorer(win, procs)

    report = switcher.switch(rt, _space(tmp_path, "b"), state)

    assert procs.suspended == {10}
    assert any("pid 11" in w and "не удалось заморозить" in w for w in report.all_warnings)
    assert state.active == "b" and report.opened.launched  # переключение состоялось
    assert [p.pid for p in state.spaces["a"].frozen_procs] == [10]


def test_window_that_cannot_be_hidden_keeps_its_process_running(tmp_path):
    """Замороженное видимое окно выглядело бы как «Не отвечает»: такой процесс не трогаем."""
    rt, win, procs, clock = make_runtime(tmp_path)
    _manifest(rt, "a")
    a_win, state = _running(rt, win, procs, "a", "app.exe", 10)
    state.active = "a"
    win.hide = lambda hwnd: False
    _arm_explorer(win, procs)

    report = switcher.switch(rt, _space(tmp_path, "b"), state)

    assert procs.suspended == set()
    assert any("не удалось спрятать" in w for w in report.all_warnings)


def test_intent_is_saved_before_the_first_process_is_suspended(tmp_path):
    rt, win, procs, clock = make_runtime(tmp_path)
    a_win, state = _running(rt, win, procs, "a", "app.exe", 10, children=(11,))
    seen = {}

    def crashing_suspend(ref: ProcRef) -> bool:
        seen["disk"] = json.loads(rt.paths.state_file.read_text(encoding="utf-8"))
        raise RuntimeError("ws упал посередине")

    procs.suspend = crashing_suspend
    with pytest.raises(RuntimeError):
        freezer.freeze(rt, "a", state)

    saved = seen["disk"]["spaces"]["a"]
    assert saved["frozen"] is True
    assert {p["pid"] for p in saved["frozen_procs"]} == {10, 11}  # thaw после сбоя знает, что возвращать
    assert not win.windows[a_win.hwnd].visible  # окна к моменту заморозки уже спрятаны


def test_freeze_exclude_from_manifest_minimizes_instead_of_freezing(tmp_path):
    rt, win, procs, clock = make_runtime(tmp_path)
    _manifest(rt, "a", exclude=["Zoom.exe"])
    zoom, state = _running(rt, win, procs, "a", "Zoom.exe", 30)
    other, _ = _running(rt, win, procs, "a", "app.exe", 10, index=1)
    state.active = "a"
    _arm_explorer(win, procs)

    switcher.switch(rt, _space(tmp_path, "b"), state)

    assert win.windows[zoom.hwnd].minimized and win.windows[zoom.hwnd].visible
    assert not win.windows[other.hwnd].visible
    assert procs.suspended == {10}


def test_global_exclude_from_config(tmp_path):
    rt, win, procs, clock = make_runtime(tmp_path, freeze_exclude=frozenset({"spotify.exe"}))
    _manifest(rt, "a")
    spot, state = _running(rt, win, procs, "a", "Spotify.exe", 40)
    state.active = "a"
    _arm_explorer(win, procs)
    switcher.switch(rt, _space(tmp_path, "b"), state)
    assert procs.suspended == set() and win.windows[spot.hwnd].minimized


def test_broken_manifest_of_current_space_does_not_block_freezing(tmp_path):
    rt, win, procs, clock = make_runtime(tmp_path)
    (rt.paths.spaces / "a.yml").write_text("name: [", encoding="utf-8")
    a_win, state = _running(rt, win, procs, "a", "app.exe", 10)
    state.active = "a"
    _arm_explorer(win, procs)
    report = switcher.switch(rt, _space(tmp_path, "b"), state)
    assert procs.suspended == {10}
    assert any("манифест 'a' не прочитан" in w for w in report.all_warnings)


def test_ws_never_freezes_the_app_it_runs_inside(tmp_path):
    rt, win, procs, clock = make_runtime(tmp_path)
    _manifest(rt, "a")
    procs.add(1, "python.exe")  # fake.self_and_ancestors() == {1}
    a_win, state = _running(rt, win, procs, "a", "app.exe", 10, children=(11,))
    procs.procs[1] = replace(procs.procs[1], parent=11)
    procs.self_and_ancestors = lambda: {1, 11, 10}
    state.active = "a"
    _arm_explorer(win, procs)

    report = switcher.switch(rt, _space(tmp_path, "b"), state)

    assert procs.suspended == set()
    assert not win.windows[a_win.hwnd].visible  # окно спрятано, но процесс с ws внутри работает
    assert any("изнутри" in w for w in report.all_warnings)
    assert state.active == "b"


def test_thaw_moves_offscreen_window_back_to_a_visible_monitor(tmp_path):
    rt, win, procs, clock = make_runtime(tmp_path)
    _manifest(rt, "a")
    a_win, state = _running(rt, win, procs, "a", "app.exe", 10)
    a_win = win.windows[a_win.hwnd] = replace(a_win, rect=Rect(2000, 100, 2800, 700), frame_rect=Rect(2007, 100, 2793, 693))
    state.active = "a"
    _arm_explorer(win, procs)
    switcher.switch(rt, _space(tmp_path, "b"), state)
    win._monitors = win._monitors[:1]  # второй монитор отключили, пока пространство спало

    switcher.switch(rt, _space(tmp_path, "a"), state)

    frame = win.windows[a_win.hwnd].frame_rect
    assert frame.right <= 1920 and frame.left >= 0 and frame.width == 786


def test_open_thaws_a_frozen_space(tmp_path):
    rt, win, procs, clock = make_runtime(tmp_path)
    a_win, state = _running(rt, win, procs, "a", "app.exe", 10)
    freezer.freeze(rt, "a", state)
    assert procs.suspended == {10}
    report = switcher.activate(rt, _space(tmp_path, "a"), state)
    assert procs.suspended == set() and report.thawed is not None and win.windows[a_win.hwnd].visible


def test_thaw_of_not_frozen_space_is_harmless(tmp_path):
    rt, win, procs, clock = make_runtime(tmp_path)
    a_win, state = _running(rt, win, procs, "a", "app.exe", 10)
    report = freezer.thaw(rt, "a", state)
    assert "не заморожено" in report.warnings[0] and procs.suspended == set()


def test_thaw_resumes_processes_even_if_windows_are_gone(tmp_path):
    rt, win, procs, clock = make_runtime(tmp_path)
    a_win, state = _running(rt, win, procs, "a", "app.exe", 10)
    freezer.freeze(rt, "a", state)
    state.spaces["a"].windows.clear()  # окна исчезли, процесс остался замороженным
    freezer.thaw(rt, "a", state)
    assert procs.suspended == set()


def test_double_freeze_is_idempotent(tmp_path):
    rt, win, procs, clock = make_runtime(tmp_path)
    a_win, state = _running(rt, win, procs, "a", "app.exe", 10)
    freezer.freeze(rt, "a", state)
    again = freezer.freeze(rt, "a", state)
    assert "уже заморожено" in again.warnings[0] and procs.suspended == {10}


def test_two_spaces_never_freeze_each_others_processes(tmp_path):
    rt, win, procs, clock = make_runtime(tmp_path)
    a_win, state = _running(rt, win, procs, "a", "app.exe", 10)
    b_win, _ = _running(rt, win, procs, "b", "app.exe", 20)
    freezer.freeze(rt, "a", state)
    assert procs.suspended == {10} and win.windows[b_win.hwnd].visible


# --- терминал ---------------------------------------------------------------


def test_launching_a_terminal_records_tab_shells(tmp_path):
    from ws.models import TerminalItem, TerminalTab

    rt, win, procs, clock = make_runtime(tmp_path)
    procs.add(21, "WindowsTerminal.exe")
    procs.add(5, "powershell.exe", parent=21)  # вкладка, открытая до запуска пространства, не наша
    procs.add(6, "pwsh.exe", parent=99)  # чужой процесс вне терминала

    def spawn(argv):
        win.appear_later(0.5, win.make_window("WindowsTerminal.exe", "CASCADIA_HOSTING_WINDOW_CLASS", 21))
        for pid in (30, 32):  # реальная структура WT 1.24: OpenConsole и оболочка рядом, под WindowsTerminal.exe
            procs.add(pid - 1, "OpenConsole.exe", parent=21)
            procs.add(pid, "powershell.exe", parent=21)

    procs.on_spawn = spawn
    space = Space(name="t", items=(TerminalItem(tabs=(TerminalTab(tmp_path), TerminalTab(tmp_path))),))
    state = State()
    launcher.open_space(rt, space, state)

    shells = state.spaces["t"].shell_procs
    assert sorted(p.pid for p in shells) == [30, 32]
    assert state_mod.load(rt.paths).spaces["t"].shell_procs == shells


# --- close -----------------------------------------------------------------


def test_close_closes_windows_and_clears_state(tmp_path):
    rt, win, procs, clock = make_runtime(tmp_path)
    a_win, state = _running(rt, win, procs, "a", "app.exe", 10)
    state.active = "a"
    report = switcher.close(rt, "a", state)
    assert report.closed == 1 and report.not_closed == []
    assert "a" not in state.spaces and state.active is None and a_win.hwnd not in win.windows


def test_close_thaws_a_frozen_space_first(tmp_path):
    rt, win, procs, clock = make_runtime(tmp_path)
    a_win, state = _running(rt, win, procs, "a", "app.exe", 10)
    freezer.freeze(rt, "a", state)
    switcher.close(rt, "a", state)
    assert procs.suspended == set() and a_win.hwnd not in win.windows


def test_close_reports_windows_that_refuse_and_never_kills_without_force(tmp_path):
    rt, win, procs, clock = make_runtime(tmp_path)
    a_win, state = _running(rt, win, procs, "a", "app.exe", 10)
    win.stubborn.add(a_win.hwnd)
    report = switcher.close(rt, "a", state, timeout=1.0)
    assert report.closed == 0 and "не закрылось" in report.not_closed[0]
    assert procs.terminated == [] and a_win.hwnd in win.windows
    assert len(state.spaces["a"].windows) == 1  # окно остаётся привязанным


def test_close_force_asks_before_terminating(tmp_path):
    rt, win, procs, clock = make_runtime(tmp_path)
    a_win, state = _running(rt, win, procs, "a", "app.exe", 10, children=(11,))
    win.stubborn.add(a_win.hwnd)
    asked = []

    def confirm(labels):
        asked.append(labels)
        return False

    report = switcher.close(rt, "a", state, confirm_kill=confirm, timeout=1.0)
    assert asked and any("pid 10" in label for label in asked[0])
    assert procs.terminated == [] and report.terminated == []


def test_close_force_terminates_after_confirmation(tmp_path):
    rt, win, procs, clock = make_runtime(tmp_path)
    a_win, state = _running(rt, win, procs, "a", "app.exe", 10, children=(11,))
    win.stubborn.add(a_win.hwnd)

    def kill_window_with_process(ref):  # завершение процесса закрывает его окно
        win.windows.pop(a_win.hwnd, None)

    real = procs.terminate
    procs.terminate = lambda ref: (kill_window_with_process(ref), real(ref))[1]
    report = switcher.close(rt, "a", state, confirm_kill=lambda labels: True, timeout=1.0)

    assert procs.terminated == [11, 10]  # потомки раньше корня
    assert len(report.terminated) == 2 and "a" not in state.spaces


def test_close_force_never_terminates_protected_or_shared_processes(tmp_path):
    rt, win, procs, clock = make_runtime(tmp_path)
    a_win, state = _running(rt, win, procs, "a", "app.exe", 10)
    win.stubborn.add(a_win.hwnd)
    procs.self_and_ancestors = lambda: {10}  # ws запущен внутри этого приложения
    asked = []
    report = switcher.close(rt, "a", state, confirm_kill=lambda labels: asked.append(labels) or True, timeout=1.0)
    assert asked == [] and procs.terminated == []
    assert any("изнутри" in w for w in report.warnings)


def test_close_skips_elevated_windows_with_explanation(tmp_path):
    rt, win, procs, clock = make_runtime(tmp_path)
    a_win, state = _running(rt, win, procs, "a", "admin.exe", 10)
    state.spaces["a"].windows[0].uncontrollable = True
    report = switcher.close(rt, "a", state)
    assert "администратора" in report.not_closed[0] and a_win.hwnd in win.windows
    assert ("close", a_win.hwnd) not in win.calls
