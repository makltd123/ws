"""Текущая ветка git без запуска git: читается .git/HEAD."""

from __future__ import annotations

from pathlib import Path

_PREFIX = "ref: refs/heads/"


def git_dir(start: Path) -> Path | None:
    """Папка .git репозитория, в котором лежит start (учитывает worktree и submodule: .git как файл)."""
    try:
        current = start.resolve()
    except OSError:
        return None
    for folder in (current, *current.parents):
        dot_git = folder / ".git"
        if dot_git.is_dir():
            return dot_git
        if dot_git.is_file():
            try:
                text = dot_git.read_text(encoding="utf-8").strip()
            except OSError:
                return None
            if text.startswith("gitdir:"):
                target = Path(text[len("gitdir:") :].strip())
                return target if target.is_absolute() else (folder / target).resolve()
            return None
    return None


def current_branch(start: Path) -> str | None:
    """Имя ветки; None, если это не репозиторий или HEAD отсоединён."""
    directory = git_dir(start)
    if directory is None:
        return None
    try:
        head = (directory / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return head[len(_PREFIX) :] if head.startswith(_PREFIX) else None
