"""Ветки git и подсказки переключения."""

from pathlib import Path

from fakes import make_runtime

from ws import git, manifest
from ws import state as state_mod
from ws.models import ExplorerItem, Space, SpaceState, TerminalItem, TerminalTab, Triggers, VSCodeItem, WindowRef
from ws.tray import TrayController, build_menu_items
from ws.triggers import BranchWatcher, branch_matches, repo_dir


def _repo(tmp_path, head="ref: refs/heads/main\n", name="repo"):
    repo = tmp_path / name
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text(head, encoding="utf-8")
    return repo


def _checkout(repo, branch):
    (repo / ".git" / "HEAD").write_text(f"ref: refs/heads/{branch}\n", encoding="utf-8")


def test_current_branch_from_head(tmp_path):
    repo = _repo(tmp_path)
    assert git.current_branch(repo) == "main"
    _checkout(repo, "feature/payments")
    assert git.current_branch(repo) == "feature/payments"


def test_subfolders_belong_to_the_repo(tmp_path):
    repo = _repo(tmp_path)
    (repo / "a" / "b").mkdir(parents=True)
    assert git.current_branch(repo / "a" / "b") == "main"


def test_detached_head_and_non_repos_have_no_branch(tmp_path):
    assert git.current_branch(_repo(tmp_path, head="3f2a9c1d4e5b6a7988\n", name="detached")) is None
    plain = tmp_path / "plain"
    plain.mkdir()
    assert git.current_branch(plain) is None
    assert git.current_branch(tmp_path / "missing") is None


def test_worktree_with_gitdir_file(tmp_path):
    real = tmp_path / "main-repo" / ".git" / "worktrees" / "wt"
    real.mkdir(parents=True)
    (real / "HEAD").write_text("ref: refs/heads/hotfix\n", encoding="utf-8")
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {real}\n", encoding="utf-8")
    assert git.current_branch(wt) == "hotfix"
    rel = tmp_path / "sub"
    rel.mkdir()
    (rel / ".git").write_text("gitdir: ../main-repo/.git/worktrees/wt\n", encoding="utf-8")
    assert git.current_branch(rel) == "hotfix"


def test_branch_masks():
    assert branch_matches("feature/payments", "feature/payments")
    assert not branch_matches("feature/payments", "feature/payments-2")
    assert branch_matches("feature/*", "feature/anything/deep")
    assert not branch_matches("feature/*", "main")
    assert not branch_matches("Main", "main")  # регистр важен: ветки в git чувствительны к нему


def test_repo_dir_priority(tmp_path):
    explicit = Space("a", items=(VSCodeItem(path=Path("x")),), triggers=Triggers("m", repo=Path("repo")))
    assert repo_dir(explicit) == Path("repo")
    # без triggers.repo берётся первая по порядку папка из items
    first = Space("a", items=(TerminalItem(tabs=(TerminalTab(Path("t")),)), VSCodeItem(path=Path("v"))))
    assert repo_dir(first) == Path("t")
    assert repo_dir(Space("a", items=(TerminalItem(), VSCodeItem(path=Path("v"))))) == Path("v")  # терминал без вкладок пропущен
    assert repo_dir(Space("a", items=(ExplorerItem(path=Path("e")),))) == Path("e")
    assert repo_dir(Space("a")) is None


def _space(repo, mask="feature/*", name="api"):
    return Space(name, items=(VSCodeItem(path=repo),), triggers=Triggers(mask))


def test_first_observation_is_not_a_change(tmp_path):
    repo = _repo(tmp_path, head="ref: refs/heads/feature/payments\n")
    watcher = BranchWatcher()
    assert watcher.check([_space(repo)], active=None) == []  # уже на нужной ветке при запуске: молчим
    assert watcher.check([_space(repo)], active=None) == []  # и дальше без изменений тоже


def test_switching_to_a_matching_branch_gives_one_suggestion(tmp_path):
    repo = _repo(tmp_path)
    watcher = BranchWatcher()
    space = _space(repo)
    watcher.check([space], active=None)
    _checkout(repo, "feature/payments")
    (suggestion,) = watcher.check([space], active=None)
    assert (suggestion.space, suggestion.branch) == ("api", "feature/payments")
    assert watcher.check([space], active=None) == []  # повторно не предлагаем, пока ветка не сменилась
    assert watcher.still_matches(space)
    _checkout(repo, "main")
    assert watcher.check([space], active=None) == [] and not watcher.still_matches(space)


