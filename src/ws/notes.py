"""Заметки «где я остановился»: что снимается при уходе из пространства и как показывается при возврате.

Оболочки пишут свои файлы сами (ws hook): notes/<пространство>/<pid>.json с cwd и последней командой.
При заморозке ws собирает их вместе с заголовком верхнего окна в notes/<пространство>.json.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from ws.config import Paths
from ws.models import SpaceState, WindowInfo

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ShellNote:
    pid: int
    cwd: str | None
    command: str | None
    time: float


@dataclass
class Note:
    left_at: float
    window_title: str | None = None
    window_exe: str | None = None
    shells: list[ShellNote] = field(default_factory=list)


def shells_dir(paths: Paths, name: str) -> Path:
    return paths.notes / name


def note_file(paths: Paths, name: str) -> Path:
    return paths.notes / f"{name}.json"


def read_shells(paths: Paths, name: str, alive: Callable[[int], bool]) -> list[ShellNote]:
    """Заметки оболочек пространства, самые свежие первыми. Файлы мёртвых оболочек удаляются."""
    folder = shells_dir(paths, name)
    if not folder.is_dir():
        return []
    result = []
    for file in folder.glob("*.json"):
        try:
            raw = json.loads(file.read_text(encoding="utf-8-sig"))
            note = ShellNote(
                pid=int(raw["pid"]),
                cwd=_text(raw.get("cwd")),
                command=_text(raw.get("command")),
                time=float(raw.get("time") or 0),
            )
        except (OSError, ValueError, KeyError, TypeError):
            continue  # битый или недописанный файл: пропускаем, хук перезапишет
        if alive(note.pid):
            result.append(note)
        else:
            try:
                file.unlink()
            except OSError:
                pass
    return sorted(result, key=lambda n: n.time, reverse=True)


def _text(value: object) -> str | None:
    return str(value) if isinstance(value, str) and value.strip() else None


def capture(
    paths: Paths,
    name: str,
    sp: SpaceState,
    windows_by_zorder: Sequence[WindowInfo],
    now: float,
    alive: Callable[[int], bool],
) -> Note:
    """Верхнее по Z-порядку видимое окно пространства считается тем, где пользователь работал."""
    hwnds = {w.hwnd for w in sp.windows}
    top = next((w for w in windows_by_zorder if w.hwnd in hwnds and w.visible and not w.minimized), None)
    return Note(
        left_at=now,
        window_title=" ".join(top.title.split()) if top and top.title else None,
        window_exe=top.exe if top else None,
        shells=read_shells(paths, name, alive),
    )


def save(paths: Paths, name: str, note: Note) -> None:
    target = note_file(paths, name)
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(asdict(note), ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, target)


def load(paths: Paths, name: str) -> Note | None:
    try:
        raw = json.loads(note_file(paths, name).read_text(encoding="utf-8"))
        return Note(
            left_at=float(raw["left_at"]),
            window_title=_text(raw.get("window_title")),
            window_exe=_text(raw.get("window_exe")),
            shells=[ShellNote(**s) for s in raw.get("shells", [])],
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


def ago(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return "только что"
    if seconds < 3600:
        return f"{seconds // 60} мин назад"
    if seconds < 86400:
        return f"{seconds // 3600} ч назад"
    return f"{seconds // 86400} дн назад"


def describe(note: Note, now: float) -> str:
    """Короткий текст для вывода и уведомления (укладывается в ~250 символов)."""
    parts = [f"Ты ушёл отсюда {ago(now - note.left_at)}."]
    if note.window_title:
        parts.append(f"Последнее окно: «{note.window_title[:60]}».")
    shell = next((s for s in note.shells if s.command), None) or (note.shells[0] if note.shells else None)
    if shell is not None:
        if shell.command:
            where = f" (в {shell.cwd})" if shell.cwd else ""
            parts.append(f"Последняя команда: {shell.command[:70]}{where}.")
        elif shell.cwd:
            parts.append(f"Терминал был в {shell.cwd}.")
    return " ".join(parts)
