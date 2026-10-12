"""ws tray: значок в трее и глобальные горячие клавиши.

TrayController не знает ни про pystray, ни про Win32, поэтому проверяется тестами на фейках.
Переключение из трея и по хоткею идёт через те же switcher.switch_named / freeze_named, что и CLI.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ws import hotkeys, manifest, switcher
from ws import state as state_mod
from ws.config import Runtime, load_config
from ws.errors import SystemFailure, UserError, WsError
from ws.hotkeys import Bindings, Hotkey
from ws.models import State
from ws.triggers import BranchWatcher, Suggestion
from ws.winapi import (
    Win32Hotkeys,
    acquire_single_instance,
    create_event,
    signal_event,
    single_instance_exists,
    wait_event,
)

log = logging.getLogger(__name__)

SINGLE_NAME = "Local\\ws-tray"
QUIT_EVENT = "Local\\ws-tray-quit"
_TICK = 0.5  # как часто проверяется сигнал остановки
_WATCH_EVERY = 6  # каждые _TICK * _WATCH_EVERY секунд сверяется, не изменились ли манифесты и конфиг


@dataclass(frozen=True)
class SpaceEntry:
    name: str
    label: str
    checked: bool  # активное пространство


class TrayController:
    def __init__(
        self,
        rt: Runtime,
        notify: Callable[[str, str], None],
        submit: Callable[[Callable[[], None]], object],
        on_change: Callable[[], None] = lambda: None,
    ) -> None:
        self.rt = rt
        self._notify = notify
        self._submit = submit  # действия идут по одному в служебном потоке, UI не блокируется
        self._on_change = on_change
        self.bindings = Bindings()
        self._last_warnings: list[str] = []
        self.watcher = BranchWatcher()
        self.suggestions: dict[str, Suggestion] = {}  # пространство -> подсказка о смене ветки

    # --- горячие клавиши
    def refresh_bindings(self) -> list[tuple[int, Hotkey]]:
        """Пересчитать назначения и вернуть их в виде (id, комбинация) для регистрации."""
        self.bindings = hotkeys.assign(manifest.list_names(self.rt.paths), self.rt.config.hotkeys)
        if self.bindings.warnings != self._last_warnings:  # не повторять одно и то же при каждой проверке
            self._last_warnings = list(self.bindings.warnings)
            for warning in self.bindings.warnings:
                self._notify("ws: горячие клавиши", warning)
        return [(i, b.hotkey) for i, b in enumerate(self.bindings.items, start=1)]

    def on_hotkey(self, hotkey_id: int) -> None:
        if 1 <= hotkey_id <= len(self.bindings.items):
            self.switch(self.bindings.items[hotkey_id - 1].space)

    # --- меню
    def entries(self) -> list[SpaceEntry]:
        try:
            state = state_mod.load_pruned(self.rt)
        except Exception:
            log.exception("не удалось прочитать состояние для меню")
            state = State()
        result = []
        for name in manifest.list_names(self.rt.paths):
            label = name
            if (combo := self.bindings.label_for(name)) is not None:
                label += f"    {combo}"
            sp = state.spaces.get(name)
            if sp is not None and sp.frozen:
                label += "    (заморожено)"
            result.append(SpaceEntry(name, label, checked=state.active == name))
        return result

    def tooltip(self) -> str:
        try:
            active = state_mod.load_pruned(self.rt).active
        except Exception:
            active = None
        return f"ws: {active}" if active else "ws: нет активного пространства"

    # --- действия
    def switch(self, name: str) -> None:
        self.suggestions.pop(name, None)  # выбрали пространство, подсказка выполнена

        def job() -> tuple[str | None, list[str]]:
            report = switcher.switch_named(self.rt, name)
            return report.note, report.all_warnings

        self._submit(lambda: self._run(f"ws: {name}", job))

    def freeze_active(self) -> None:
        def job() -> tuple[str | None, list[str]]:
            return None, switcher.freeze_named(self.rt).warnings

        self._submit(lambda: self._run("ws: заморозка", job))

    # --- подсказки по ветке git
    def check_triggers(self) -> None:
        """Раз в несколько секунд: сменилась ли ветка в репозитории пространства с triggers.git_branch."""
        spaces = []
        for name in manifest.list_names(self.rt.paths):
            try:
                space = manifest.load(self.rt.paths, name)
            except WsError:
                continue  # сломанный манифест не должен останавливать остальные проверки
            if space.triggers is not None:
                spaces.append(space)
        try:
            active = state_mod.load_pruned(self.rt).active
        except Exception:
            active = None
        before = dict(self.suggestions)
        for suggestion in self.watcher.check(spaces, active):
            self.suggestions[suggestion.space] = suggestion
            self._notify(
                f"ws: ветка {suggestion.branch}",
                f"Пространство '{suggestion.space}' подходит к этой ветке. Выбери его в меню значка ws.",
            )
        by_name = {s.name: s for s in spaces}
        for name in list(self.suggestions):
            space = by_name.get(name)
            if space is None or name == active or not self.watcher.still_matches(space):
                del self.suggestions[name]  # переключились сами или ветку сменили обратно
        if self.suggestions != before:
            self._on_change()

    def _run(self, title: str, action: Callable[[], tuple[str | None, list[str]]]) -> None:
        try:
            note, warnings = action()
            lines = [note] if note else []
            if warnings:
                more = f" (и ещё {len(warnings) - 1})" if len(warnings) > 1 else ""
                lines.append(f"! {warnings[0]}{more}")
            if lines:
                self._notify(title, "\n".join(lines))
        except WsError as exc:
            self._notify("ws: ошибка", str(exc))
        except Exception as exc:
            log.exception("действие из трея")
            self._notify("ws: системная ошибка", f"{type(exc).__name__}: {exc}")
        finally:
            self._on_change()


# --- запуск ------------------------------------------------------------------


def is_running() -> bool:
    return single_instance_exists(SINGLE_NAME)


def request_stop() -> bool:
    return signal_event(QUIT_EVENT)


def start_detached() -> None:
    """Запустить ws tray отдельным процессом без консоли и дождаться, что он поднялся."""
    python = Path(sys.executable)
    pythonw = python.with_name("pythonw.exe")
    if pythonw.exists():
        argv, flags = [str(pythonw), "-m", "ws", "tray"], subprocess.DETACHED_PROCESS
    else:
        argv, flags = [str(python), "-m", "ws", "tray"], subprocess.CREATE_NO_WINDOW
    subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags | subprocess.CREATE_NEW_PROCESS_GROUP,
        close_fds=True,
    )
    for _ in range(50):
        if is_running():
            return
        threading.Event().wait(0.1)
    raise SystemFailure("ws tray не запустился за 5 секунд (подробности в ws.log)")


def make_icon(size: int = 64):
    """Четыре «окна» на тёмной плитке, одно подсвечено."""
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    k = size / 64
    draw.rounded_rectangle((2 * k, 2 * k, 61 * k, 61 * k), radius=12 * k, fill=(30, 41, 59, 255))
    cells = [(10, 10, 30, 30), (34, 10, 54, 30), (10, 34, 30, 54), (34, 34, 54, 54)]
    colors = [(96, 165, 250), (148, 163, 184), (148, 163, 184), (148, 163, 184)]
    for (x0, y0, x1, y1), color in zip(cells, colors):
        draw.rounded_rectangle((x0 * k, y0 * k, x1 * k, y1 * k), radius=4 * k, fill=(*color, 255))
    return img


def build_menu_items(controller: TrayController, on_refresh: Callable[[], None], on_quit: Callable[[], None]):
    """Пункты меню: пространства, затем «Заморозить активное», «Обновить», «Выход»."""
    import pystray

    def space_action(name: str):
        # фабрика нужна, чтобы замкнуть name; pystray различает действия по числу аргументов (icon, item)
        def action(icon, item) -> None:
            controller.switch(name)

        return action

    def is_checked(value: bool):
        return lambda item: value

    for suggestion in list(controller.suggestions.values()):
        yield pystray.MenuItem(
            f"→ Перейти в «{suggestion.space}» (ветка {suggestion.branch})", space_action(suggestion.space)
        )
    if controller.suggestions:
        yield pystray.Menu.SEPARATOR
    entries = controller.entries()
    if not entries:
        yield pystray.MenuItem("Нет пространств (ws edit <имя>)", None, enabled=False)
    for entry in entries:
        yield pystray.MenuItem(entry.label, space_action(entry.name), checked=is_checked(entry.checked))
    yield pystray.Menu.SEPARATOR
    yield pystray.MenuItem("Заморозить активное", lambda icon, item: controller.freeze_active())
    yield pystray.MenuItem("Обновить", lambda icon, item: on_refresh())
    yield pystray.MenuItem("Выход", lambda icon, item: on_quit())


def run_tray(rt: Runtime) -> None:
    """Блокирующий цикл трея. Возвращается, когда выбран «Выход» или пришёл ws tray --stop."""
    import pystray

    instance = acquire_single_instance(SINGLE_NAME)
    if instance is None:
        raise UserError("ws tray уже запущен (остановить: ws tray --stop)")
    quit_event = create_event(QUIT_EVENT)
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ws-action")
    stop = threading.Event()
    icon_holder: list[pystray.Icon] = []

    def notify(title: str, message: str) -> None:
        log.info("уведомление: %s: %s", title, message)
        if icon_holder:
            try:
                icon_holder[0].notify(message[:250], title[:60])
            except Exception:
                log.exception("не удалось показать уведомление")

    def refresh_ui() -> None:
        if icon_holder:
            icon_holder[0].title = controller.tooltip()[:120]
            icon_holder[0].update_menu()

    controller = TrayController(rt, notify, executor.submit, on_change=refresh_ui)
    hotkey_service = Win32Hotkeys(controller.on_hotkey, lambda message: notify("ws: горячие клавиши", message))

    def menu_items():
        return build_menu_items(controller, lambda: apply_changes(force=True), lambda: icon.stop())

    icon = pystray.Icon("ws", make_icon(), "ws", menu=pystray.Menu(menu_items))
    icon_holder.append(icon)

    def signature() -> tuple:
        return (
            tuple(manifest.list_names(rt.paths)),
            mtime_of(rt.paths.config_file),
            mtime_of(rt.paths.state_file),
        )

    last = {"sig": None, "config": mtime_of(rt.paths.config_file)}

    def apply_changes(force: bool = False) -> None:
        sig = signature()
        if not force and sig == last["sig"]:
            return
        if sig[1] != last["config"]:
            last["config"] = sig[1]
            try:
                rt.config = load_config(rt.paths)
            except WsError as exc:
                notify("ws: config.yml", str(exc))
        last["sig"] = sig
        hotkey_service.set_bindings(controller.refresh_bindings())
        refresh_ui()

    def background() -> None:
        ticks = 0
        while not stop.is_set():
            if wait_event(quit_event, _TICK):
                icon.stop()
                return
            ticks += 1
            if ticks % _WATCH_EVERY == 0:
                try:
                    apply_changes()
                    controller.check_triggers()
                except Exception:
                    log.exception("проверка изменений")

    def setup(icon_: pystray.Icon) -> None:
        icon_.visible = True
        hotkey_service.start()
        apply_changes(force=True)
        threading.Thread(target=background, name="ws-watch", daemon=True).start()
        log.info("ws tray запущен: %s", ", ".join(f"{b.hotkey.label}={b.space}" for b in controller.bindings.items))

    try:
        icon.run(setup=setup)
    finally:
        stop.set()
        hotkey_service.stop()
        executor.shutdown(wait=True)
        instance.Close()
        quit_event.Close()
        log.info("ws tray остановлен")


def mtime_of(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0
