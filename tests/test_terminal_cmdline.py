import subprocess
from pathlib import Path

from ws.adapters.terminal import build_argv
from ws.models import TerminalItem, TerminalTab


def test_two_tabs_are_separated_by_standalone_semicolon():
    item = TerminalItem(
        tabs=(
            TerminalTab(Path(r"C:\proj\api"), "uv run uvicorn app:app --reload", "server"),
            TerminalTab(Path(r"C:\proj\api"), None, "shell"),
        )
    )
    assert build_argv(item) == [
        "wt.exe", "-w", "new",
        "new-tab", "-d", r"C:\proj\api", "--title", "server", "--suppressApplicationTitle",
        "powershell.exe", "-NoExit", "-Command", "uv run uvicorn app:app --reload",
        ";",
        "new-tab", "-d", r"C:\proj\api", "--title", "shell", "--suppressApplicationTitle",
    ]


def test_semicolons_inside_text_are_escaped():
    item = TerminalItem(tabs=(TerminalTab(Path(r"C:\a"), "echo a; echo b", "x;y"),))
    argv = build_argv(item)
    assert "echo a\\; echo b" in argv
    assert "x\\;y" in argv
    assert argv.count(";") == 0  # разделителей между вкладками нет


def test_path_with_spaces_stays_one_argument_in_command_line():
    item = TerminalItem(tabs=(TerminalTab(Path(r"C:\My Projects\api"), None, None),))
    line = subprocess.list2cmdline(build_argv(item))
    assert '"C:\\My Projects\\api"' in line


def test_root_directory_survives_quoting():
    item = TerminalItem(tabs=(TerminalTab(Path("C:\\"), None, None),))
    line = subprocess.list2cmdline(build_argv(item) + ["x y"])
    assert line.endswith('"x y"') and "C:\\" in line


def test_no_tabs_just_opens_window():
    assert build_argv(TerminalItem()) == ["wt.exe", "-w", "new"]
