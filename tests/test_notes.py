"""Заметки «где я остановился»: чтение файлов хука, снимок при уходе, текст при возврате."""

import json
from dataclasses import replace

from fakes import make_runtime

from ws import freezer, notes
from ws import state as state_mod
from ws.config import Paths
from ws.models import Rect, SpaceState, State, WindowRef


def _shell_file(paths, space, pid, **fields):
    folder = paths.notes / space
    folder.mkdir(parents=True, exist_ok=True)
    data = {"pid": pid, "cwd": r"C:\proj\api", "command": "pytest -q", "time": 1000, **fields}
    (folder / f"{pid}.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8-sig")  # PS 5.1 пишет BOM


def test_shell_notes_are_sorted_and_dead_shells_are_cleaned(tmp_path):
    paths = Paths(tmp_path)
    paths.ensure()
    _shell_file(paths, "a", 1, time=100, command="old")
    _shell_file(paths, "a", 2, time=300, command="newest")
    _shell_file(paths, "a", 3, time=200, command="dead")
    (paths.notes / "a" / "4.json").write_text("{не json", encoding="utf-8")
    (paths.notes / "a" / "5.json").write_text('{"cwd": "x"}', encoding="utf-8")  # нет pid

    result = notes.read_shells(paths, "a", alive=lambda pid: pid != 3)

    assert [s.command for s in result] == ["newest", "old"]
    assert not (paths.notes / "a" / "3.json").exists()  # файл мёртвой оболочки убран
    assert (paths.notes / "a" / "4.json").exists()  # битый не трогаем: хук его перезапишет


def test_missing_folder_is_not_an_error(tmp_path):
    paths = Paths(tmp_path)
    paths.ensure()
    assert notes.read_shells(paths, "nothing", alive=lambda pid: True) == []
    assert notes.load(paths, "nothing") is None


def test_empty_fields_become_none(tmp_path):
    paths = Paths(tmp_path)
    paths.ensure()
    _shell_file(paths, "a", 1, command="  ", cwd=None)
    (shell,) = notes.read_shells(paths, "a", alive=lambda pid: True)
    assert shell.command is None and shell.cwd is None


def test_note_roundtrip(tmp_path):
    paths = Paths(tmp_path)
    paths.ensure()
    note = notes.Note(10.0, "title", "Code.exe", [notes.ShellNote(5, r"C:\x", "ls", 9.0)])
    notes.save(paths, "a", note)
    assert notes.load(paths, "a") == note
    paths.notes.joinpath("a.json").write_text("мусор", encoding="utf-8")
    assert notes.load(paths, "a") is None


def test_ago_wording():
    assert notes.ago(5) == "только что"
    assert notes.ago(5 * 60) == "5 мин назад"
    assert notes.ago(3 * 3600 + 5) == "3 ч назад"
    assert notes.ago(2 * 86400) == "2 дн назад"
    assert notes.ago(-10) == "только что"


def test_describe_uses_window_and_last_command():
    note = notes.Note(1000.0, "api - Visual Studio Code", "Code.exe", [notes.ShellNote(1, r"C:\proj\api", "pytest -q", 1.0)])
    text = notes.describe(note, 1000.0 + 12 * 60)
    assert text == "Ты ушёл отсюда 12 мин назад. Последнее окно: «api - Visual Studio Code». Последняя команда: pytest -q (в C:\\proj\\api)."


def test_describe_prefers_a_shell_that_actually_ran_a_command():
    shells = [notes.ShellNote(1, r"C:\a", None, 9.0), notes.ShellNote(2, r"C:\b", "make", 5.0)]
    assert "make" in notes.describe(notes.Note(0.0, None, None, shells), 60.0)
    only_dir = notes.describe(notes.Note(0.0, None, None, [notes.ShellNote(1, r"C:\a", None, 9.0)]), 60.0)
    assert "Терминал был в C:\\a" in only_dir
    assert notes.describe(notes.Note(0.0), 5.0) == "Ты ушёл отсюда только что."


def test_long_texts_are_trimmed():
    long = "x" * 500
    text = notes.describe(notes.Note(0.0, long, None, [notes.ShellNote(1, None, long, 1.0)]), 60.0)
    assert len(text) < 250


# --- заморозка пишет заметку, разморозка её показывает ----------------------


