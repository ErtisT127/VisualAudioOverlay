"""ctypes adapter for the native Direct2D/DirectComposition overlay."""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from pathlib import Path

from PyQt6.QtCore import QObject, QPoint, QTimer, pyqtSignal
from PyQt6.QtWidgets import QApplication


class _VaoEvent(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_uint32),
        ("x", ctypes.c_int32),
        ("y", ctypes.c_int32),
        ("sequence", ctypes.c_uint64),
    ]


class NativeOverlay(QObject):
    """Qt-free renderer exposed through a small Qt-compatible facade.

    The facade owns no QWidget.  It only marshals commands to the native render
    thread and polls the bounded native drag-event queue from the Qt thread.
    """

    positionChanged = pyqtSignal(int, int)
    positionPreview = pyqtSignal(int, int)

    _EVENT_PREVIEW = 1
    _EVENT_COMMITTED = 2

    def __init__(self, dll_path: str | os.PathLike[str] | None = None):
        super().__init__()
        self._dll = self._load_library(dll_path)
        # Test doubles expose the same function names but are not ctypes DLL
        # handles.  Keep their coordinates literal so unit tests continue to
        # exercise the Qt-compatible facade without pretending they are a
        # physical-pixel Win32 window.
        self._physical_coordinates = isinstance(self._dll, ctypes.CDLL)
        self._bind_api()
        self._handle = self._dll.vao_create()
        if not self._handle:
            raise RuntimeError("DirectComposition overlay initialization failed")

        self._visible = False
        self._drag_enabled = False
        self._x = 0
        self._y = 0
        self._width = 300
        self._height = 300
        # Qt exposes screen/window coordinates in device-independent pixels,
        # while SetWindowPos and GetWindowRect use physical pixels for a
        # per-monitor-aware native window.  Keep the public facade in Qt
        # coordinates and convert only at the ctypes boundary.  This is a
        # no-op on 100% DPI and on non-Windows test environments.
        self._native_scale = 1.0
        self._generation = 0
        self._accent_color = "#9751F2"
        self._stroke_width = 6

        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(15)
        self._poll_timer.timeout.connect(self._poll_events)
        self._apply_style()

    @staticmethod
    def _load_library(dll_path):
        candidates = []
        if dll_path:
            candidates.append(Path(dll_path))
        # The DLL is built under native/ at the repository root. Packaged
        # (Nuitka onefile) runs this module from the extraction root, where
        # build_nuitka unpacks the DLL to native/overlay_native.dll; in the
        # repo dev layout this module sits in src/, one level above the
        # repository root, so probe both this module's directory and its parent.
        module_dir = Path(__file__).resolve().parent
        for root in (module_dir, module_dir.parent):
            candidates.extend(
                [
                    root / "native" / "overlay_native.dll",
                    root / "native" / "overlay_native" / "build" / "overlay_native.dll",
                    root / "native" / "overlay_native" / "build" / "liboverlay_native.dll",
                ]
            )
        for candidate in candidates:
            if candidate.is_file():
                try:
                    return ctypes.WinDLL(str(candidate))
                except OSError:
                    continue
        searched = ", ".join(str(path) for path in candidates)
        raise RuntimeError(f"Native overlay DLL not found or unloadable: {searched}")

    def _bind_api(self):
        self._dll.vao_create.argtypes = []
        self._dll.vao_create.restype = ctypes.c_void_p
        self._dll.vao_destroy.argtypes = [ctypes.c_void_p]
        self._dll.vao_destroy.restype = None
        self._dll.vao_show.argtypes = [ctypes.c_void_p]
        self._dll.vao_show.restype = ctypes.c_int
        self._dll.vao_hide.argtypes = [ctypes.c_void_p]
        self._dll.vao_hide.restype = ctypes.c_int
        self._dll.vao_set_geometry.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int32,
            ctypes.c_int32,
            ctypes.c_int32,
            ctypes.c_int32,
        ]
        self._dll.vao_set_geometry.restype = ctypes.c_int
        self._dll.vao_set_drag_enabled.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self._dll.vao_set_drag_enabled.restype = ctypes.c_int
        self._dll.vao_set_generation.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
        self._dll.vao_set_generation.restype = ctypes.c_int
        self._dll.vao_set_style.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_float,
        ]
        self._dll.vao_set_style.restype = ctypes.c_int
        self._dll.vao_submit_audio.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint64,
            ctypes.c_float,
            ctypes.c_float,
            ctypes.c_int64,
        ]
        self._dll.vao_submit_audio.restype = ctypes.c_int
        self._dll.vao_poll_event.argtypes = [ctypes.c_void_p, ctypes.POINTER(_VaoEvent)]
        self._dll.vao_poll_event.restype = ctypes.c_int

    @staticmethod
    def _color_rgba(value: str) -> int:
        value = str(value).strip()
        if len(value) != 7 or not value.startswith("#"):
            raise ValueError("overlay color must be #RRGGBB")
        return (int(value[1:], 16) << 8) | 0xFF

    def _apply_style(self):
        native_width = self._stroke_width * self._native_scale
        result = self._dll.vao_set_style(
            self._handle,
            self._color_rgba(self._accent_color),
            ctypes.c_float(native_width),
        )
        if not result:
            raise RuntimeError("failed to set native overlay style")

    def show(self):
        if not self._dll.vao_show(self._handle):
            raise RuntimeError("failed to show native overlay")
        self._visible = True
        self._poll_timer.start()

    def hide(self):
        if self._handle:
            self._dll.vao_hide(self._handle)
        self._visible = False
        self._poll_timer.stop()

    def close(self):
        self._poll_timer.stop()
        if self._handle:
            self._dll.vao_destroy(self._handle)
            self._handle = None
        self._visible = False

    def isVisible(self):
        return self._visible

    def move(self, x: int, y: int):
        self._x = int(x)
        self._y = int(y)
        self._set_geometry()

    def pos(self):
        return QPoint(self._x, self._y)

    def width(self):
        return self._width

    def height(self):
        return self._height

    def set_geometry(self, x: int, y: int, width: int, height: int):
        self._x, self._y = int(x), int(y)
        self._width, self._height = int(width), int(height)
        self._set_geometry()

    def _set_geometry(self):
        previous_scale = self._native_scale
        screen, native_rect = self._screen_mapping(self._x + self._width / 2, self._y + self._height / 2)
        # Test doubles and non-Windows callers do not expose QScreen objects;
        # retain the legacy scale hook for those paths while native Windows
        # instances use the actual screen DPI.
        scale = self._screen_scale(screen) if screen is not None else self._scale_for_point(self._x, self._y)
        self._native_scale = scale
        if native_rect is not None and screen is not None:
            logical_geo = screen.geometry()
            native_x = native_rect[0] + round((self._x - logical_geo.x()) * scale)
            native_y = native_rect[1] + round((self._y - logical_geo.y()) * scale)
        else:
            native_x = round(self._x * scale)
            native_y = round(self._y * scale)
        native_width = max(1, round(self._width * scale))
        native_height = max(1, round(self._height * scale))
        if not self._dll.vao_set_geometry(self._handle, native_x, native_y, native_width, native_height):
            raise RuntimeError("failed to set native overlay geometry")
        if abs(scale - previous_scale) > 1e-3:
            self._apply_style()

    @staticmethod
    def _screen_scale(screen) -> float:
        try:
            return max(1.0, float(screen.devicePixelRatio())) if screen else 1.0
        except AttributeError, TypeError, ValueError:
            return 1.0

    def _screen_mapping(self, x: float, y: float):
        """Return the Qt screen and matching physical monitor rectangle."""
        if not self._physical_coordinates or os.name != "nt":
            return None, None
        app = QApplication.instance()
        if app is None:
            return None, None
        screens = list(app.screens())
        point = (round(x), round(y))
        screen = next((s for s in screens if s.geometry().contains(*point)), None)
        if screen is None:
            screen = app.primaryScreen()
        if screen is None:
            return None, None
        return screen, self._native_monitor_rect(screen, screens)

    @staticmethod
    def _native_monitor_rect(screen, screens):
        """Resolve a QScreen to its physical virtual-desktop rectangle.

        QScreen.name() is the Win32 display device name (for example
        ``\\\\.\\DISPLAY2``).  Enumerating monitors avoids assuming that the
        logical and physical virtual-desktop origins are both zero, which is
        incorrect for negative-position and mixed-DPI monitors.
        """
        if os.name != "nt":
            return None
        try:
            user32 = getattr(NativeOverlay, "_user32", None)
            if user32 is None:
                user32 = ctypes.WinDLL("user32", use_last_error=True)
                NativeOverlay._user32 = user32

            class _MonitorInfo(ctypes.Structure):
                _fields_ = [
                    ("cbSize", wintypes.DWORD),
                    ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT),
                    ("dwFlags", wintypes.DWORD),
                    ("szDevice", wintypes.WCHAR * 32),
                ]

            get_info = user32.GetMonitorInfoW
            get_info.argtypes = [wintypes.HMONITOR, ctypes.POINTER(_MonitorInfo)]
            get_info.restype = wintypes.BOOL
            enum_proc_type = ctypes.WINFUNCTYPE(
                wintypes.BOOL,
                wintypes.HMONITOR,
                wintypes.HDC,
                ctypes.POINTER(wintypes.RECT),
                wintypes.LPARAM,
            )
            result = []

            @enum_proc_type
            def callback(handle, _hdc, _rect, _data):
                info = _MonitorInfo()
                info.cbSize = ctypes.sizeof(info)
                if get_info(handle, ctypes.byref(info)):
                    result.append(
                        (
                            str(info.szDevice),
                            info.rcMonitor.left,
                            info.rcMonitor.top,
                            info.rcMonitor.right,
                            info.rcMonitor.bottom,
                            info.dwFlags,
                        )
                    )
                return True

            if not user32.EnumDisplayMonitors(None, None, callback, 0):
                return None
            name = str(screen.name())
            for item in result:
                if item[0].rstrip("\x00") == name:
                    return item[1:5]

            # Qt exposes the monitor's friendly EDID name while Win32 exposes
            # DISPLAY1/2. Match by physical dimensions and primary status
            # instead of list order; DPI-aware Qt commonly returns a different
            # ordering for the logical and physical virtual desktops.
            logical = screen.geometry()
            scale = NativeOverlay._screen_scale(screen)
            expected_w = round(logical.width() * scale)
            expected_h = round(logical.height() * scale)
            candidates = [
                item
                for item in result
                if abs((item[3] - item[1]) - expected_w) <= 2 and abs((item[4] - item[2]) - expected_h) <= 2
            ]
            app = QApplication.instance()
            primary = app.primaryScreen() if app is not None else None
            if primary is screen or primary == screen:
                primary_candidates = [item for item in candidates if item[5] & 1]
                if primary_candidates:
                    return primary_candidates[0][1:5]
            non_primary = [item for item in candidates if not (item[5] & 1)]
            if non_primary:
                peers = [
                    candidate
                    for candidate in screens
                    if abs(candidate.geometry().width() * NativeOverlay._screen_scale(candidate) - expected_w) <= 2
                    and abs(candidate.geometry().height() * NativeOverlay._screen_scale(candidate) - expected_h) <= 2
                    and candidate != primary
                ]
                peers.sort(key=lambda candidate: (candidate.geometry().x(), candidate.geometry().y()))
                non_primary.sort(key=lambda item: (item[1], item[2]))
                if screen in peers:
                    return non_primary[min(peers.index(screen), len(non_primary) - 1)][1:5]
            # Last resort for unusual drivers where dimensions are unavailable.
            index = screens.index(screen)
            if 0 <= index < len(result):
                return result[index][1:5]
        except AttributeError, OSError, TypeError, ValueError:
            return None
        return None

    def _scale_for_point(self, x: float, y: float) -> float:
        """Return the Qt device-pixel ratio for a logical desktop point.

        The native window is created on a dedicated thread but in the same
        process, so its Win32 coordinates are physical pixels.  Restrict this
        conversion to Windows; keeping the fallback at 1.0 also makes the
        wrapper deterministic in headless/unit-test environments.
        """
        if not self._physical_coordinates or os.name != "nt":
            return 1.0
        app = QApplication.instance()
        if app is None:
            return 1.0
        screen, _ = self._screen_mapping(x, y)
        return self._screen_scale(screen)

    def _logical_position(self, native_x: int, native_y: int) -> tuple[int, int]:
        if self._physical_coordinates and os.name == "nt":
            app = QApplication.instance()
            if app is not None:
                screens = list(app.screens())
                for screen in screens:
                    rect = self._native_monitor_rect(screen, screens)
                    if rect is None:
                        continue
                    if rect[0] <= native_x < rect[2] and rect[1] <= native_y < rect[3]:
                        scale = self._screen_scale(screen)
                        geo = screen.geometry()
                        return (
                            round(geo.x() + (native_x - rect[0]) / scale),
                            round(geo.y() + (native_y - rect[1]) / scale),
                        )
        scale = self._scale_for_point(native_x / self._native_scale, native_y / self._native_scale)
        return round(native_x / scale), round(native_y / scale)

    def set_drag_enabled(self, enabled):
        enabled = bool(enabled)
        if not self._dll.vao_set_drag_enabled(self._handle, int(enabled)):
            raise RuntimeError("failed to change native overlay hit testing")
        self._drag_enabled = enabled

    @property
    def drag_enabled(self):
        return self._drag_enabled

    def set_accent_color(self, hex_color):
        self._accent_color = str(hex_color)
        self._apply_style()

    def set_stroke_width(self, width):
        self._stroke_width = int(width)
        self._apply_style()

    def set_capture_generation(self, generation):
        self._generation = int(generation)
        if not self._dll.vao_set_generation(self._handle, self._generation):
            raise RuntimeError("failed to change native overlay generation")

    def update_audio_data(self, angle, intensity, generation=None):
        if generation is None:
            generation = self._generation
        self._dll.vao_submit_audio(
            self._handle,
            int(generation),
            float(angle),
            float(intensity),
            0,
        )

    def _poll_events(self):
        event = _VaoEvent()
        while self._dll.vao_poll_event(self._handle, ctypes.byref(event)):
            if event.type == self._EVENT_PREVIEW:
                self._x, self._y = self._logical_position(event.x, event.y)
                self.positionPreview.emit(self._x, self._y)
            elif event.type == self._EVENT_COMMITTED:
                self._x, self._y = self._logical_position(event.x, event.y)
                self.positionChanged.emit(self._x, self._y)
