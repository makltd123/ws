"""Горячие клавиши: разбор комбинаций и назначение пространствам. Чистая логика без Windows."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Sequence

from ws.errors import UserError

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN = 0x1, 0x2, 0x4, 0x8

_MODIFIERS = {
    "ctrl": MOD_CONTROL,
    "control": MOD_CONTROL,
    "alt": MOD_ALT,
    "shift": MOD_SHIFT,
    "win": MOD_WIN,
}
_NAMED_KEYS = {
    "space": 0x20, "tab": 0x09, "enter": 0x0D, "return": 0x0D, "esc": 0x1B, "escape": 0x1B,
    "backspace": 0x08, "insert": 0x2D, "delete": 0x2E, "home": 0x24, "end": 0x23,
    "pgup": 0x21, "pageup": 0x21, "pgdn": 0x22, "pagedown": 0x22,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
}  # fmt: skip
DEFAULT_COUNT = 9  # Ctrl+Alt+1..9 для первых девяти пространств


@dataclass(frozen=True)
class Hotkey:
    modifiers: int
    vk: int
    label: str  # каноническая запись для показа: "Ctrl+Alt+1"


@dataclass(frozen=True)
class Binding:
    space: str
    hotkey: Hotkey


@dataclass
class Bindings:
    items: list[Binding] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def label_for(self, space: str) -> str | None:
        return next((b.hotkey.label for b in self.items if b.space == space), None)


def parse_hotkey(text: str) -> Hotkey:
    parts = [p.strip().lower() for p in str(text).split("+") if p.strip()]
    if len(parts) < 2:
        raise UserError(f"горячая клавиша '{text}': нужен хотя бы один модификатор и клавиша, например ctrl+alt+1")
    *mods, key = parts
    modifiers = 0
    for mod in mods:
        if mod not in _MODIFIERS:
            raise UserError(f"горячая клавиша '{text}': неизвестный модификатор '{mod}' (ctrl, alt, shift, win)")
        modifiers |= _MODIFIERS[mod]
    if key in _MODIFIERS:
        raise UserError(f"горячая клавиша '{text}': после модификаторов нужна обычная клавиша")
    if len(key) == 1 and key.isascii() and key.isalnum():
        vk = ord(key.upper())
    elif key.startswith("f") and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
        vk = 0x70 + int(key[1:]) - 1
    elif key in _NAMED_KEYS:
        vk = _NAMED_KEYS[key]
    else:
        raise UserError(f"горячая клавиша '{text}': неизвестная клавиша '{key}'")
    return Hotkey(modifiers, vk, _label(modifiers, key))


def _label(modifiers: int, key: str) -> str:
    names = [("Ctrl", MOD_CONTROL), ("Alt", MOD_ALT), ("Shift", MOD_SHIFT), ("Win", MOD_WIN)]
    mods = [name for name, bit in names if modifiers & bit]
    shown = key.upper() if len(key) == 1 or (key.startswith("f") and key[1:].isdigit()) else key.capitalize()
    return "+".join([*mods, shown])


def assign(space_names: Sequence[str], configured: Mapping[str, str] | None) -> Bindings:
    """Какое пространство на какой клавише.

    Без ключа hotkeys в config.yml: Ctrl+Alt+1..9 первым девяти пространствам по алфавиту.
    С ключом: только то, что перечислено; неизвестные пространства и повторы попадают в warnings.
    """
    result = Bindings()
    wanted: list[tuple[str, str]]
    if configured is None:
        wanted = [(name, f"ctrl+alt+{i}") for i, name in enumerate(sorted(space_names)[:DEFAULT_COUNT], start=1)]
    else:
        wanted = list(configured.items())
    taken: dict[tuple[int, int], str] = {}
    for space, combo in wanted:
        if space not in space_names:
            result.warnings.append(f"hotkeys: нет пространства '{space}', пропускаю ({combo})")
            continue
        try:
            hotkey = parse_hotkey(combo)
        except UserError as exc:
            result.warnings.append(str(exc))
            continue
        key = (hotkey.modifiers, hotkey.vk)
        if key in taken:
            result.warnings.append(f"{hotkey.label} уже назначена пространству '{taken[key]}', '{space}' пропущено")
            continue
        taken[key] = space
        result.items.append(Binding(space, hotkey))
    return result
