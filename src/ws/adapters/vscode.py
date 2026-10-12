"""VS Code. По умолчанию отдельный экземпляр на пространство (--user-data-dir)."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from ws.adapters.base import Adapter, Capture, CaptureContext, Issue, LaunchCommand, env_path, option_value
from ws.config import Runtime
from ws.errors import SystemFailure, UserError
from ws.models import Item, Space, VSCodeItem, WindowInfo


def find_code_exe() -> Path | None:
    on_path = shutil.which("code")
    if on_path:
        # ...\Microsoft VS Code\bin\code.cmd -> ...\Microsoft VS Code\Code.exe
        candidate = Path(on_path).resolve().parent.parent / "Code.exe"
        if candidate.exists():
            return candidate
    for base in (env_path("LOCALAPPDATA"), env_path("PROGRAMFILES")):
        if base is None:
            continue
        for rel in (Path("Programs") / "Microsoft VS Code" / "Code.exe", Path("Microsoft VS Code") / "Code.exe"):
            if (base / rel).exists():
                return base / rel
    return None


def _seed_settings(profile: Path) -> None:
    """Скопировать настройки и сочетания клавиш основного профиля, если у экземпляра их ещё нет."""
    appdata = env_path("APPDATA")
    if appdata is None:
        return
    source = appdata / "Code" / "User"
    target = profile / "User"
    target.mkdir(parents=True, exist_ok=True)
    for name in ("settings.json", "keybindings.json"):
        if (source / name).exists() and not (target / name).exists():
            shutil.copy2(source / name, target / name)


_VALUE_FLAGS = {"--user-data-dir", "--extensions-dir"}


def folder_from_cmdline(cmdline: tuple[str, ...]) -> Path | None:
    """Папка, переданная VS Code позиционным аргументом (первого экземпляра или запущенного через ws)."""
    skip_next = False
    for arg in cmdline[1:]:
        if skip_next:
            skip_next = False
        elif arg in _VALUE_FLAGS:
            skip_next = True
        elif not arg.startswith("-") and os.path.exists(arg):
            return Path(arg)
    return None


class VSCodeAdapter(Adapter):
    type_name = "vscode"

    def owns(self, window: WindowInfo) -> bool:
        return window.exe.lower() == "code.exe" and window.class_name == "Chrome_WidgetWin_1"

    def capture(self, window: WindowInfo, ctx: CaptureContext) -> Capture:
        proc = ctx.rt.procs.info(window.pid)
        cmdline = proc.cmdline if proc else ()
        isolate = option_value(cmdline, "--user-data-dir") is not None
        folder = folder_from_cmdline(cmdline)
        if folder is not None:
            return Capture(VSCodeItem(path=folder, isolate=isolate))
        title = " ".join(window.title.split())
        return Capture(
            item=None,
            notes=(
                f"VS Code: путь папки не определён по командной строке (заголовок окна: {title}).",
                "Впиши path вручную и раскомментируй блок.",
            ),
            stub=("type: vscode", "path: TODO") + (() if isolate else ("isolate: false",)),
        )

    def check(self, rt: Runtime) -> list[Issue]:
        exe = find_code_exe()
        return [Issue("ok", f"vscode: {exe}") if exe else Issue("warn", "VS Code не найден")]

    def launch(self, item: Item, space: Space, rt: Runtime) -> LaunchCommand:
        assert isinstance(item, VSCodeItem)
        exe = find_code_exe()
        if exe is None:
            raise SystemFailure("VS Code не найден (проверь ws doctor)")
        if not item.path.exists():
            raise UserError(f"путь не найден: {item.path}")
        argv = [str(exe), "--new-window"]
        if item.isolate:
            profile = rt.paths.profiles / f"vscode-{space.name}"
            _seed_settings(profile)
            extensions = Path.home() / ".vscode" / "extensions"
            argv += ["--user-data-dir", str(profile), "--extensions-dir", str(extensions)]
        argv.append(str(item.path))
        # Из терминала VS Code эти переменные заставили бы новый Code.exe подключиться
        # к уже запущенному экземпляру или запуститься как обычный Node.
        env_remove = tuple(k for k in os.environ if k == "ELECTRON_RUN_AS_NODE" or k.startswith("VSCODE_"))
        return LaunchCommand(argv=argv, env_remove=env_remove)

    def matches(self, item: Item, window: WindowInfo) -> bool:
        return window.exe.lower() == "code.exe" and window.class_name == "Chrome_WidgetWin_1"
