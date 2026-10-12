"""ws save на фейках: что снимается, как пишется манифест и что происходит со state."""

from dataclasses import replace
from pathlib import Path

import pytest
from fakes import make_runtime

from ws import launcher, manifest, snapshot
from ws import state as state_mod
from ws.adapters.vscode import folder_from_cmdline
from ws.errors import UserError
from ws.models import (
    AppItem,
    BrowserItem,
    ExplorerItem,
    Rect,
    State,
    TerminalItem,
    VSCodeItem,
    WindowSpec,
)

CODE = r"C:\Apps\Code.exe"


def _win(win, exe, cls, pid, rect=None, title=None, **changes):
    info = win.make_window(exe, cls, pid, rect=rect)
    return win.add(replace(info, title=title or exe, **changes))


def _desktop(tmp_path):
    """Рабочий стол: проводник, VS Code (запущен через ws), терминал, Chrome (профиль ws), Obsidian и шум."""
    rt, win, procs, clock = make_runtime(tmp_path)
    proj = tmp_path / "proj"
    proj.mkdir()
    procs.add(10, "Code.exe", path=CODE, cmdline=[CODE, "--new-window", "--user-data-dir", "X", "--extensions-dir", "Y", str(proj)])
    procs.add(11, "WindowsTerminal.exe", path=r"C:\wt\WindowsTerminal.exe")
    profile = rt.paths.profiles / "work"
    procs.add(12, "chrome.exe", path=r"C:\Chrome\chrome.exe", cmdline=[r"C:\Chrome\chrome.exe", f"--user-data-dir={profile}"])
    procs.add(13, "Obsidian.exe", path=r"C:\Apps\Obsidian.exe", cmdline=[r"C:\Apps\Obsidian.exe", "--token=SECRET"])
    procs.add(14, "explorer.exe", path=r"C:\Windows\explorer.exe")
    procs.add(15, "Taskmgr.exe", path=r"C:\Windows\System32\Taskmgr.exe")

    wins = {
        "code": _win(win, "Code.exe", "Chrome_WidgetWin_1", 10, Rect(0, 0, 1294, 1047), "api - Visual Studio Code"),
        "wt": _win(win, "WindowsTerminal.exe", "CASCADIA_HOSTING_WINDOW_CLASS", 11, Rect(1273, 0, 1927, 527)),
        "chrome": _win(win, "chrome.exe", "Chrome_WidgetWin_1", 12, Rect(1273, 520, 1927, 1047), "Docs"),
        "obsidian": _win(win, "Obsidian.exe", "Chrome_WidgetWin_1", 13, Rect(100, 100, 800, 600), "notes"),
        "explorer": _win(win, "explorer.exe", "CabinetWClass", 14, Rect(200, 200, 900, 700), "docs"),
    }
    win.explorer_folder_map[wins["explorer"].hwnd] = proj
    # шум, который не должен попасть в манифест
    _win(win, "explorer.exe", "Shell_TrayWnd", 14, Rect(0, 1040, 1920, 1080), "")  # без заголовка
    _win(win, "explorer.exe", "Progman", 14, Rect(0, 0, 1920, 1080), "Program Manager")
    _win(win, "ApplicationFrameHost.exe", "ApplicationFrameWindow", 16, title="Параметры")
    _win(win, "Taskmgr.exe", "TaskManagerWindow", 15, title="Диспетчер задач", cloaked=True)
    return rt, win, procs, wins, proj


def test_folder_from_cmdline(tmp_path):
    folder = tmp_path / "p"
    folder.mkdir()
    assert folder_from_cmdline((CODE, "--new-window", "--user-data-dir", "X", "--extensions-dir", str(folder), str(folder))) == folder
    assert folder_from_cmdline((CODE,)) is None
    assert folder_from_cmdline((CODE, "--unknown-flag", str(tmp_path / "missing"))) is None
    # путь, лежащий сразу после --user-data-dir, папкой проекта не считается
    assert folder_from_cmdline((CODE, "--user-data-dir", str(folder))) is None


def test_save_captures_each_app_type(tmp_path):
    rt, win, procs, wins, proj = _desktop(tmp_path)
    state = State()

    report = snapshot.save_space(rt, "snap", state)

    space = manifest.load(rt.paths, "snap")
    by_type = {item.type: item for item in space.items}
    assert isinstance(by_type["vscode"], VSCodeItem) and by_type["vscode"].path == proj and by_type["vscode"].isolate
    assert isinstance(by_type["terminal"], TerminalItem) and by_type["terminal"].tabs == ()
    assert isinstance(by_type["browser"], BrowserItem) and by_type["browser"].profile == "work"
    assert by_type["browser"].browser == "chrome"
    assert isinstance(by_type["explorer"], ExplorerItem) and by_type["explorer"].path == proj
    assert isinstance(by_type["app"], AppItem) and by_type["app"].exe == Path(r"C:\Apps\Obsidian.exe")
    assert by_type["app"].args == ()  # аргументы (в них бывают токены) не сохраняются
    assert "SECRET" not in rt.paths.spaces.joinpath("snap.yml").read_text(encoding="utf-8")
    assert len(space.items) == 5  # панель задач, рабочий стол, UWP и скрытые окна не попали
    assert report.skipped == []