def _running(rt, win, procs, name="a", exe="app.exe", pid=10, title="main.py"):
    procs.add(pid, exe)
    info = win.add(replace(win.make_window(exe, "Chrome_WidgetWin_1", pid), title=title))
    state = State()
    state.spaces[name] = SpaceState(windows=[WindowRef(info.hwnd, pid, procs.procs[pid].created, exe, title, 0)])
    return info, state


def test_freeze_saves_a_note_and_thaw_describes_it(tmp_path):
    rt, win, procs, clock = make_runtime(tmp_path)
    info, state = _running(rt, win, procs, title="main.py - proj")
    _shell_file(rt.paths, "a", 10, command="uvicorn app:app", time=clock.now())  # pid 10 жив (процесс окна)

    freezer.freeze(rt, "a", state)
    saved = notes.load(rt.paths, "a")
    assert saved.window_title == "main.py - proj" and saved.left_at == clock.now()
    assert [s.command for s in saved.shells] == ["uvicorn app:app"]

    clock.sleep(15 * 60)
    report = freezer.thaw(rt, "a", state)
    assert report.note == "Ты ушёл отсюда 15 мин назад. Последнее окно: «main.py - proj». Последняя команда: uvicorn app:app (в C:\\proj\\api)."


def test_top_visible_window_of_the_space_is_the_one_reported(tmp_path):
    rt, win, procs, clock = make_runtime(tmp_path)
    procs.add(10, "app.exe")
    other = win.add(replace(win.make_window("notepad.exe", "Notepad", 99), title="чужое окно"))
    first = win.add(replace(win.make_window("app.exe", "A", 10), title="верхнее"))
    second = win.add(replace(win.make_window("app.exe", "A", 10), title="нижнее"))
    refs = [WindowRef(w.hwnd, 10, procs.procs[10].created, "app.exe", w.title, i) for i, w in enumerate((first, second))]
    sp = SpaceState(windows=refs)
    note = notes.capture(rt.paths, "a", sp, [other, first, second], 5.0, lambda pid: True)
    assert note.window_title == "верхнее" and other.title not in str(note)


def test_minimized_and_hidden_windows_are_not_the_top_one(tmp_path):
    rt, win, procs, clock = make_runtime(tmp_path)
    procs.add(10, "app.exe")
    hidden = win.add(replace(win.make_window("app.exe", "A", 10, visible=False), title="скрытое"))
    mini = win.add(replace(win.make_window("app.exe", "A", 10), title="свёрнутое", minimized=True))
    shown = win.add(replace(win.make_window("app.exe", "A", 10), title="видимое"))
    sp = SpaceState(windows=[WindowRef(w.hwnd, 10, 0.0, "app.exe", w.title, i) for i, w in enumerate((hidden, mini, shown))])
    assert notes.capture(rt.paths, "a", sp, [hidden, mini, shown], 5.0, lambda pid: True).window_title == "видимое"


def test_a_failing_note_never_blocks_freezing(tmp_path, monkeypatch):
    rt, win, procs, clock = make_runtime(tmp_path)
    info, state = _running(rt, win, procs)

    def boom(*args, **kwargs):
        raise OSError("диск недоступен")

    monkeypatch.setattr("ws.notes.save", boom)
    report = freezer.freeze(rt, "a", state)
    assert procs.suspended == {10} and report.suspended == 1


def test_thaw_without_note_has_none(tmp_path):
    rt, win, procs, clock = make_runtime(tmp_path)
    info, state = _running(rt, win, procs)
    state.spaces["a"].frozen = True
    assert freezer.thaw(rt, "a", state).note is None


def test_switch_report_exposes_the_note(tmp_path):
    from ws import switcher
    from ws.models import ExplorerItem, Space

    rt, win, procs, clock = make_runtime(tmp_path)
    info, state = _running(rt, win, procs)
    state.active = "a"
    procs.add(700, "explorer.exe")
    procs.on_spawn = lambda argv: win.appear_later(0.2, win.make_window("explorer.exe", "CabinetWClass", 700))
    space_b = Space(name="b", items=(ExplorerItem(path=tmp_path),))
    switcher.switch(rt, space_b, state)
    clock.sleep(120)
    report = switcher.switch(rt, Space(name="a", items=(ExplorerItem(path=tmp_path),)), state)
    assert report.note and report.note.startswith("Ты ушёл отсюда 2 мин назад")
    assert switcher.SwitchReport().note is None
    assert Rect  # импорт используется в других тестах файла
