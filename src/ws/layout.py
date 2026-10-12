"""Пересчёт координат «монитор + смещение» в экранные и обратно. Чистая логика."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from ws.errors import SystemFailure
from ws.models import MonitorInfo, Rect, WindowSpec

# Невидимая рамка окна в Win10/11 обычно 7-8 px; больше этого значит мусорные данные.
_MAX_INSET = 50


@dataclass(frozen=True)
class Insets:
    left: int = 0
    top: int = 0
    right: int = 0
    bottom: int = 0


def order_monitors(found: Sequence[tuple[bool, Rect, Rect, int]]) -> list[MonitorInfo]:
    """Стабильная нумерация: 0 = основной, остальные слева направо, сверху вниз.

    На входе кортежи (primary, bounds, work, dpi) в том порядке, как их отдала Windows.
    """
    ordered = sorted(found, key=lambda m: (not m[0], m[1].left, m[1].top))
    return [
        MonitorInfo(index=i, primary=primary, bounds=bounds, work=work, dpi=dpi)
        for i, (primary, bounds, work, dpi) in enumerate(ordered)
    ]


def _monitor(index: int, monitors: Sequence[MonitorInfo]) -> MonitorInfo:
    if not monitors:
        raise SystemFailure("Windows не вернула ни одного монитора")
    for monitor in monitors:
        if monitor.index == index:
            return monitor
    return monitors[0]  # монитора с таким индексом нет: падаем на основной


def resolve(spec: WindowSpec, monitors: Sequence[MonitorInfo]) -> Rect:
    """Видимые границы окна на экране. Окно не выходит за рабочую область монитора."""
    work = _monitor(spec.monitor, monitors).work
    width = min(spec.w, work.width)
    height = min(spec.h, work.height)
    left = min(max(work.left + spec.x, work.left), work.right - width)
    top = min(max(work.top + spec.y, work.top), work.bottom - height)
    return Rect(left, top, left + width, top + height)


def _overlap(a: Rect, b: Rect) -> int:
    width = min(a.right, b.right) - max(a.left, b.left)
    height = min(a.bottom, b.bottom) - max(a.top, b.top)
    return width * height if width > 0 and height > 0 else 0


def to_spec(frame_rect: Rect, monitors: Sequence[MonitorInfo], maximized: bool = False) -> WindowSpec:
    """Обратное к resolve: по видимым границам окна получить запись для манифеста."""
    if not monitors:
        raise SystemFailure("Windows не вернула ни одного монитора")
    best = max(monitors, key=lambda m: (_overlap(frame_rect, m.bounds), -m.index))
    if _overlap(frame_rect, best.bounds) == 0:
        best = monitors[0]
    return WindowSpec(
        monitor=best.index,
        x=frame_rect.left - best.work.left,
        y=frame_rect.top - best.work.top,
        w=frame_rect.width,
        h=frame_rect.height,
        maximized=maximized,
    )


def insets(rect: Rect, frame_rect: Rect) -> Insets:
    """Толщина невидимой рамки: разница между GetWindowRect и видимыми границами DWM."""
    result = Insets(
        left=frame_rect.left - rect.left,
        top=frame_rect.top - rect.top,
        right=rect.right - frame_rect.right,
        bottom=rect.bottom - frame_rect.bottom,
    )
    values = (result.left, result.top, result.right, result.bottom)
    if any(v < 0 or v > _MAX_INSET for v in values):
        return Insets()
    return result


def outer_rect(visible: Rect, ins: Insets) -> Rect:
    """Что передать в SetWindowPos, чтобы видимая часть окна совпала с visible."""
    return Rect(visible.left - ins.left, visible.top - ins.top, visible.right + ins.right, visible.bottom + ins.bottom)


def close_enough(a: Rect, b: Rect, tolerance: int = 2) -> bool:
    return (
        abs(a.left - b.left) <= tolerance
        and abs(a.top - b.top) <= tolerance
        and abs(a.right - b.right) <= tolerance
        and abs(a.bottom - b.bottom) <= tolerance
    )


def on_screen(rect: Rect, monitors: Sequence[MonitorInfo]) -> bool:
    return any(_overlap(rect, m.work) > 0 for m in monitors)
