"""Unit tests for per-monitor overlay position storage (main.py).

The screen-ownership lookup is pure rect logic with no Qt state, so it stays
fast and deterministic. Screen-mapping paths that touch QApplication
(set_monitor, start placement) are thin callers of this helper.
"""

from main import _find_screen_at_centre


class FakeRect:
    """The subset of QRect used by the helpers under test."""

    def __init__(self, x, y, width, height):
        self._x = x
        self._y = y
        self._w = width
        self._h = height

    def x(self):
        return self._x

    def y(self):
        return self._y

    def width(self):
        return self._w

    def height(self):
        return self._h

    def contains(self, px, py):
        # QRect point tests are half-open: the right and bottom edges lie
        # outside the rect (integer rects: right() = x + width - 1).
        return self._x <= px < self._x + self._w and self._y <= py < self._y + self._h


class FakeScreen:
    def __init__(self, name, rect):
        self._name = name
        self._rect = rect

    def name(self):
        return self._name

    def geometry(self):
        return self._rect


def screen(name, x, y, w=1920, h=1080):
    return FakeScreen(name, FakeRect(x, y, w, h))


# ── _find_screen_at_centre ───────────────────────────────────────────────


def test_centre_picks_the_owning_screen():
    screens = [screen("PRIMARY", 0, 0), screen("SECONDARY", 1920, 0)]
    assert _find_screen_at_centre(screens, 100, 540) is screens[0]
    assert _find_screen_at_centre(screens, 2000, 540) is screens[1]


def test_centre_off_every_screen_returns_none():
    screens = [screen("PRIMARY", 0, 0)]
    assert _find_screen_at_centre(screens, 5000, 5000) is None


def test_centre_on_monitor_left_of_primary():
    # Windows can place a monitor at negative virtual-desktop coordinates.
    screens = [screen("PRIMARY", 0, 0), screen("LEFT", -1920, 0)]
    assert _find_screen_at_centre(screens, -1000, 540) is screens[1]


def test_centre_inside_closed_rect_edges():
    # QRect.contains() includes the left/top edge of the rect.
    screens = [screen("PRIMARY", 0, 0, w=640, h=360)]
    assert _find_screen_at_centre(screens, 0, 0) is screens[0]
    assert _find_screen_at_centre(screens, 640, 360) is None


def test_fractional_centre_is_rounded_to_pixels():
    # QRect.contains() only takes whole pixels; the fractional centre must be
    # rounded, never passed through (PyQt6 raises TypeError on a float).
    screens = [screen("PRIMARY", 0, 0, w=640, h=360)]
    assert _find_screen_at_centre(screens, 639.4, 300.2) is screens[0]
    assert _find_screen_at_centre(screens, 639.6, 300.2) is None
