"""Windows Terminal. Раскладка вкладок задаётся только манифестом."""

from __future__ import annotations

import shutil

from ws.adapters.base import Adapter, Capture, CaptureContext, Issue, LaunchCommand
from ws.config import Runtime
from ws.errors import SystemFailure
from ws.models import Item, Space, TerminalItem, TerminalTab, WindowInfo


def _esc(text: str) -> str:
    # В командной строке wt «;» разделяет подкоманды, буквальная точка с запятой пишется «\;».
    return text.replace(";", "\\;")


def _tab_args(tab: TerminalTab) -> list[str]:
    args = ["new-tab", "-d", _esc(str(tab.dir))]
    if tab.title:
        args += ["--title", _esc(tab.title), "--suppressApplicationTitle"]
    if tab.cmd:
        # -NoExit: после команды вкладка остаётся с рабочей оболочкой.
        args += ["powershell.exe", "-NoExit", "-Command", _esc(tab.cmd)]
    return args


def build_argv(item: TerminalItem, wt: str = "wt.exe") -> list[str]:
    """Командная строка wt. Каждый элемент списка станет одним аргументом процесса."""
    argv = [wt, "-w", "new"]
    for i, tab in enumerate(item.tabs):
        if i > 0:
            argv.append(";")
        argv += _tab_args(tab)
    return argv


class TerminalAdapter(Adapter):
    type_name = "terminal"

    def owns(self, window: WindowInfo) -> bool:
        return window.exe.lower() == "windowsterminal.exe" and window.class_name == "CASCADIA_HOSTING_WINDOW_CLASS"

    def capture(self, window: WindowInfo, ctx: CaptureContext) -> Capture:
        # Вкладки из запущенного терминала не прочитать: сохраняется только позиция окна.
        return Capture(
            TerminalItem(),
            notes=(
                "Вкладки терминала из запущенного окна не читаются. Перечисли их вручную, например:",
                'tabs: [{dir: C:\\proj, cmd: "npm run dev", title: dev}]',
            ),
        )

    def check(self, rt: Runtime) -> list[Issue]:
        wt = shutil.which("wt")
        return [Issue("ok", f"wt: {wt}") if wt else Issue("warn", "wt.exe (Windows Terminal) не найден")]

    def launch(self, item: Item, space: Space, rt: Runtime) -> LaunchCommand:
        assert isinstance(item, TerminalItem)
        wt = shutil.which("wt")
        if wt is None:
            raise SystemFailure("wt.exe (Windows Terminal) не найден (проверь ws doctor)")
        return LaunchCommand(argv=build_argv(item, wt))

    def matches(self, item: Item, window: WindowInfo) -> bool:
        return window.exe.lower() == "windowsterminal.exe" and window.class_name == "CASCADIA_HOSTING_WINDOW_CLASS"
