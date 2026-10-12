"""ws save: снять раскладку текущих окон в новый манифест."""

from __future__ import annotations

import dataclasses
import json
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

import yaml

from ws import layout, manifest
from ws import state as state_mod
from ws.adapters import adapter_owning
from ws.adapters.base import Adapter, CannotCapture, Capture, CaptureContext
from ws.config import Runtime
from ws.errors import SystemFailure, UserError
from ws.launcher import is_candidate
from ws.models import (
    AppItem,
    BrowserItem,
    ExplorerItem,
    Item,
    SpaceState,
    State,
    TerminalItem,
    VSCodeItem,
    WindowInfo,
    WindowRef,
    WindowSpec,
)

# Оболочка Windows и системные хосты: это не рабочие окна пользователя, молча пропускаем.
_SYSTEM_EXES = {
    "searchhost.exe",
    "startmenuexperiencehost.exe",
    "shellexperiencehost.exe",
    "textinputhost.exe",
    "lockapp.exe",
    "applicationframehost.exe",
}
_PLAIN = re.compile(r"[\w./\\:()-][\w ./\\:()-]*")


@dataclass
class SaveReport:
    path: Path
    captured: list[str] = field(default_factory=list)  # что записано в манифест
    commented: list[str] = field(default_factory=list)  # записано закомментированным блоком (нужна доработка)
    skipped: list[str] = field(default_factory=list)  # окна, которые не попали в манифест, с причиной
    backup: Path | None = None


@dataclass
class _Entry:
    window: WindowInfo
    spec: WindowSpec
    capture: Capture
    item: Item | None  # элемент уже с window=spec


def save_space(
    rt: Runtime,
    name: str,
    state: State,
    *,
    force: bool = False,
    include: Callable[[WindowInfo], bool] | None = None,
) -> SaveReport:
    path = manifest.manifest_path(rt.paths, name)
    if name in manifest.registered_repos(rt.paths):
        raise UserError(f"Имя '{name}' занято манифестом из репозитория ({manifest.registered_repos(rt.paths)[name]})")
    if path.exists() and not force:
        raise UserError(f"Пространство '{name}' уже существует ({path}). Чтобы перезаписать, добавь --force")

    report = SaveReport(path=path)
    monitors = rt.win.monitors()
    foreign = {w.hwnd for n, sp in state.spaces.items() if n != name for w in sp.windows}
    windows = [w for w in rt.win.list_windows() if is_candidate(w) and w.title and (include is None or include(w))]
    ctx = CaptureContext(rt=rt, explorer_folders=rt.win.explorer_folders() if _has_explorer(windows) else {})

    entries: list[_Entry] = []
    for window in windows:
        label = f'{window.exe or "?"} "{_one_line(window.title)[:50]}"'
        adapter = adapter_owning(window)
        if _is_system(window, adapter):
            continue
        reason = _skip_reason(rt, window, foreign)
        if reason:
            report.skipped.append(f"{label}: {reason}")
            continue
        try:
            capture = adapter.capture(window, ctx)
        except CannotCapture as exc:
            report.skipped.append(f"{label}: {exc}")
            continue
        spec = layout.to_spec(window.frame_rect, monitors, maximized=window.maximized)
        item = dataclasses.replace(capture.item, window=spec) if capture.item is not None else None
        entries.append(_Entry(window, spec, capture, item))

    # Читается слева направо, сверху вниз по мониторам; порядок стабилен между запусками.
    entries.sort(key=lambda e: (e.spec.monitor, e.spec.x, e.spec.y))
    items = [e.item for e in entries if e.item is not None]

    text = _render(name, rt.clock.now(), entries)
    _self_check(name, text, items)

    if path.exists():
        report.backup = path.with_suffix(".yml.bak")
        shutil.copy2(path, report.backup)
    path.write_text(text, encoding="utf-8")

    refs = []
    index = 0
    for entry in entries:
        label = f'{entry.window.exe} "{_one_line(entry.window.title)[:50]}"'
        if entry.item is None:
            report.commented.append(f"{label}: закомментирован, нужна ручная доработка")
            continue
        report.captured.append(f"{entry.item.type}: {label}")
        proc = rt.procs.info(entry.window.pid)
        refs.append(
            WindowRef(
                hwnd=entry.window.hwnd,
                pid=entry.window.pid,
                pid_created=proc.created if proc else 0.0,
                exe=entry.window.exe,
                title=entry.window.title,
                item_index=index,
            )
        )
        index += 1
    # Снятые окна сразу принадлежат новому пространству, иначе `ws open` следом создал бы дубликаты.
    state.spaces[name] = SpaceState(windows=refs, last_active=rt.clock.now())
    if state.active is None and refs:
        state.active = name
    return report