def test_no_suggestion_for_the_active_space_or_other_branches(tmp_path):
    repo = _repo(tmp_path)
    watcher = BranchWatcher()
    space = _space(repo)
    watcher.check([space], active="api")
    _checkout(repo, "feature/x")
    assert watcher.check([space], active="api") == []
    _checkout(repo, "bugfix/y")
    assert watcher.check([space], active=None) == []


def test_spaces_without_triggers_and_removed_spaces_are_ignored(tmp_path):
    repo = _repo(tmp_path)
    watcher = BranchWatcher()
    plain = Space("plain", items=(VSCodeItem(path=repo),))
    assert watcher.check([plain], active=None) == []
    space = _space(repo)
    watcher.check([space], active=None)
    watcher.check([], active=None)  # пространство пропало из списка
    _checkout(repo, "feature/z")
    watcher.check([space], active=None)  # снова появилось: это новое первое наблюдение
    assert watcher.check([space], active=None) == []


# --- в трее ----------------------------------------------------------------


class TrayHarness:
    def __init__(self, tmp_path):
        self.rt, self.win, self.procs, self.clock = make_runtime(tmp_path)
        self.notes = []
        self.changes = 0
        self.repo = _repo(tmp_path)
        (self.rt.paths.spaces / "api.yml").write_text(
            f"name: api\nitems:\n  - {{type: explorer, path: '{self.repo}'}}\ntriggers:\n  git_branch: 'feature/*'\n",
            encoding="utf-8",
        )
        (self.rt.paths.spaces / "plain.yml").write_text("name: plain\nitems: []\n", encoding="utf-8")
        self.procs.add(700, "explorer.exe")
        self.procs.on_spawn = lambda argv: self.win.appear_later(
            0.2, self.win.make_window("explorer.exe", "CabinetWClass", 700)
        )
        self.controller = TrayController(
            self.rt, lambda t, m: self.notes.append((t, m)), lambda fn: fn(), on_change=self._changed
        )
        self.controller.refresh_bindings()

    def _changed(self):
        self.changes += 1


def test_tray_suggests_the_space_when_the_branch_changes(tmp_path):
    h = TrayHarness(tmp_path)
    h.controller.check_triggers()  # первое наблюдение
    assert h.controller.suggestions == {} and h.notes == []
    _checkout(h.repo, "feature/payments")

    h.controller.check_triggers()

    assert list(h.controller.suggestions) == ["api"]
    assert h.notes[0][0] == "ws: ветка feature/payments" and "'api'" in h.notes[0][1]
    assert h.changes == 1
    import pystray

    items = list(pystray.Menu(lambda: build_menu_items(h.controller, lambda: None, lambda: None)).items)
    assert str(items[0].text) == "→ Перейти в «api» (ветка feature/payments)"
    items[0](None)  # клик по подсказке = переключение
    assert state_mod.load(h.rt.paths).active == "api" and h.controller.suggestions == {}


def test_suggestion_disappears_when_the_branch_goes_back(tmp_path):
    h = TrayHarness(tmp_path)
    h.controller.check_triggers()
    _checkout(h.repo, "feature/payments")
    h.controller.check_triggers()
    _checkout(h.repo, "main")
    h.controller.check_triggers()
    assert h.controller.suggestions == {} and h.changes == 2


def test_broken_manifest_does_not_stop_trigger_checks(tmp_path):
    h = TrayHarness(tmp_path)
    (h.rt.paths.spaces / "broken.yml").write_text("name: [", encoding="utf-8")
    h.controller.check_triggers()
    _checkout(h.repo, "feature/x")
    h.controller.check_triggers()
    assert "api" in h.controller.suggestions


def test_active_space_gets_no_suggestion(tmp_path):
    h = TrayHarness(tmp_path)
    h.procs.add(1, "x")
    window = h.win.add(h.win.make_window("x", "c", 1))
    state = state_mod.State(active="api")
    state.spaces["api"] = SpaceState(windows=[WindowRef(window.hwnd, 1, h.procs.procs[1].created, "x", "x", 0)])
    state_mod.save(h.rt.paths, state)
    h.controller.check_triggers()
    _checkout(h.repo, "feature/payments")
    h.controller.check_triggers()
    assert h.controller.suggestions == {}


def test_manifest_triggers_reach_the_watcher_via_repo_manifest(tmp_path):
    repo = _repo(tmp_path, name="proj")
    (repo / ".workspace.yml").write_text(
        "name: proj\nitems:\n  - {type: vscode, path: .}\ntriggers: {git_branch: 'release/*'}\n", encoding="utf-8"
    )
    space = manifest.load_file(repo / ".workspace.yml")
    watcher = BranchWatcher()
    watcher.check([space], None)
    _checkout(repo, "release/1.2")
    assert [s.space for s in watcher.check([space], None)] == ["proj"]
