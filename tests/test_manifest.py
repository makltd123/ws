from pathlib import Path

import pytest

from ws import manifest
from ws.errors import UserError
from ws.models import AppItem, BrowserItem, ExplorerItem, TerminalItem, VSCodeItem

FULL = r"""
name: api-project
description: Бэкенд платёжки
env:
  GIT_AUTHOR_EMAIL: me@work.com
items:
  - type: vscode
    path: C:\proj\api
    window: {monitor: 0, x: 0, y: 0, w: 1280, h: 1040}
  - type: terminal
    tabs:
      - {dir: C:\proj\api, cmd: "uv run uvicorn app:app --reload", title: server}
      - {dir: C:\proj\api, title: shell}
    window: {monitor: 0, x: 1280, y: 0, w: 640, h: 520}
  - type: browser
    browser: chrome
    profile: api-project
    urls: [https://github.com/org/api, "http://localhost:8000/docs"]
  - type: explorer
    path: C:\proj\api\docs
  - type: app
    exe: C:\Program Files\Obsidian\Obsidian.exe
    args: ["--vault", "C:\\proj\\notes"]
freeze:
  exclude: [Zoom.exe, Teams.exe]
"""


def test_full_manifest():
    space = manifest.parse(FULL, "x.yml")
    assert space.name == "api-project"
    assert space.env == {"GIT_AUTHOR_EMAIL": "me@work.com"}
    vscode, terminal, browser, explorer, app = space.items
    assert isinstance(vscode, VSCodeItem) and vscode.isolate is True
    assert vscode.window.w == 1280
    assert isinstance(terminal, TerminalItem) and len(terminal.tabs) == 2
    assert terminal.tabs[0].cmd == "uv run uvicorn app:app --reload"
    assert terminal.tabs[1].cmd is None
    assert isinstance(browser, BrowserItem) and browser.profile == "api-project" and len(browser.urls) == 2
    assert isinstance(explorer, ExplorerItem) and explorer.window is None
    assert isinstance(app, AppItem) and app.args == ("--vault", r"C:\proj\notes")
    assert space.freeze_exclude == {"zoom.exe", "teams.exe"}


def test_name_defaults_to_file_name():
    assert manifest.parse("items: []", "x.yml", default_name="demo").name == "demo"


@pytest.mark.parametrize(
    "text, fragment",
    [
        ("name: a\nitems:\n  - type: nope", "items[0].type"),
        ("name: a\nitems:\n  - {type: vscode}", "items[0].path: обязательное поле"),
        ("name: a\nitems:\n  - {type: vscode, path: C, bogus: 1}", "неизвестные поля: bogus"),
        ("name: a\nitems:\n  - {type: browser, browser: firefox}", "items[0].browser"),
        ("name: a\nitems:\n  - {type: browser, profile: 'a b'}", "items[0].profile"),
        ("name: a\nitems:\n  - {type: explorer, path: C, window: {x: 0, y: 0, w: 0, h: 5}}", "w и h"),
        ("name: a\nitems:\n  - {type: explorer, path: C, window: {x: 0, y: 0, w: 5}}", "window.h"),
        ("name: '../evil'", "name"),
        ("name: a\nitems: 5", "items: ожидался список"),
        ("name: [", "YAML"),
        ("- 1", "ожидался словарь"),
    ],
)
def test_errors_name_the_field(text, fragment):
    with pytest.raises(UserError) as exc:
        manifest.parse(text, "x.yml")
    assert fragment in str(exc.value)
    assert "x.yml" in str(exc.value)


@pytest.mark.parametrize("name", ["", "a b", "../x", "a/b", "con:", "x."])
def test_bad_names(name):
    with pytest.raises(UserError):
        manifest.validate_name(name)


def test_load_missing(tmp_path: Path):
    from ws.config import Paths

    paths = Paths(tmp_path)
    paths.ensure()
    with pytest.raises(UserError, match="Нет пространства"):
        manifest.load(paths, "ghost")


def test_load_name_must_match_file(tmp_path: Path):
    from ws.config import Paths

    paths = Paths(tmp_path)
    paths.ensure()
    (paths.spaces / "one.yml").write_text("name: two\nitems: []\n", encoding="utf-8")
    with pytest.raises(UserError, match="файл называется"):
        manifest.load(paths, "one")
    assert manifest.list_names(paths) == ["one"]
