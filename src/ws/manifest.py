"""Чтение и проверка YAML-манифестов пространств."""

from __future__ import annotations

import json
import os
import re
from dataclasses import replace
from pathlib import Path
from typing import Any

import yaml

from ws.config import Paths
from ws.errors import UserError
from ws.models import (
    AppItem,
    BrowserItem,
    ExplorerItem,
    Item,
    Space,
    TerminalItem,
    TerminalTab,
    Triggers,
    VSCodeItem,
    WindowSpec,
)

# Имена попадают в пути файлов, поэтому набор символов ограничен.
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

TEMPLATE = """\
name: {name}
description: ""
items:
  - type: explorer
    path: C:\\
    # window: {{monitor: 0, x: 0, y: 0, w: 900, h: 700}}
"""


class ManifestError(UserError):
    pass


def validate_name(name: str, what: str = "имя пространства") -> str:
    if not NAME_RE.match(name) or name.endswith("."):
        raise UserError(f"Недопустимое {what} '{name}': разрешены латинские буквы, цифры, '.', '_' и '-'")
    return name


REPO_MANIFEST = ".workspace.yml"


def manifest_path(paths: Paths, name: str) -> Path:
    """Где лежит (или будет лежать) личный манифест пространства в %APPDATA%\\ws\\spaces."""
    return paths.spaces / f"{validate_name(name)}.yml"


# --- манифесты в репозиториях (.workspace.yml) ---------------------------------
# `ws open .` запоминает такой манифест в repos.json, и дальше пространство доступно по имени
# так же, как личные: ws switch <имя>, трей, горячие клавиши.


