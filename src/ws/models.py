"""Модель данных. Здесь нет вызовов Windows, только dataclass'ы."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar, Mapping


# --- геометрия и снимки системы ---------------------------------------------


@dataclass(frozen=True)
class Rect:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top


@dataclass(frozen=True)
class MonitorInfo:
    index: int  # 0 = основной, дальше слева направо, сверху вниз
    primary: bool
    bounds: Rect
    work: Rect  # рабочая область без панели задач
    dpi: int


@dataclass(frozen=True)
class WindowInfo:
    hwnd: int
    pid: int
    exe: str  # имя файла, например "Code.exe"
    title: str
    class_name: str
    visible: bool
    minimized: bool
    maximized: bool
    cloaked: bool
    tool_window: bool
    owner: int
    rect: Rect  # GetWindowRect, вместе с невидимой рамкой
    frame_rect: Rect  # видимые границы по данным DWM


@dataclass(frozen=True)
class ProcInfo:
    pid: int
    exe: str
    cmdline: tuple[str, ...]
    created: float
    parent: int | None
    status: str
    path: str = ""  # полный путь к exe; пусто, если доступа нет


# --- манифест -----------------------------------------------------------------


@dataclass(frozen=True)
class WindowSpec:
    """Позиция окна: смещение внутри рабочей области монитора, в пикселях."""

    monitor: int
    x: int
    y: int
    w: int
    h: int
    maximized: bool = False


@dataclass(frozen=True, kw_only=True)
class Item:
    type: ClassVar[str] = ""
    window: WindowSpec | None = None


@dataclass(frozen=True, kw_only=True)
class VSCodeItem(Item):
    type: ClassVar[str] = "vscode"
    path: Path
    isolate: bool = True  # отдельный экземпляр VS Code на пространство


@dataclass(frozen=True)
class TerminalTab:
    dir: Path
    cmd: str | None = None
    title: str | None = None


@dataclass(frozen=True, kw_only=True)
class TerminalItem(Item):
    type: ClassVar[str] = "terminal"
    tabs: tuple[TerminalTab, ...] = ()


@dataclass(frozen=True, kw_only=True)
class BrowserItem(Item):
    type: ClassVar[str] = "browser"
    browser: str = "chrome"  # chrome | edge
    profile: str | None = None  # None = обычный браузер пользователя, без изоляции
    urls: tuple[str, ...] = ()


@dataclass(frozen=True, kw_only=True)
class ExplorerItem(Item):
    type: ClassVar[str] = "explorer"
    path: Path


@dataclass(frozen=True, kw_only=True)
class AppItem(Item):
    type: ClassVar[str] = "app"
    exe: Path
    args: tuple[str, ...] = ()


@dataclass(frozen=True)
class Triggers:
    """Автоматические подсказки переключения (этап 5)."""

    git_branch: str  # маска имени ветки, например "feature/payments" или "feature/*"
    repo: Path | None = None  # где искать репозиторий; по умолчанию первая папка из items


@dataclass(frozen=True)
class Space:
    name: str
    description: str = ""
    env: Mapping[str, str] = field(default_factory=dict)
    items: tuple[Item, ...] = ()
    freeze_exclude: frozenset[str] = frozenset()
    triggers: Triggers | None = None
    source: Path | None = field(default=None, compare=False)  # файл манифеста, если известен


# --- состояние ----------------------------------------------------------------


@dataclass
class WindowRef:
    hwnd: int
    pid: int
    pid_created: float  # время старта процесса; 0.0 = узнать не удалось
    exe: str
    title: str
    item_index: int | None = None
    last_rect: Rect | None = None
    uncontrollable: bool = False


@dataclass
class ProcRef:
    pid: int
    created: float


@dataclass
class SpaceState:
    windows: list[WindowRef] = field(default_factory=list)
    frozen: bool = False
    frozen_procs: list[ProcRef] = field(default_factory=list)
    shell_procs: list[ProcRef] = field(default_factory=list)  # оболочки вкладок терминала
    last_active: float = 0.0


@dataclass
class State:
    active: str | None = None
    spaces: dict[str, SpaceState] = field(default_factory=dict)
