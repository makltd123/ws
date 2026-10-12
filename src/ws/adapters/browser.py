"""Chrome и Edge. Профиль на пространство даёт отдельную группу процессов."""

from __future__ import annotations

from pathlib import Path

from ws.adapters.base import (
    Adapter,
    Capture,
    CaptureContext,
    Issue,
    LaunchCommand,
    env_path,
    find_executable,
    option_value,
)
from ws.config import Runtime
from ws.errors import SystemFailure
from ws.models import BrowserItem, Item, Space, WindowInfo

_EXE = {"chrome": "chrome.exe", "edge": "msedge.exe"}
_NAME_BY_EXE = {exe: name for name, exe in _EXE.items()}


def find_browser(browser: str) -> Path | None:
    program_files = env_path("PROGRAMFILES")
    program_files_x86 = env_path("PROGRAMFILES(X86)")
    local = env_path("LOCALAPPDATA")
    rel = {
        "chrome": Path("Google") / "Chrome" / "Application" / "chrome.exe",
        "edge": Path("Microsoft") / "Edge" / "Application" / "msedge.exe",
    }[browser]
    candidates = [base / rel for base in (program_files, program_files_x86, local) if base is not None]
    return find_executable(_EXE[browser], candidates)


class BrowserAdapter(Adapter):
    type_name = "browser"

    def owns(self, window: WindowInfo) -> bool:
        return window.exe.lower() in _NAME_BY_EXE and window.class_name == "Chrome_WidgetWin_1"

    def capture(self, window: WindowInfo, ctx: CaptureContext) -> Capture:
        browser = _NAME_BY_EXE[window.exe.lower()]
        proc = ctx.rt.procs.info(window.pid)
        user_data = option_value(proc.cmdline, "--user-data-dir") if proc else None
        notes = ["Открытые вкладки из запущенного браузера не читаются. Перечисли нужные адреса вручную: urls: [...]"]
        profile = None
        if user_data:
            data_dir = Path(user_data)
            if data_dir.parent == ctx.rt.paths.profiles:
                profile = data_dir.name
            else:
                notes.append(
                    f"Браузер запущен с --user-data-dir={user_data}; ws поддерживает профили только "
                    f"в {ctx.rt.paths.profiles}, поэтому окно описано без профиля."
                )
        return Capture(BrowserItem(browser=browser, profile=profile), notes=tuple(notes))

    def check(self, rt: Runtime) -> list[Issue]:
        issues = []
        for name in ("chrome", "edge"):
            path = find_browser(name)
            issues.append(Issue("ok", f"{name}: {path}") if path else Issue("warn", f"{name} не найден"))
        return issues

    def launch(self, item: Item, space: Space, rt: Runtime) -> LaunchCommand:
        assert isinstance(item, BrowserItem)
        exe = find_browser(item.browser)
        if exe is None:
            raise SystemFailure(f"{item.browser} не найден (проверь ws doctor)")
        argv = [str(exe)]
        if item.profile:
            profile_dir = rt.paths.profiles / item.profile
            profile_dir.mkdir(parents=True, exist_ok=True)
            argv += [f"--user-data-dir={profile_dir}", "--no-first-run", "--no-default-browser-check"]
        argv.append("--new-window")
        argv += list(item.urls)
        return LaunchCommand(argv=argv)

    def matches(self, item: Item, window: WindowInfo) -> bool:
        assert isinstance(item, BrowserItem)
        return window.exe.lower() == _EXE[item.browser] and window.class_name == "Chrome_WidgetWin_1"
