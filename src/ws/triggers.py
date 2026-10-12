"""Автоподсказки: при смене ветки git в репозитории пространства предложить переключиться на него."""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from ws import git
from ws.models import ExplorerItem, Space, TerminalItem, VSCodeItem

_UNSET = object()


@dataclass(frozen=True)
class Suggestion:
    space: str
    branch: str


def repo_dir(space: Space) -> Path | None:
    """Где искать репозиторий: triggers.repo, иначе первая папка из items."""
    if space.triggers is not None and space.triggers.repo is not None:
        return space.triggers.repo
    for item in space.items:
        if isinstance(item, (VSCodeItem, ExplorerItem)):
            return item.path
        if isinstance(item, TerminalItem) and item.tabs:
            return item.tabs[0].dir
    return None


def branch_matches(pattern: str, branch: str) -> bool:
    return fnmatch.fnmatchcase(branch, pattern)


class BranchWatcher:
    """Помнит последнюю ветку каждого репозитория и замечает именно смену, а не состояние."""

    def __init__(self, branch_of: Callable[[Path], str | None] = git.current_branch) -> None:
        self._branch_of = branch_of
        self._last: dict[str, object] = {}

    def check(self, spaces: Iterable[Space], active: str | None) -> list[Suggestion]:
        suggestions = []
        seen = set()
        for space in spaces:
            if space.triggers is None:
                continue
            seen.add(space.name)
            repo = repo_dir(space)
            branch = self._branch_of(repo) if repo is not None else None
            previous = self._last.get(space.name, _UNSET)
            self._last[space.name] = branch
            if previous is _UNSET or branch == previous or branch is None:
                continue  # первое наблюдение не считается сменой, чтобы не пугать при запуске трея
            if branch_matches(space.triggers.git_branch, branch) and space.name != active:
                suggestions.append(Suggestion(space.name, branch))
        for gone in set(self._last) - seen:
            del self._last[gone]
        return suggestions

    def still_matches(self, space: Space) -> bool:
        """Репозиторий всё ещё на нужной ветке (иначе подсказка устарела)."""
        branch = self._last.get(space.name)
        return isinstance(branch, str) and space.triggers is not None and branch_matches(space.triggers.git_branch, branch)