def registered_repos(paths: Paths) -> dict[str, Path]:
    """Имя -> файл .workspace.yml. Записи, чьих файлов больше нет, не показываются."""
    try:
        raw = json.loads(paths.repos_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {name: Path(p) for name, p in raw.items() if isinstance(p, str) and NAME_RE.match(name) and Path(p).exists()}


def register_repo(paths: Paths, space: Space) -> None:
    if space.source is None:
        return
    if manifest_path(paths, space.name).exists():
        raise UserError(
            f"Пространство '{space.name}' уже есть среди личных ({manifest_path(paths, space.name)}). "
            f"Переименуй name: в {space.source}, чтобы имена не пересекались"
        )
    known = registered_repos(paths)
    if known.get(space.name) == space.source:
        return
    known[space.name] = space.source
    tmp = paths.repos_file.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({n: str(p) for n, p in known.items()}, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, paths.repos_file)


def list_names(paths: Paths) -> list[str]:
    local = (
        {p.stem for p in paths.spaces.glob("*.yml") if NAME_RE.match(p.stem)} if paths.spaces.exists() else set()
    )
    return sorted(local | set(registered_repos(paths)))


def editable_path(paths: Paths, name: str) -> Path:
    """Файл, который открывает ws edit: личный, иначе файл репозитория, иначе место под новый личный."""
    local = manifest_path(paths, name)
    if local.exists():
        return local
    return registered_repos(paths).get(name, local)


def load(paths: Paths, name: str) -> Space:
    path = manifest_path(paths, name)
    if path.exists():
        space = parse(path.read_text(encoding="utf-8"), str(path), default_name=name)
        if space.name != name:
            raise ManifestError(f"{path}: name: в файле указано '{space.name}', а файл называется '{name}.yml'")
        return replace(space, source=path)
    repo_file = _registered_file(paths, name)
    if repo_file is not None:
        space = load_file(repo_file)
        if space.name != name:
            raise ManifestError(
                f"{repo_file}: теперь name: '{space.name}' вместо '{name}'; открой заново: ws open {repo_file.parent}"
            )
        return space
    raise UserError(f"Нет пространства '{name}' (ожидался файл {path})")


def _registered_file(paths: Paths, name: str) -> Path | None:
    return registered_repos(paths).get(name)


def load_file(path: Path) -> Space:
    """Манифест из произвольного файла; относительные пути отсчитываются от его папки."""
    base = path.resolve().parent
    default = re.sub(r"[^A-Za-z0-9._-]", "-", base.name).strip("-.") or "repo"
    if not NAME_RE.match(default):
        default = "repo-" + default
    space = parse(path.read_text(encoding="utf-8"), str(path), default_name=default, base_dir=base)
    return replace(space, source=path.resolve())


def find_repo_manifest(start: Path) -> Path | None:
    """.workspace.yml в папке или выше (до корня диска)."""
    current = start.resolve()
    for folder in (current, *current.parents):
        candidate = folder / REPO_MANIFEST
        if candidate.is_file():
            return candidate
    return None


def is_path_arg(arg: str) -> bool:
    """Имена пространств не содержат эти символы, значит это путь, а не имя."""
    return arg.startswith(".") or any(c in arg for c in "\\/:~")


def resolve(paths: Paths, arg: str, register: bool = True) -> Space:
    """Имя пространства или путь к репозиторию с .workspace.yml (`ws open .`)."""
    if not is_path_arg(arg):
        return load(paths, arg)
    target = Path(arg).expanduser()
    if not target.exists():
        raise UserError(f"Путь не найден: {arg}")
    start = target.parent if target.is_file() else target
    file = target.resolve() if target.is_file() else find_repo_manifest(start)
    if file is None:
        raise UserError(f"В {start.resolve()} и выше нет файла {REPO_MANIFEST}")
    space = load_file(file)
    if register:
        register_repo(paths, space)
    return space


def parse(text: str, source: str, default_name: str | None = None, base_dir: Path | None = None) -> Space:
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ManifestError(f"{source}: не удалось разобрать YAML: {exc}") from exc
    r = _Reader(source, base_dir)
    top = r.mapping(raw, "", {"name", "description", "env", "items", "freeze", "triggers"})

    name = top.get("name", default_name)
    if not isinstance(name, str):
        r.fail("name", "обязательное строковое поле")
    try:
        validate_name(name)
    except UserError as exc:
        r.fail("name", str(exc))

    env_raw = r.mapping(top.get("env") or {}, "env", None)
    env = {str(k): str(v) for k, v in env_raw.items()}

    items_raw = top.get("items") or []
    if not isinstance(items_raw, list):
        r.fail("items", "ожидался список")
    items = tuple(_item(r, raw_item, f"items[{i}]") for i, raw_item in enumerate(items_raw))

    freeze = r.mapping(top.get("freeze") or {}, "freeze", {"exclude"})
    exclude = r.str_list(freeze.get("exclude"), "freeze.exclude")

    triggers = None
    if top.get("triggers") is not None:
        t = r.mapping(top["triggers"], "triggers", {"git_branch", "repo"})
        branch = r.string(t, "git_branch", "triggers")
        assert branch is not None
        triggers = Triggers(git_branch=branch, repo=r.path(t, "repo", "triggers") if "repo" in t else None)

    return Space(
        name=name,
        description=str(top.get("description") or ""),
        env=env,
        items=items,
        freeze_exclude=frozenset(e.lower() for e in exclude),
        triggers=triggers,
    )


class _Reader:
    """Хелперы проверки; в сообщении об ошибке всегда есть путь до поля."""

    def __init__(self, source: str, base_dir: Path | None = None) -> None:
        self.source = source
        self.base_dir = base_dir  # относительные пути отсчитываются отсюда (манифест в репозитории)

    def fail(self, path: str, message: str) -> Any:
        where = f"{self.source}: {path}: " if path else f"{self.source}: "
        raise ManifestError(where + message)

    def mapping(self, value: Any, path: str, allowed: set[str] | None) -> dict[str, Any]:
        if not isinstance(value, dict):
            self.fail(path, "ожидался словарь")
        if allowed is not None:
            unknown = sorted(str(k) for k in value if k not in allowed)
            if unknown:
                self.fail(path, f"неизвестные поля: {', '.join(unknown)}")
        return value

    def string(self, d: dict[str, Any], key: str, path: str, required: bool = True) -> str | None:
        value = d.get(key)
        if value is None:
            if required:
                self.fail(f"{path}.{key}", "обязательное поле")
            return None
        if not isinstance(value, str) or not value:
            self.fail(f"{path}.{key}", "ожидалась непустая строка")
        return value

    def path(self, d: dict[str, Any], key: str, path: str) -> Path:
        value = self.string(d, key, path)
        assert value is not None
        result = Path(os.path.expandvars(value)).expanduser()
        if self.base_dir is not None and not result.is_absolute():
            result = Path(os.path.normpath(self.base_dir / result))
        return result

    def integer(self, d: dict[str, Any], key: str, path: str, default: int | None = None) -> int:
        value = d.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int):
            self.fail(f"{path}.{key}", "ожидалось целое число")
        return value

    def boolean(self, d: dict[str, Any], key: str, path: str, default: bool) -> bool:
        value = d.get(key, default)
        if not isinstance(value, bool):
            self.fail(f"{path}.{key}", "ожидалось true или false")
        return value

    def str_list(self, value: Any, path: str) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list) or not all(isinstance(v, (str, int, float)) for v in value):
            self.fail(path, "ожидался список строк")
        return [str(v) for v in value]


