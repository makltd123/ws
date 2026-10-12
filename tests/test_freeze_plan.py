"""План заморозки: что замораживать можно, а что нельзя ни при каких условиях."""

from dataclasses import replace

from fakes import FakeClock, FakeProcessSystem, FakeWindowSystem

from ws.freezer import NEVER_FREEZE, plan
from ws.models import ProcRef, SpaceState, WindowRef


class World:
    def __init__(self):
        clock = FakeClock()
        self.win = FakeWindowSystem(clock)
        self.procs = FakeProcessSystem(clock)

    def window(self, exe, pid, cls="Chrome_WidgetWin_1", visible=True, **kw):
        info = self.win.add(replace(self.win.make_window(exe, cls, pid, visible=visible), title=exe, **kw))
        return info

    def ref(self, info, index=0):
        return WindowRef(info.hwnd, info.pid, self.procs.procs[info.pid].created, info.exe, info.title, index)

    def plan(self, sp, other=(), protected=(), excludes=()):
        windows = {w.hwnd: w for w in self.win.list_windows()}
        return plan(sp, windows, set(other), self.procs.tree, set(protected), set(excludes))


def test_vscode_like_tree_is_frozen_and_window_hidden():
    w = World()
    w.procs.add(10, "Code.exe")
    w.procs.add(11, "Code.exe", parent=10)
    w.procs.add(12, "Code.exe", parent=10)
    win = w.window("Code.exe", 10)
    w.window("Code.exe", 11, visible=False)  # служебное скрытое окно потомка не мешает
    sp = SpaceState(windows=[w.ref(win)])

    p = w.plan(sp)

    assert sorted(x.pid for x in p.suspend) == [10, 11, 12]
    assert p.roots[0].skip_reason is None and p.roots[0].windows[0].hwnd == win.hwnd


def test_never_freezes_the_chain_that_runs_ws():
    """ws switch запущен из терминала VS Code этого пространства: его процессы замораживать нельзя."""
    w = World()
    w.procs.add(10, "Code.exe")
    w.procs.add(11, "Code.exe", parent=10)  # pty host
    w.procs.add(12, "powershell.exe", parent=11)
    w.procs.add(13, "python.exe", parent=12)  # сам ws
    win = w.window("Code.exe", 10)
    sp = SpaceState(windows=[w.ref(win)])

    p = w.plan(sp, protected={13, 12, 11, 10, 1})

    assert p.suspend == []
    assert "изнутри" in p.roots[0].skip_reason
    assert p.roots[0].windows  # окно при этом всё равно будет спрятано


def test_foreign_visible_window_in_same_process_blocks_freezing():
    w = World()
    w.procs.add(10, "Code.exe")
    mine = w.window("Code.exe", 10)
    w.window("Code.exe", 10)  # ещё одно видимое окно того же процесса, не из пространства
    p = w.plan(SpaceState(windows=[w.ref(mine)]))
    assert p.suspend == [] and "вне этого пространства" in p.roots[0].skip_reason


def test_window_owned_by_other_space_blocks_freezing_even_if_hidden():
    w = World()
    w.procs.add(10, "Code.exe")
    mine = w.window("Code.exe", 10)
    theirs = w.window("Code.exe", 10, visible=False)  # окно замороженного соседнего пространства
    p = w.plan(SpaceState(windows=[w.ref(mine)]), other={theirs.hwnd})
    assert p.suspend == []


def test_foreign_window_in_a_child_process_blocks_the_whole_tree():
    w = World()
    w.procs.add(10, "chrome.exe")
    w.procs.add(11, "chrome.exe", parent=10)
    mine = w.window("chrome.exe", 10)
    w.window("chrome.exe", 11)  # окно потомка вне пространства
    assert w.plan(SpaceState(windows=[w.ref(mine)])).suspend == []


def test_system_and_shared_processes_are_hidden_but_never_suspended():
    w = World()
    w.procs.add(20, "explorer.exe")
    w.procs.add(21, "WindowsTerminal.exe")
    explorer = w.window("explorer.exe", 20, cls="CabinetWClass")
    terminal = w.window("WindowsTerminal.exe", 21, cls="CASCADIA_HOSTING_WINDOW_CLASS")
    sp = SpaceState(windows=[w.ref(explorer, 0), w.ref(terminal, 1)])
    p = w.plan(sp)
    assert p.suspend == []
    assert [r.windows[0].hwnd for r in p.roots] == [explorer.hwnd, terminal.hwnd]
    assert {"explorer.exe", "dwm.exe", "windowsterminal.exe", "applicationframehost.exe"} <= NEVER_FREEZE


def test_never_list_member_inside_a_tree_is_skipped_individually():
    w = World()
    w.procs.add(10, "app.exe")
    w.procs.add(11, "conhost.exe", parent=10)
    w.procs.add(12, "helper.exe", parent=10)
    win = w.window("app.exe", 10)
    p = w.plan(SpaceState(windows=[w.ref(win)]))
    assert sorted(x.pid for x in p.suspend) == [10, 12]


def test_excluded_app_is_minimized_not_frozen():
    w = World()
    w.procs.add(30, "Zoom.exe")
    w.procs.add(31, "Zoom.exe", parent=30)
    win = w.window("Zoom.exe", 30)
    p = w.plan(SpaceState(windows=[w.ref(win)]), excludes={"zoom.exe"})
    assert p.suspend == [] and p.roots[0].excluded


def test_terminal_shell_trees_are_frozen_instead_of_the_terminal_itself():
    w = World()
    w.procs.add(21, "WindowsTerminal.exe")
    w.procs.add(22, "OpenConsole.exe", parent=21)
    w.procs.add(23, "powershell.exe", parent=21)
    w.procs.add(24, "uvicorn.exe", parent=23)
    term = w.window("WindowsTerminal.exe", 21, cls="CASCADIA_HOSTING_WINDOW_CLASS")
    sp = SpaceState(windows=[w.ref(term)], shell_procs=[ProcRef(23, w.procs.procs[23].created)])
    p = w.plan(sp)
    assert sorted(x.pid for x in p.suspend) == [23, 24]


def test_shell_tab_that_runs_ws_is_not_frozen():
    w = World()
    w.procs.add(23, "powershell.exe")
    w.procs.add(24, "python.exe", parent=23)
    sp = SpaceState(shell_procs=[ProcRef(23, w.procs.procs[23].created)])
    p = w.plan(sp, protected={24, 23})
    assert p.suspend == [] and "изнутри" in p.roots[0].skip_reason


def test_dead_processes_and_uncontrollable_windows_are_ignored():
    w = World()
    w.procs.add(10, "admin.exe")
    admin = w.window("admin.exe", 10)
    ghost = w.window("ghost.exe", 99)  # процесса нет
    ref_admin = w.ref(admin)
    ref_admin.uncontrollable = True
    ref_ghost = WindowRef(ghost.hwnd, 99, 0.0, "ghost.exe", "t", 1)
    p = w.plan(SpaceState(windows=[ref_admin, ref_ghost]))
    assert p.suspend == []
    assert [r.pid for r in p.roots] == [99]  # окно администратора вообще не трогаем


def test_same_process_for_two_windows_is_planned_once():
    w = World()
    w.procs.add(10, "app.exe")
    a, b = w.window("app.exe", 10), w.window("app.exe", 10)
    p = w.plan(SpaceState(windows=[w.ref(a, 0), w.ref(b, 1)]))
    assert [x.pid for x in p.suspend] == [10] and len(p.roots) == 1 and len(p.roots[0].windows) == 2
