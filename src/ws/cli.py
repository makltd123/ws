"""CLI. Только разбор аргументов и вывод; вся логика в сценариях."""

from __future__ import annotations

import functools
import logging
import os
import shlex
import subprocess
import sys
from typing import Callable, Optional

import typer

from ws import manifest, snapshot, switcher
from ws import state as state_mod
from ws.adapters import ADAPTERS
from ws.config import Runtime, SystemClock, default_paths, load_config, setup_logging
from ws.errors import SystemFailure, UserError, WsError
from ws.procs import PsutilProcessSystem
from ws.winapi import Win32WindowSystem, named_mutex

log = logging.getLogger(__name__)

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Контекстные рабочие пространства для Windows.")


def build_runtime() -> Runtime:
    paths = default_paths()
    paths.ensure()
    setup_logging(paths)
    win = Win32WindowSystem()
    win.enable_dpi_awareness()  # до любых операций с координатами
    return Runtime(
        paths=paths,
        config=load_config(paths),
        win=win,
        procs=PsutilProcessSystem(),
        clock=SystemClock(),
        lock=lambda: named_mutex("Local\\ws-state"),
    )


def _guard(fn: Callable[..., None]) -> Callable[..., None]:
    """Коды возврата: 0 успех, 1 ошибка пользователя, 2 системная ошибка."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs) -> None:
        try:
            fn(*args, **kwargs)
        except (typer.Exit, typer.Abort):
            raise
        except WsError as exc:
            typer.echo(f"Ошибка: {exc}", err=True)
            raise typer.Exit(exc.exit_code)
        except Exception as exc:
            log.exception("необработанная ошибка")
            typer.echo(f"Системная ошибка: {type(exc).__name__}: {exc} (подробности в ws.log)", err=True)
            raise typer.Exit(2)

    return wrapper


@app.command("list")
@_guard
def list_() -> None:
    """Пространства и их статус."""
    rt = build_runtime()
    names = manifest.list_names(rt.paths)
    if not names:
        typer.echo(f"Пространств пока нет. Создай первое: ws edit <имя>  (папка {rt.paths.spaces})")
        return
    state = state_mod.load_pruned(rt)
    repos = manifest.registered_repos(rt.paths)
    for name in names:
        sp = state.spaces.get(name)
        if state.active == name:
            status = "active"
        elif sp is not None and sp.frozen:
            status = "frozen"
        elif sp is not None and sp.windows:
            status = "running"
        else:
            status = "not running"
        origin = f"  [репозиторий: {repos[name].parent}]" if name in repos and not manifest.manifest_path(rt.paths, name).exists() else ""
        typer.echo(f"{name:<24}{status}{origin}")


@app.command("open")
@_guard
def open_(name: str = typer.Argument(..., help="Имя пространства или путь к репозиторию с .workspace.yml (например, .)")) -> None:
    """Запустить пространство или показать его окна, если оно уже запущено."""
    _print_switch(name, switcher.activate_named(build_runtime(), name))


def _print_switch(name: str, report: switcher.SwitchReport) -> None:
    opened = report.opened
    line = f"{report.name or name}: запущено {len(opened.launched)}, уже было {len(opened.reused)}"
    if report.thawed is not None:
        line += f", разморожено процессов {report.thawed.resumed}"
    typer.echo(line)
    if report.frozen is not None:
        typer.echo(f"  предыдущее пространство: заморожено процессов {report.frozen.suspended}, спрятано окон {report.frozen.hidden + report.frozen.minimized}")
    if report.note:
        typer.echo(f"  ↩ {report.note}")
    for warning in report.all_warnings:
        typer.echo(f"  ! {warning}")


@app.command("switch")
@_guard
def switch(name: str = typer.Argument(..., help="Имя пространства")) -> None:
    """Заморозить активное пространство и открыть указанное."""
    _print_switch(name, switcher.switch_named(build_runtime(), name))


@app.command("freeze")
@_guard
def freeze(name: Optional[str] = typer.Argument(None, help="Имя пространства (по умолчанию активное)")) -> None:
    """Заморозить активное или указанное пространство: окна прячутся, процессы останавливаются."""
    result = switcher.freeze_named(build_runtime(), name)
    report = result.report
    typer.echo(f"{result.name}: заморожено процессов {report.suspended}, спрятано окон {report.hidden}, свёрнуто {report.minimized}")
    for warning in result.warnings:
        typer.echo(f"  ! {warning}")


@app.command("thaw")
@_guard
def thaw(name: str = typer.Argument(..., help="Имя пространства")) -> None:
    """Разморозить пространство без переключения (остальные окна не трогаются)."""
    report = switcher.thaw_named(build_runtime(), name)
    typer.echo(f"{name}: разморожено процессов {report.resumed}, показано окон {report.shown}")
    if report.note:
        typer.echo(f"  ↩ {report.note}")
    for warning in report.warnings:
        typer.echo(f"  ! {warning}")


@app.command("tray")
@_guard
def tray(
    background: bool = typer.Option(False, "--background", "-b", help="Запуститься без консоли и сразу вернуть управление"),
    stop: bool = typer.Option(False, "--stop", help="Остановить уже запущенный ws tray"),
) -> None:
    """Значок в трее со списком пространств и глобальные горячие клавиши (Ctrl+Alt+1..9)."""
    from ws import tray as tray_mod

    if stop:
        typer.echo("Сигнал остановки отправлен" if tray_mod.request_stop() else "ws tray не запущен")
        return
    if tray_mod.is_running():
        raise UserError("ws tray уже запущен (остановить: ws tray --stop)")
    if background:
        tray_mod.start_detached()
        typer.echo("ws tray запущен в фоне (остановить: ws tray --stop)")
        return
    tray_mod.run_tray(build_runtime())


@app.command("close")
@_guard
def close_(
    name: str = typer.Argument(..., help="Имя пространства"),
    force: bool = typer.Option(False, "--force", help="Если окно не закрылось, предложить завершить его процесс"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Не спрашивать подтверждение закрытия"),
) -> None:
    """Закрыть окна пространства (WM_CLOSE) с подтверждением. Процессы не убиваются без --force и вопроса."""
    rt = build_runtime()
    peek = state_mod.load_pruned(rt)
    switcher.require_running(peek, name)
    if not yes:
        count = len(peek.spaces[name].windows)
        typer.confirm(f"Закрыть окна пространства '{name}' ({count})? Несохранённое может потеряться", abort=True)

    def confirm_kill(labels: list[str]) -> bool:
        listing = "\n".join(f"    {label}" for label in labels)
        return typer.confirm(f"Окна не закрылись. Принудительно завершить процессы? Несохранённые данные пропадут:\n{listing}")

    with state_mod.locked(rt):
        state = state_mod.load_pruned(rt)
        report = switcher.close(rt, name, state, confirm_kill=confirm_kill if force else None)
    typer.echo(f"{name}: закрыто окон {report.closed}")
    for label in report.terminated:
        typer.echo(f"  завершён: {label}")
    for line in report.not_closed + report.warnings:
        typer.echo(f"  ! {line}")


@app.command("save")
@_guard
def save(
    name: str = typer.Argument(..., help="Имя нового пространства"),
    force: bool = typer.Option(False, "--force", help="Перезаписать существующий манифест (старый сохранится как .bak)"),
) -> None:
    """Снять раскладку текущих видимых окон в новый манифест."""
    rt = build_runtime()
    with state_mod.locked(rt):
        state = state_mod.load_pruned(rt)
        report = snapshot.save_space(rt, name, state, force=force)
        state_mod.save(rt.paths, state)
    typer.echo(f"Манифест записан: {report.path}")
    typer.echo(f"  в манифесте: {len(report.captured)}, закомментировано: {len(report.commented)}, пропущено: {len(report.skipped)}")
    for line in report.captured:
        typer.echo(f"  + {line}")
    for line in report.commented:
        typer.echo(f"  ~ {line}")
    for line in report.skipped:
        typer.echo(f"  - {line}")
    if report.backup:
        typer.echo(f"  Старый манифест сохранён: {report.backup}")
    typer.echo(f"Лишнее удали через: ws edit {name}")


@app.command("edit")
@_guard
def edit(name: str = typer.Argument(..., help="Имя пространства")) -> None:
    """Открыть манифест в $EDITOR или в блокноте (новый создаётся из шаблона)."""
    rt = build_runtime()
    path = manifest.editable_path(rt.paths, name)
    if not path.exists():
        path.write_text(manifest.TEMPLATE.format(name=name), encoding="utf-8")
        typer.echo(f"Создан шаблон {path}")
    editor = shlex.split(os.environ.get("EDITOR") or "notepad", posix=False)
    try:
        subprocess.Popen([*editor, str(path)])
    except OSError as exc:
        raise SystemFailure(f"не удалось запустить редактор {editor[0]}: {exc}") from exc


@app.command("status")
@_guard
def status() -> None:
    """Что сейчас активно."""
    rt = build_runtime()
    state = state_mod.load_pruned(rt)
    typer.echo(f"Активное пространство: {state.active or '-'}")
    for name, sp in sorted(state.spaces.items()):
        if not sp.windows:
            continue
        stopped = sum(1 for ref in sp.frozen_procs if (info := rt.procs.info(ref.pid)) and info.status == "stopped")
        mark = " [заморожено]" if sp.frozen else ""
        typer.echo(f"  {name}{mark}: окон {len(sp.windows)}, заморожено процессов {len(sp.frozen_procs)} (psutil видит остановленными: {stopped})")


@app.command("doctor")
@_guard
def doctor() -> None:
    """Проверить окружение: wt, code, браузеры, DPI, права."""
    rt = build_runtime()
    mark = {"ok": "[ok]  ", "warn": "[!]   ", "error": "[ERR] "}

    def line(level: str, message: str) -> None:
        typer.echo(mark[level] + message)

    line("ok", f"Python {sys.version.split()[0]}")
    line("ok", f"данные: {rt.paths.root}")
    dpi_ok = rt.win.enable_dpi_awareness()
    line("ok" if dpi_ok else "warn", "per-monitor DPI awareness " + ("включён" if dpi_ok else "не включился, координаты могут съехать"))
    if getattr(rt.win, "self_elevated", False):
        line("warn", "ws запущен от администратора; обычные окна пользователя могут не управляться")
    else:
        line("ok", "ws запущен без прав администратора")
    for monitor in rt.win.monitors():
        w, h = monitor.work.width, monitor.work.height
        line("ok", f"монитор {monitor.index}{' (основной)' if monitor.primary else ''}: {w}x{h}, {monitor.dpi} dpi")
    for adapter in ADAPTERS.values():
        for issue in adapter.check(rt):
            line(issue.level, issue.message)
    state = state_mod.load_pruned(rt)
    leftovers = [(n, sp) for n, sp in sorted(state.spaces.items()) if sp.frozen]
    for name, sp in leftovers:
        line("warn", f"пространство '{name}' заморожено ({len(sp.frozen_procs)} процессов, окна спрятаны). Вернуть: ws thaw {name}")
    if not leftovers:
        line("ok", "замороженных пространств нет")


hook_app = typer.Typer(no_args_is_help=True, help="PowerShell-хук для заметок «где я остановился» (ставится только по явной команде).")
app.add_typer(hook_app, name="hook")


@hook_app.command("show")
@_guard
def hook_show(shell: str = typer.Option("powershell", "--shell", help="powershell (5.1) или pwsh (7)")) -> None:
    """Показать блок, который добавит install, и файл профиля. Ничего не меняет."""
    from ws import hook

    path = hook.profile_path(shell)
    typer.echo(f"Профиль {shell}: {path}")
    typer.echo(f"Политика выполнения скриптов: {hook.execution_policy(shell)} (при Restricted профиль не загружается)\n")
    typer.echo(hook.SNIPPET)


@hook_app.command("install")
@_guard
def hook_install(
    shell: str = typer.Option("powershell", "--shell", help="powershell (5.1) или pwsh (7)"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Не спрашивать подтверждение"),
) -> None:
    """Добавить хук в профиль PowerShell (с резервной копией). Повторная установка безопасна."""
    from ws import hook

    path = hook.profile_path(shell)
    if not yes:
        typer.confirm(f"Добавить блок ws в {path}? (ws hook show покажет его; убрать: ws hook uninstall)", abort=True)
    backup = hook.install(path)
    typer.echo(f"Хук установлен в {path}")
    if backup:
        typer.echo(f"  Резервная копия исходного профиля: {backup}")
    policy = hook.execution_policy(shell)
    if policy in ("Restricted", "AllSigned", "Undefined"):
        typer.echo(f"  ! Политика выполнения скриптов: {policy}; профиль не загрузится, пока её не разрешить (Set-ExecutionPolicy -Scope CurrentUser RemoteSigned). ws её сам не меняет.")
    typer.echo("  Работает в оболочках, которые запустил ws (вкладки терминала и терминалы VS Code пространства).")


@hook_app.command("uninstall")
@_guard
def hook_uninstall(shell: str = typer.Option("powershell", "--shell", help="powershell (5.1) или pwsh (7)")) -> None:
    """Убрать блок ws из профиля PowerShell."""
    from ws import hook

    path = hook.profile_path(shell)
    typer.echo("Хук удалён" if hook.uninstall(path) else f"В {path} хука ws нет")


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    app()


if __name__ == "__main__":
    main()