def test_save_positions_are_visible_frame_in_monitor_coordinates(tmp_path):
    rt, win, procs, wins, proj = _desktop(tmp_path)
    snapshot.save_space(rt, "snap", State())
    space = manifest.load(rt.paths, "snap")
    code = next(i for i in space.items if i.type == "vscode")
    # outer 0,0,1294,1047 минус невидимая рамка 7/0/7/7 = видимые 7,0..1287,1040
    assert code.window == WindowSpec(monitor=0, x=7, y=0, w=1280, h=1040)
    assert [i.window.x for i in space.items] == sorted(i.window.x for i in space.items)  # слева направо


def test_save_binds_windows_so_open_does_not_duplicate(tmp_path):
    rt, win, procs, wins, proj = _desktop(tmp_path)
    state = State()
    snapshot.save_space(rt, "snap", state)

    refs = state.spaces["snap"].windows
    assert {r.hwnd for r in refs} == {w.hwnd for w in wins.values()}
    assert state.active == "snap"
    space = manifest.load(rt.paths, "snap")
    assert sorted(r.item_index for r in refs) == list(range(len(space.items)))

    report = launcher.open_space(rt, space, state)
    assert report.launched == [] and len(report.reused) == 5
    assert procs.spawned == []


def test_save_roundtrip_acceptance_close_everything_then_open(tmp_path, monkeypatch):
    """Приёмка этапа 2: после save и закрытия всего open восстанавливает ту же раскладку."""
    rt, win, procs, wins, proj = _desktop(tmp_path)
    (tmp_path / "ob").mkdir()
    state = State()
    snapshot.save_space(rt, "snap", state)
    space = manifest.load(rt.paths, "snap")
    before = {i.type: win.windows[next(r.hwnd for r in state.spaces["snap"].windows if r.item_index == n)].frame_rect
              for n, i in enumerate(space.items)}

    for hwnd in list(win.windows):  # «закрыли всё»
        win.windows.pop(hwnd)
    state = State()
    # запуск каждого адаптера «рождает» окно того же типа в произвольном месте
    shells = {
        "vscode": ("Code.exe", "Chrome_WidgetWin_1", 10),
        "terminal": ("WindowsTerminal.exe", "CASCADIA_HOSTING_WINDOW_CLASS", 11),
        "browser": ("chrome.exe", "Chrome_WidgetWin_1", 12),
        "explorer": ("explorer.exe", "CabinetWClass", 14),
        "app": ("Obsidian.exe", "Chrome_WidgetWin_1", 13),
    }
    order = iter(space.items)

    def on_spawn(argv):
        item = next(order)
        exe, cls, pid = shells[item.type]
        win.appear_later(0.3, win.make_window(exe, cls, pid, rect=Rect(300, 300, 700, 600)))

    procs.on_spawn = on_spawn
    # вместо реальных путей запуска подменяем exe/папки на существующие заглушки
    monkeypatch.setattr("ws.adapters.vscode.find_code_exe", lambda: Path(CODE))
    monkeypatch.setattr("ws.adapters.terminal.shutil.which", lambda name: r"C:\wt\wt.exe")
    monkeypatch.setattr("ws.adapters.browser.find_browser", lambda browser: Path(r"C:\Chrome\chrome.exe"))
    space = replace(space, items=tuple(
        replace(i, exe=tmp_path / "ob" / "x.exe") if isinstance(i, AppItem) else i for i in space.items))

    report = launcher.open_space(rt, space, state)

    assert report.warnings == [] and len(report.launched) == 5
    after = {i.type: win.windows[next(r.hwnd for r in state.spaces["snap"].windows if r.item_index == n)].frame_rect
             for n, i in enumerate(space.items)}
    assert after == before


def test_vscode_without_folder_is_commented_out_with_position(tmp_path):
    rt, win, procs, clock = make_runtime(tmp_path)
    procs.add(10, "Code.exe", path=CODE, cmdline=[CODE])  # обычный VS Code, папка в командной строке не видна
    _win(win, "Code.exe", "Chrome_WidgetWin_1", 10, Rect(0, 0, 1294, 1047), "main.py - proj - Visual Studio Code")
    state = State()

    report = snapshot.save_space(rt, "snap", state)

    text = rt.paths.spaces.joinpath("snap.yml").read_text(encoding="utf-8")
    assert "# - type: vscode" in text and "#   path: TODO" in text and "#   isolate: false" in text
    assert "window: {monitor: 0, x: 7, y: 0, w: 1280, h: 1040}" in text
    assert "main.py - proj - Visual Studio Code" in text
    assert manifest.load(rt.paths, "snap").items == ()  # `ws open` не споткнётся
    assert len(report.commented) == 1 and report.captured == []
    assert state.spaces["snap"].windows == []  # неописанное окно к пространству не привязано