def _window(r: _Reader, value: Any, path: str) -> WindowSpec | None:
    if value is None:
        return None
    d = r.mapping(value, path, {"monitor", "x", "y", "w", "h", "maximized"})
    spec = WindowSpec(
        monitor=r.integer(d, "monitor", path, default=0),
        x=r.integer(d, "x", path),
        y=r.integer(d, "y", path),
        w=r.integer(d, "w", path),
        h=r.integer(d, "h", path),
        maximized=r.boolean(d, "maximized", path, default=False),
    )
    if spec.monitor < 0:
        r.fail(f"{path}.monitor", "индекс монитора не может быть отрицательным")
    if spec.w <= 0 or spec.h <= 0:
        r.fail(path, "w и h должны быть больше нуля")
    return spec


def _item(r: _Reader, value: Any, path: str) -> Item:
    d = r.mapping(value, path, None)
    kind = d.get("type")
    common = {"type", "window"}

    if kind == "vscode":
        r.mapping(d, path, common | {"path", "isolate"})
        return VSCodeItem(
            path=r.path(d, "path", path),
            isolate=r.boolean(d, "isolate", path, default=True),
            window=_window(r, d.get("window"), f"{path}.window"),
        )

    if kind == "terminal":
        r.mapping(d, path, common | {"tabs"})
        tabs_raw = d.get("tabs") or []
        if not isinstance(tabs_raw, list):
            r.fail(f"{path}.tabs", "ожидался список")
        tabs = []
        for i, tab_raw in enumerate(tabs_raw):
            tab_path = f"{path}.tabs[{i}]"
            tab = r.mapping(tab_raw, tab_path, {"dir", "cmd", "title"})
            tabs.append(
                TerminalTab(
                    dir=r.path(tab, "dir", tab_path),
                    cmd=r.string(tab, "cmd", tab_path, required=False),
                    title=r.string(tab, "title", tab_path, required=False),
                )
            )
        return TerminalItem(tabs=tuple(tabs), window=_window(r, d.get("window"), f"{path}.window"))

    if kind == "browser":
        r.mapping(d, path, common | {"browser", "profile", "urls"})
        browser = d.get("browser", "chrome")
        if browser not in ("chrome", "edge"):
            r.fail(f"{path}.browser", "ожидалось chrome или edge")
        profile = r.string(d, "profile", path, required=False)
        if profile is not None:
            try:
                validate_name(profile, "имя профиля")
            except UserError as exc:
                r.fail(f"{path}.profile", str(exc))
        return BrowserItem(
            browser=browser,
            profile=profile,
            urls=tuple(r.str_list(d.get("urls"), f"{path}.urls")),
            window=_window(r, d.get("window"), f"{path}.window"),
        )

    if kind == "explorer":
        r.mapping(d, path, common | {"path"})
        return ExplorerItem(path=r.path(d, "path", path), window=_window(r, d.get("window"), f"{path}.window"))

    if kind == "app":
        r.mapping(d, path, common | {"exe", "args"})
        return AppItem(
            exe=r.path(d, "exe", path),
            args=tuple(r.str_list(d.get("args"), f"{path}.args")),
            window=_window(r, d.get("window"), f"{path}.window"),
        )

    return r.fail(f"{path}.type", "ожидалось одно из: vscode, terminal, browser, explorer, app")
