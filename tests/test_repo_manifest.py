"""Манифесты в репозиториях (.workspace.yml), реестр repos.json и триггеры в манифесте."""

import json
from pathlib import Path

import pytest

from ws import manifest
from ws.config import Paths
from ws.errors import UserError
from ws.models import ExplorerItem, TerminalItem, VSCodeItem


def _paths(tmp_path) -> Paths:
    paths = Paths(tmp_path / "ws-home")
    paths.ensure()
    return paths


def _repo(tmp_path, text, name="My Project") -> Path:
    repo = tmp_path / name
    (repo / "src").mkdir(parents=True)
    (repo / ".workspace.yml").write_text(text, encoding="utf-8")
    return repo


REPO_YML = """\
name: shop
items:
  - type: vscode
    path: .
  - type: terminal
    tabs: [{dir: src, title: dev}]
  - type: explorer
    path: ..\\other
triggers:
  git_branch: "feature/*"
"""


def test_relative_paths_are_resolved_against_the_manifest_folder(tmp_path):
    repo = _repo(tmp_path, REPO_YML)
    space = manifest.load_file(repo / ".workspace.yml")
    vscode, terminal, explorer = space.items
    assert isinstance(vscode, VSCodeItem) and vscode.path == repo.resolve()
    assert isinstance(terminal, TerminalItem) and terminal.tabs[0].dir == repo.resolve() / "src"
    assert isinstance(explorer, ExplorerItem) and explorer.path == repo.resolve().parent / "other"
    assert space.source == (repo / ".workspace.yml").resolve()


def test_absolute_paths_stay_as_they_are(tmp_path):
    repo = _repo(tmp_path, "name: abs\nitems:\n  - {type: explorer, path: 'C:\\Windows'}\n")
    assert manifest.load_file(repo / ".workspace.yml").items[0].path == Path("C:\\Windows")


def test_name_defaults_to_a_sanitized_folder_name(tmp_path):
    repo = _repo(tmp_path, "items: []\n", name="My Project (v2)")
    assert manifest.load_file(repo / ".workspace.yml").name == "My-Project--v2"


def test_find_repo_manifest_walks_up(tmp_path):
    repo = _repo(tmp_path, "name: shop\nitems: []\n")
    deep = repo / "src" / "pkg"
    deep.mkdir()
    assert manifest.find_repo_manifest(deep) == (repo / ".workspace.yml").resolve()
    lonely = tmp_path / "lonely"
    lonely.mkdir()
    assert manifest.find_repo_manifest(lonely) is None


@pytest.mark.parametrize("arg, expected", [(".", True), ("..", True), (r"C:\x", True), ("a/b", True), ("~", True),
                                           (".hidden", True), ("shop", False), ("api-project", False), ("my_space.v2", False)])
def test_is_path_arg(arg, expected):
    assert manifest.is_path_arg(arg) is expected


def test_open_dot_registers_and_the_name_works_afterwards(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    repo = _repo(tmp_path, REPO_YML)
    monkeypatch.chdir(repo / "src")

    space = manifest.resolve(paths, ".")  # ws open . из подпапки репозитория

    assert space.name == "shop"
    assert manifest.list_names(paths) == ["shop"]
    assert manifest.load(paths, "shop").items == space.items  # дальше по имени, как личные
    assert manifest.editable_path(paths, "shop") == (repo / ".workspace.yml").resolve()
    assert json.loads(paths.repos_file.read_text(encoding="utf-8")) == {"shop": str((repo / ".workspace.yml").resolve())}


def test_resolve_accepts_a_folder_or_the_file_itself(tmp_path):
    paths = _paths(tmp_path)
    repo = _repo(tmp_path, REPO_YML)
    assert manifest.resolve(paths, str(repo), register=False).name == "shop"
    assert manifest.resolve(paths, str(repo / ".workspace.yml"), register=False).name == "shop"
    assert manifest.list_names(paths) == []  # без register ничего не запоминается


def test_resolve_errors_are_user_errors(tmp_path):
    paths = _paths(tmp_path)
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(UserError, match="нет файла .workspace.yml"):
        manifest.resolve(paths, str(empty))
    with pytest.raises(UserError, match="Путь не найден"):
        manifest.resolve(paths, str(tmp_path / "missing"))
    with pytest.raises(UserError, match="Нет пространства"):
        manifest.resolve(paths, "ghost")


def test_a_name_that_exists_as_a_personal_space_cannot_be_registered(tmp_path):
    paths = _paths(tmp_path)
    (paths.spaces / "shop.yml").write_text("name: shop\nitems: []\n", encoding="utf-8")
    repo = _repo(tmp_path, REPO_YML)
    with pytest.raises(UserError, match="уже есть среди личных"):
        manifest.resolve(paths, str(repo))


def test_personal_manifest_wins_and_registry_is_not_shown_twice(tmp_path):
    paths = _paths(tmp_path)
    repo = _repo(tmp_path, REPO_YML)
    manifest.resolve(paths, str(repo))
    (paths.spaces / "shop.yml").write_text("name: shop\nitems: []\n", encoding="utf-8")  # позже появился личный
    assert manifest.list_names(paths) == ["shop"]
    assert manifest.load(paths, "shop").items == ()


def test_deleted_repo_manifest_disappears_from_the_list(tmp_path):
    paths = _paths(tmp_path)
    repo = _repo(tmp_path, REPO_YML)
    manifest.resolve(paths, str(repo))
    (repo / ".workspace.yml").unlink()
    assert manifest.list_names(paths) == []
    with pytest.raises(UserError, match="Нет пространства"):
        manifest.load(paths, "shop")


def test_renamed_space_in_repo_manifest_is_reported(tmp_path):
    paths = _paths(tmp_path)
    repo = _repo(tmp_path, REPO_YML)
    manifest.resolve(paths, str(repo))
    (repo / ".workspace.yml").write_text(REPO_YML.replace("name: shop", "name: store"), encoding="utf-8")
    with pytest.raises(manifest.ManifestError, match="теперь name"):
        manifest.load(paths, "shop")


def test_corrupt_registry_is_ignored(tmp_path):
    paths = _paths(tmp_path)
    paths.repos_file.write_text("{oops", encoding="utf-8")
    assert manifest.registered_repos(paths) == {}
    paths.repos_file.write_text('["list"]', encoding="utf-8")
    assert manifest.registered_repos(paths) == {}


def test_triggers_are_parsed(tmp_path):
    repo = _repo(tmp_path, REPO_YML.replace('git_branch: "feature/*"', 'git_branch: "feature/*"\n  repo: src'))
    space = manifest.load_file(repo / ".workspace.yml")
    assert space.triggers.git_branch == "feature/*" and space.triggers.repo == repo.resolve() / "src"


def test_triggers_without_repo_and_absent_triggers(tmp_path):
    assert manifest.parse("name: a\nitems: []\n", "x").triggers is None
    space = manifest.parse("name: a\nitems: []\ntriggers: {git_branch: main}\n", "x")
    assert space.triggers.repo is None


@pytest.mark.parametrize(
    "text, fragment",
    [
        ("name: a\ntriggers: {}\n", "triggers.git_branch: обязательное поле"),
        ("name: a\ntriggers: {git_branch: x, bogus: 1}\n", "неизвестные поля: bogus"),
        ("name: a\ntriggers: 5\n", "triggers: ожидался словарь"),
        ("name: a\ntriggers: {git_branch: ''}\n", "непустая строка"),
    ],
)
def test_bad_triggers(text, fragment):
    with pytest.raises(UserError, match=fragment):
        manifest.parse(text, "x.yml")
