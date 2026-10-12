"""Любое exe."""

from __future__ import annotations

from pathlib import Path

from ws.adapters.base import Adapter, CannotCapture, Capture, CaptureContext, LaunchCommand
from ws.config import Runtime
from ws.models import AppItem, Item, Space, WindowInfo


class GenericAdapter(Adapter):
    type_name = "app"
    loose = True  # лаунчеры часто запускают окно из другого exe

    def owns(self, window: WindowInfo) -> bool:
        return True  # запасной вариант: всё, что не опознали остальные

    def capture(self, window: WindowInfo, ctx: CaptureContext) -> Capture:
        if window.class_name == "ApplicationFrameWindow":
            raise CannotCapture("UWP/Store-приложение: запуск по exe не поддерживается")
        proc = ctx.rt.procs.info(window.pid)
        if proc is None or not proc.path:
            raise CannotCapture("не удалось определить путь к exe (нет доступа к процессу)")
        # Аргументы командной строки не сохраняем: в них бывают пароли и токены.
        return Capture(AppItem(exe=Path(proc.path)))

    def launch(self, item: Item, space: Space, rt: Runtime) -> LaunchCommand:
        assert isinstance(item, AppItem)
        cwd = item.exe.parent if item.exe.parent.is_dir() else None
        return LaunchCommand(argv=[str(item.exe), *item.args], cwd=cwd)

    def matches(self, item: Item, window: WindowInfo) -> bool:
        assert isinstance(item, AppItem)
        return window.exe.lower() == item.exe.name.lower()
