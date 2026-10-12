"""Пути, глобальный конфиг, логирование и Runtime (набор зависимостей сценариев)."""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, ContextManager

import yaml

from ws.errors import UserError


@dataclass(frozen=True)
class Paths:
    root: Path

    @property
    def spaces(self) -> Path:
        return self.root / "spaces"

    @property
    def profiles(self) -> Path:
        return self.root / "profiles"

    @property
    def notes(self) -> Path:
        return self.root / "notes"

    @property
    def repos_file(self) -> Path:
        return self.root / "repos.json"

    @property
    def state_file(self) -> Path:
        return self.root / "state.json"

    @property
    def config_file(self) -> Path:
        return self.root / "config.yml"

    @property
    def log_file(self) -> Path:
        return self.root / "ws.log"

    def ensure(self) -> None:
        for directory in (self.root, self.spaces, self.profiles, self.notes):
            directory.mkdir(parents=True, exist_ok=True)


def default_paths() -> Paths:
    # WS_HOME нужен для тестов и ручных проверок на отдельной папке.
    override = os.environ.get("WS_HOME")
    if override:
        return Paths(Path(override))
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Paths(Path(appdata) / "ws")


@dataclass(frozen=True)
class Config:
    freeze_exclude: frozenset[str] = frozenset()
    # имя пространства -> комбинация ("ctrl+alt+1"); None = не задано, действуют Ctrl+Alt+1..9 по порядку
    hotkeys: dict[str, str] | None = None
    bind_timeout: float = 10.0  # сколько ждать новое окно после запуска
    poll_interval: float = 0.2
    loose_grace: float = 3.0  # через сколько брать единственное «похожее» окно


def load_config(paths: Paths) -> Config:
    if not paths.config_file.exists():
        return Config()
    try:
        raw: Any = yaml.safe_load(paths.config_file.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise UserError(f"{paths.config_file}: не удалось разобрать YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise UserError(f"{paths.config_file}: ожидался словарь")
    freeze = raw.get("freeze") or {}
    exclude = freeze.get("exclude") or [] if isinstance(freeze, dict) else []
    hotkeys = raw.get("hotkeys")
    if hotkeys is not None and not isinstance(hotkeys, dict):
        raise UserError(f"{paths.config_file}: hotkeys: ожидался словарь «имя пространства: комбинация»")
    return Config(
        freeze_exclude=frozenset(str(e).lower() for e in exclude),
        hotkeys={str(k): str(v) for k, v in hotkeys.items()} if hotkeys is not None else None,
        bind_timeout=float(raw.get("bind_timeout", 10.0)),
    )


def setup_logging(paths: Paths) -> None:
    handler = logging.FileHandler(paths.log_file, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)


class SystemClock:
    def now(self) -> float:
        return time.time()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


@dataclass
class Runtime:
    """Всё, что нужно сценариям. cli.py собирает настоящий, тесты фейковый."""

    paths: Paths
    config: Config
    win: Any  # winapi.WindowSystem
    procs: Any  # procs.ProcessSystem
    clock: Any = field(default_factory=SystemClock)
    lock: Callable[[], ContextManager[None]] | None = None
