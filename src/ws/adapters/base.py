"""Интерфейс адаптера: знания о конкретном приложении."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Iterable, Mapping, Sequence

from ws.models import Item, Space, WindowInfo

if TYPE_CHECKING:
    from ws.config import Runtime


@dataclass(frozen=True)
class LaunchCommand:
    argv: list[str]
    env: dict[str, str] = field(default_factory=dict)  # добавки сверх env пространства
    cwd: Path | None = None
    env_remove: tuple[str, ...] = ()  # переменные, которые нельзя передавать дальше


@dataclass(frozen=True)
class Issue:
    level: str  # ok | warn | error
    message: str


@dataclass(frozen=True)
class CaptureContext:
    rt: Runtime
    explorer_folders: Mapping[int, Path]  # hwnd окна Проводника -> папка


@dataclass(frozen=True)
class Capture:
    """Результат снятия одного окна.

    item=None значит «элемент собрать не удалось»: в манифест уходит закомментированный
    блок из stub, чтобы позиция окна не потерялась, а `ws open` не споткнулся.
    """

    item: Item | None
    notes: tuple[str, ...] = ()  # комментарии для пользователя перед элементом
    stub: tuple[str, ...] = ()  # строки YAML закомментированного блока (без window)


class CannotCapture(Exception):
    """Окно нельзя описать манифестом; текст попадёт в отчёт как причина пропуска."""


class Adapter:
    type_name: ClassVar[str] = ""
    # Если окно по exe не опознано, через loose_grace секунд берётся единственное новое окно.
    loose: ClassVar[bool] = False

    def check(self, rt: Runtime) -> list[Issue]:
        return []

    def owns(self, window: WindowInfo) -> bool:
        """Для ws save: это окно приложения данного типа?"""
        return False

    def capture(self, window: WindowInfo, ctx: CaptureContext) -> Capture:
        """Для ws save: описать окно элементом манифеста (без позиции окна)."""
        raise CannotCapture("снятие не поддерживается")

    def launch(self, item: Item, space: Space, rt: Runtime) -> LaunchCommand:
        raise NotImplementedError

    def matches(self, item: Item, window: WindowInfo) -> bool:
        """Подходит ли новое окно под ожидание этого элемента."""
        raise NotImplementedError


def option_value(cmdline: Sequence[str], name: str) -> str | None:
    """Значение опции командной строки: `--name=value` или `--name value`."""
    for i, arg in enumerate(cmdline):
        if arg.startswith(name + "="):
            return arg[len(name) + 1 :]
        if arg == name and i + 1 < len(cmdline):
            return cmdline[i + 1]
    return None


def env_path(name: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value) if value else None


def find_executable(name: str, candidates: Iterable[Path | None]) -> Path | None:
    for candidate in candidates:
        if candidate is not None and candidate.exists():
            return candidate
    found = shutil.which(name)
    return Path(found) if found else None
