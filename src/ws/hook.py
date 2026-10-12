"""PowerShell-хук для заметок «где я остановился». Ставится только явной командой ws hook install."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from ws.errors import SystemFailure, UserError

START = "# >>> ws hook >>>"
END = "# <<< ws hook <<<"

# Только ASCII: профиль может читаться Windows PowerShell 5.1 без указания кодировки.
SNIPPET = f"""{START}
# Records the working directory and the last command of this shell for ws.
# Active only in shells started by ws (it sets WS_SPACE). Remove with: ws hook uninstall
if ($env:WS_SPACE) {{
    if (-not (Test-Path variable:global:__ws_original_prompt)) {{
        $global:__ws_original_prompt = $function:prompt
    }}
    function global:prompt {{
        try {{
            $wsRoot = if ($env:WS_HOME) {{ $env:WS_HOME }} else {{ Join-Path $env:APPDATA 'ws' }}
            $wsDir = Join-Path $wsRoot (Join-Path 'notes' $env:WS_SPACE)
            if (-not (Test-Path -LiteralPath $wsDir)) {{ New-Item -ItemType Directory -Path $wsDir -Force | Out-Null }}
            $wsLast = Get-History -Count 1
            $wsNote = @{{
                pid = $PID
                cwd = (Get-Location).ProviderPath
                command = $(if ($wsLast) {{ $wsLast.CommandLine }} else {{ $null }})
                time = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
            }}
            ConvertTo-Json -InputObject $wsNote -Compress | Set-Content -LiteralPath (Join-Path $wsDir "$PID.json") -Encoding UTF8
        }} catch {{ }}
        & $global:__ws_original_prompt
    }}
}}
{END}
"""

SHELLS = {"powershell": "powershell", "pwsh": "pwsh"}


def _run(shell: str, command: str) -> str:
    exe = shutil.which(SHELLS[shell])
    if exe is None:
        raise UserError(f"{SHELLS[shell]} не найден в PATH")
    result = subprocess.run(
        [exe, "-NoProfile", "-NonInteractive", "-Command", command], capture_output=True, text=True, timeout=30
    )
    if result.returncode != 0:
        raise SystemFailure(f"{SHELLS[shell]}: {result.stderr.strip() or 'ошибка'}")
    return result.stdout.strip()


def profile_path(shell: str) -> Path:
    """Файл профиля текущего пользователя (с учётом перенаправленных в OneDrive Документов)."""
    if shell not in SHELLS:
        raise UserError("--shell: powershell (Windows PowerShell 5.1) или pwsh (PowerShell 7)")
    return Path(_run(shell, "[Console]::OutputEncoding = [Text.Encoding]::UTF8; $PROFILE"))


def execution_policy(shell: str) -> str:
    return _run(shell, "Get-ExecutionPolicy")


def has_block(text: str) -> bool:
    return START in text and END in text


def remove_block(text: str) -> str:
    if not has_block(text):
        return text
    before, rest = text.split(START, 1)
    _, after = rest.split(END, 1)
    before, after = before.rstrip(), after.strip()
    if before and after:
        return before + "\n\n" + after + "\n"
    return (before or after) + "\n" if (before or after) else ""


def add_block(text: str) -> str:
    """Добавить блок в конец; уже стоящий блок заменяется (повторный install безопасен)."""
    text = remove_block(text)
    separator = "" if not text else ("\n" if text.endswith("\n") else "\n\n")
    return text + separator + SNIPPET


def _read(path: Path) -> tuple[str, bool]:
    if not path.exists():
        return "", True
    raw = path.read_bytes()
    bom = raw.startswith(b"\xef\xbb\xbf")
    try:
        return raw.decode("utf-8-sig"), bom
    except UnicodeDecodeError as exc:
        raise UserError(f"{path} не в кодировке UTF-8; чтобы не повредить файл, добавь блок вручную (ws hook show)") from exc


def _write(path: Path, text: str, bom: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8-sig" if bom else "utf-8"))


def install(path: Path) -> Path | None:
    """Вернуть путь резервной копии (None, если профиля раньше не было)."""
    text, bom = _read(path)
    backup = None
    if path.exists():
        backup = path.with_name(path.name + ".ws-backup")
        if not backup.exists():  # первая копия хранит исходный профиль, повторные установки её не затирают
            shutil.copy2(path, backup)
    _write(path, add_block(text), bom)
    return backup


def uninstall(path: Path) -> bool:
    if not path.exists():
        return False
    text, bom = _read(path)
    if not has_block(text):
        return False
    _write(path, remove_block(text), bom)
    return True