def _has_explorer(windows: list[WindowInfo]) -> bool:
    return any(w.exe.lower() == "explorer.exe" and w.class_name == "CabinetWClass" for w in windows)


def _is_system(window: WindowInfo, adapter: Adapter) -> bool:
    exe = window.exe.lower()
    if exe in _SYSTEM_EXES:
        return True
    # explorer.exe показывает ещё рабочий стол и панель задач, нужны только окна папок
    return exe == "explorer.exe" and adapter.type_name != "explorer"


def _skip_reason(rt: Runtime, window: WindowInfo, foreign: set[int]) -> str | None:
    if window.hwnd in foreign:
        return "уже принадлежит другому пространству"
    if window.minimized:
        return "окно свёрнуто, у него нет позиции (разверни и повтори)"
    if not window.exe:
        return "нет доступа к процессу (возможно, запущен от администратора)"
    if rt.win.is_elevated(window.pid):
        return "запущено от администратора, ws не может им управлять"
    return None


def _one_line(text: str) -> str:
    return " ".join(text.split())


# --- генерация YAML -----------------------------------------------------------
# PyYAML не умеет комментарии, поэтому текст собирается вручную и проверяется обратным разбором.


def _scalar(value: str) -> str:
    """Читаемая запись строки: без кавычек, если YAML вернёт ровно её; иначе JSON-строка (валидный YAML)."""
    if _PLAIN.fullmatch(value) and not value.endswith(" "):
        try:
            if yaml.safe_load(f"k: {value}") == {"k": value}:
                return value
        except yaml.YAMLError:
            pass
    return json.dumps(value, ensure_ascii=False)


def _window_line(spec: WindowSpec) -> str:
    text = f"window: {{monitor: {spec.monitor}, x: {spec.x}, y: {spec.y}, w: {spec.w}, h: {spec.h}"
    return text + (", maximized: true}" if spec.maximized else "}")


def _item_lines(item: Item) -> list[str]:
    lines = [f"type: {item.type}"]
    if isinstance(item, VSCodeItem):
        lines.append(f"path: {_scalar(str(item.path))}")
        if not item.isolate:
            lines.append("isolate: false")
    elif isinstance(item, TerminalItem):
        assert not item.tabs, "снимок не читает вкладки"
    elif isinstance(item, BrowserItem):
        lines.append(f"browser: {item.browser}")
        if item.profile:
            lines.append(f"profile: {_scalar(item.profile)}")
    elif isinstance(item, ExplorerItem):
        lines.append(f"path: {_scalar(str(item.path))}")
    elif isinstance(item, AppItem):
        lines.append(f"exe: {_scalar(str(item.exe))}")
    else:  # pragma: no cover
        raise SystemFailure(f"неизвестный тип элемента {type(item).__name__}")
    if item.window is not None:
        lines.append(_window_line(item.window))
    return lines


def _render(name: str, now: float, entries: list[_Entry]) -> str:
    stamp = datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M")
    out = [
        f"# Снято командой ws save, {stamp}. Проверь и поправь: ws edit {name}",
        f"name: {name}",
        f'description: "Снято {stamp}"',
        "items:",
    ]
    for entry in entries:
        out.append("")
        out += [f"  # {_one_line(note)}" for note in entry.capture.notes]
        if entry.item is not None:
            first, *rest = _item_lines(entry.item)
            out.append(f"  - {first}")
            out += [f"    {line}" for line in rest]
        else:
            first, *rest = [*entry.capture.stub, _window_line(entry.spec)]
            out.append(f"  # - {first}")
            out += [f"  #   {line}" for line in rest]
    return "\n".join(out) + "\n"


def _self_check(name: str, text: str, items: list[Item]) -> None:
    """Манифест, который мы сами не смогли бы открыть, на диск не пишется."""
    try:
        parsed = manifest.parse(text, "<снимок>", default_name=name)
    except UserError as exc:
        raise SystemFailure(f"внутренняя ошибка: снятый манифест не разбирается ({exc}); файл не записан") from exc
    if parsed.items != tuple(items):
        raise SystemFailure("внутренняя ошибка: снятый манифест читается не так, как был снят; файл не записан")