def test_browser_with_foreign_user_data_dir_is_described_without_profile(tmp_path):
    rt, win, procs, clock = make_runtime(tmp_path)
    procs.add(12, "msedge.exe", path=r"C:\Edge\msedge.exe", cmdline=["msedge.exe", r"--user-data-dir=D:\elsewhere"])
    _win(win, "msedge.exe", "Chrome_WidgetWin_1", 12, title="Bing")
    snapshot.save_space(rt, "snap", State())
    item = manifest.load(rt.paths, "snap").items[0]
    assert isinstance(item, BrowserItem) and item.browser == "edge" and item.profile is None
    assert r"D:\elsewhere" in rt.paths.spaces.joinpath("snap.yml").read_text(encoding="utf-8")


def test_skip_reasons_are_reported(tmp_path):
    rt, win, procs, clock = make_runtime(tmp_path)
    procs.add(20, "a.exe", path=r"C:\a.exe")
    procs.add(21, "b.exe", path=r"C:\b.exe")
    procs.add(22, "c.exe", path=r"C:\c.exe")
    minimized = _win(win, "a.exe", "A", 20, title="свёрнутое", minimized=True)
    elevated = _win(win, "b.exe", "B", 21, title="admin")
    win.elevated.add(21)
    owned = _win(win, "c.exe", "C", 22, title="чужое")
    _win(win, "x.exe", "X", 23, title="без процесса")  # info() вернёт None
    state = State()
    state.spaces["other"] = state_mod.SpaceState(windows=[state_mod.WindowRef(owned.hwnd, 22, 0.0, "c.exe", "t", 0)])

    report = snapshot.save_space(rt, "snap", state)

    reasons = " | ".join(report.skipped)
    assert "свёрнуто" in reasons and "от администратора" in reasons
    assert "другому пространству" in reasons and "не удалось определить путь" in reasons
    assert len(report.skipped) == 4 and report.captured == []
    assert minimized.hwnd and elevated.hwnd  # окна на месте, ничего не двигалось
    assert not [c for c in win.calls if c[0] in ("move", "hide", "close")]


def test_existing_manifest_needs_force_and_gets_backup(tmp_path):
    rt, win, procs, clock = make_runtime(tmp_path)
    procs.add(20, "a.exe", path=r"C:\a.exe")
    _win(win, "a.exe", "A", 20, title="окно")
    (rt.paths.spaces / "snap.yml").write_text("name: snap\nitems: []\n", encoding="utf-8")

    with pytest.raises(UserError, match="--force"):
        snapshot.save_space(rt, "snap", State())
    assert (rt.paths.spaces / "snap.yml").read_text(encoding="utf-8") == "name: snap\nitems: []\n"

    report = snapshot.save_space(rt, "snap", State(), force=True)
    assert report.backup is not None and report.backup.read_text(encoding="utf-8") == "name: snap\nitems: []\n"
    assert len(manifest.load(rt.paths, "snap").items) == 1


def test_force_resave_recaptures_windows_of_the_same_space(tmp_path):
    rt, win, procs, clock = make_runtime(tmp_path)
    procs.add(20, "a.exe", path=r"C:\a.exe")
    w = _win(win, "a.exe", "A", 20, title="окно")
    state = State()
    snapshot.save_space(rt, "snap", state)
    report = snapshot.save_space(rt, "snap", state, force=True)
    assert len(report.captured) == 1 and report.skipped == []
    assert [r.hwnd for r in state.spaces["snap"].windows] == [w.hwnd]


def test_bad_name_is_rejected_before_anything_is_written(tmp_path):
    rt, *_ = make_runtime(tmp_path)
    with pytest.raises(UserError):
        snapshot.save_space(rt, "../evil", State())


def test_include_filter_limits_what_is_captured(tmp_path):
    rt, win, procs, wins, proj = _desktop(tmp_path)
    snapshot.save_space(rt, "snap", State(), include=lambda w: w.exe == "chrome.exe")
    assert [i.type for i in manifest.load(rt.paths, "snap").items] == ["browser"]


@pytest.mark.parametrize(
    "value",
    [r"C:\proj\api", r"C:\Мои проекты\api", r"D:\a: b", r"C:\x #1", "true", "123", r"C:\Program Files (x86)\App", "a'b"],
)
def test_scalar_roundtrips_through_yaml(value):
    import yaml

    assert yaml.safe_load("k: " + snapshot._scalar(value)) == {"k": value}


def test_self_check_blocks_a_manifest_that_would_read_back_differently(tmp_path):
    from ws.errors import SystemFailure

    item = ExplorerItem(path=Path(r"C:\x"))
    with pytest.raises(SystemFailure, match="не записан"):
        snapshot._self_check("snap", "name: snap\nitems: []\n", [item])
