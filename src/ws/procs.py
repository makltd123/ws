"""Процессы: единственный модуль, где вызывается psutil."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Callable, Mapping, Protocol, Sequence

import psutil

from ws.models import ProcInfo, ProcRef
from ws.state import same_process


class ProcessSystem(Protocol):
    def info(self, pid: int) -> ProcInfo | None: ...
    def tree(self, pid: int) -> list[ProcInfo]: ...
    def find(self, pred: Callable[[ProcInfo], bool]) -> list[ProcInfo]: ...
    def suspend(self, ref: ProcRef) -> bool: ...
    def resume(self, ref: ProcRef) -> bool: ...
    def terminate(self, ref: ProcRef) -> bool: ...  # только для ws close --force после подтверждения
    def spawn(
        self,
        argv: Sequence[str],
        env: Mapping[str, str],
        cwd: Path | None,
        env_remove: Sequence[str] = (),
    ) -> int: ...
    def self_and_ancestors(self) -> set[int]: ...


def _describe(proc: psutil.Process) -> ProcInfo | None:
    try:
        with proc.oneshot():
            try:
                cmdline = tuple(proc.cmdline())
            except (psutil.AccessDenied, OSError):
                cmdline = ()
            try:
                path = proc.exe()
            except (psutil.AccessDenied, OSError):
                path = ""
            return ProcInfo(
                pid=proc.pid,
                exe=proc.name(),
                cmdline=cmdline,
                created=proc.create_time(),
                parent=proc.ppid(),
                status=proc.status(),
                path=path,
            )
    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
        return None


class PsutilProcessSystem:
    def info(self, pid: int) -> ProcInfo | None:
        try:
            return _describe(psutil.Process(pid))
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            return None

    def tree(self, pid: int) -> list[ProcInfo]:
        try:
            root = psutil.Process(pid)
            procs = [root, *root.children(recursive=True)]
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            return []
        return [info for info in map(_describe, procs) if info is not None]

    def find(self, pred: Callable[[ProcInfo], bool]) -> list[ProcInfo]:
        found = []
        for proc in psutil.process_iter():
            info = _describe(proc)
            if info is not None and pred(info):
                found.append(info)
        return found

    def _checked(self, ref: ProcRef) -> psutil.Process | None:
        """Процесс по pid, только если это тот же процесс, что был записан."""
        try:
            proc = psutil.Process(ref.pid)
            return proc if same_process(proc.create_time(), ref.created) else None
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            return None

    def suspend(self, ref: ProcRef) -> bool:
        proc = self._checked(ref)
        if proc is None:
            return False
        try:
            proc.suspend()
            return True
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            return False

    def resume(self, ref: ProcRef) -> bool:
        proc = self._checked(ref)
        if proc is None:
            return False
        try:
            proc.resume()
            return True
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            return False

    def terminate(self, ref: ProcRef) -> bool:
        proc = self._checked(ref)
        if proc is None:
            return False
        try:
            proc.terminate()
            return True
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            return False

    def spawn(
        self,
        argv: Sequence[str],
        env: Mapping[str, str],
        cwd: Path | None,
        env_remove: Sequence[str] = (),
    ) -> int:
        full_env = {k: v for k, v in os.environ.items() if k not in set(env_remove)}
        full_env.update(env)
        proc = subprocess.Popen(
            list(argv),
            env=full_env,
            cwd=str(cwd) if cwd else None,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
        )
        return proc.pid

    def self_and_ancestors(self) -> set[int]:
        me = psutil.Process()
        try:
            return {me.pid, *(p.pid for p in me.parents())}
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            return {me.pid}
