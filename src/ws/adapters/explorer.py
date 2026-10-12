"""Проводник."""

from __future__ import annotations

from ws.adapters.base import Adapter, CannotCapture, Capture, CaptureContext, LaunchCommand
from ws.config import Runtime
from ws.errors import UserError
from ws.models import ExplorerItem, Item, Space, WindowInfo


class ExplorerAdapter(Adapter):
    type_name = "explorer"

    def owns(self, window: WindowInfo) -> bool:
        return window.exe.lower() == "explorer.exe" and window.class_name == "CabinetWClass"

    def capture(self, window: WindowInfo, ctx: CaptureContext) -> Capture:
        folder = ctx.explorer_folders.get(window.hwnd)
        if folder is None:
            raise CannotCapture("папка окна Проводника не определена (виртуальная папка или вкладки)")
        return Capture(ExplorerItem(path=folder))

    def launch(self, item: Item, space: Space, rt: Runtime) -> LaunchCommand:
        assert isinstance(item, ExplorerItem)
        if not item.path.is_dir():
            raise UserError(f"папка не найдена: {item.path}")
        return LaunchCommand(argv=["explorer.exe", str(item.path)])

    def matches(self, item: Item, window: WindowInfo) -> bool:
        return window.exe.lower() == "explorer.exe" and window.class_name == "CabinetWClass"
