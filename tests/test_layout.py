from fakes import monitor

from ws import layout
from ws.models import Rect, WindowSpec

ONE = [monitor(0, 0, 0, 1920, 1080, primary=True)]
TWO = [monitor(0, 0, 0, 1920, 1080, primary=True), monitor(1, 1920, 0, 1920, 1080)]


def test_resolve_on_primary():
    assert layout.resolve(WindowSpec(0, 10, 20, 800, 600), TWO) == Rect(10, 20, 810, 620)


def test_resolve_on_second_monitor():
    assert layout.resolve(WindowSpec(1, 0, 0, 640, 520), TWO) == Rect(1920, 0, 2560, 520)


def test_missing_monitor_falls_back_to_primary():
    assert layout.resolve(WindowSpec(1, 0, 0, 640, 520), ONE) == Rect(0, 0, 640, 520)


def test_window_is_kept_inside_work_area():
    # рабочая область 1920x1040 (панель задач 40 px), окно больше и сдвинуто вправо
    assert layout.resolve(WindowSpec(0, 1800, 900, 1000, 500), ONE) == Rect(920, 540, 1920, 1040)
    assert layout.resolve(WindowSpec(0, 0, 0, 5000, 5000), ONE) == Rect(0, 0, 1920, 1040)


def test_to_spec_is_inverse_of_resolve():
    spec = WindowSpec(1, 100, 50, 700, 400)
    assert layout.to_spec(layout.resolve(spec, TWO), TWO) == spec


def test_to_spec_picks_monitor_with_biggest_overlap():
    rect = Rect(1800, 100, 2400, 500)  # 120 px на первом, 480 на втором
    assert layout.to_spec(rect, TWO).monitor == 1


def test_to_spec_offscreen_goes_to_primary():
    assert layout.to_spec(Rect(9000, 9000, 9100, 9100), TWO).monitor == 0


def test_insets_and_outer_rect():
    rect = Rect(93, 100, 907, 707)
    frame = Rect(100, 100, 900, 700)
    ins = layout.insets(rect, frame)
    assert ins == layout.Insets(7, 0, 7, 7)
    assert layout.outer_rect(Rect(0, 0, 800, 600), ins) == Rect(-7, 0, 807, 607)


def test_garbage_insets_are_ignored():
    assert layout.insets(Rect(0, 0, 100, 100), Rect(500, 500, 600, 600)) == layout.Insets()


def test_monitor_order_primary_first_then_left_to_right():
    p = (True, Rect(1920, 0, 3840, 1080), Rect(1920, 0, 3840, 1040), 96)
    left = (False, Rect(0, 0, 1920, 1080), Rect(0, 0, 1920, 1040), 96)
    right = (False, Rect(3840, 0, 5760, 1080), Rect(3840, 0, 5760, 1040), 96)
    result = layout.order_monitors([right, p, left])
    assert [m.index for m in result] == [0, 1, 2]
    assert result[0].primary and result[1].bounds.left == 0 and result[2].bounds.left == 3840
