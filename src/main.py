# nuitka-project: --onefile
# nuitka-project: --output-dir=dist/nuitka
# nuitka-project: --output-filename=VisualAudioOverlay.exe

# nuitka-project: --windows-console-mode=disable
# nuitka-project: --windows-icon-from-ico=assets/icon.ico

# nuitka-project: --enable-plugin=pyqt6
# nuitka-project: --noinclude-qt-translations

# nuitka-project: --include-data-dir=dashboard_v2=dashboard_v2
# nuitka-project: --include-data-dir=assets=assets

# nuitka-project: --include-module=audio_capture
# nuitka-project: --include-module=app_logging
# nuitka-project: --include-module=direction
# nuitka-project: --include-module=mono_output
# nuitka-project: --include-module=process_loopback

# nuitka-project: --include-package=comtypes
# nuitka-project: --include-package=pycaw
# nuitka-project: --include-package=soundcard
# nuitka-project: --include-package=psutil

# nuitka-project: --nofollow-import-to=comtypes.test
# nuitka-project: --nofollow-import-to=comtypes.test.*
# nuitka-project: --nofollow-import-to=comtypes.server
# nuitka-project: --nofollow-import-to=comtypes.server.*
# nuitka-project: --nofollow-import-to=comtypes.tools
# nuitka-project: --nofollow-import-to=comtypes.tools.*

# nuitka-project: --nofollow-import-to=pycaw.test
# nuitka-project: --nofollow-import-to=pycaw.test.*

# nuitka-project: --nofollow-import-to=numpy.f2py
# nuitka-project: --nofollow-import-to=numpy.f2py.*
# nuitka-project: --nofollow-import-to=numpy.testing
# nuitka-project: --nofollow-import-to=numpy.testing.*
# nuitka-project: --nofollow-import-to=numpy.tests
# nuitka-project: --nofollow-import-to=numpy.tests.*

# nuitka-project: --nofollow-import-to=psutil._pslinux
# nuitka-project: --nofollow-import-to=psutil._psosx
# nuitka-project: --nofollow-import-to=psutil._psbsd
# nuitka-project: --nofollow-import-to=psutil._pssunos
# nuitka-project: --nofollow-import-to=psutil._psaix

# nuitka-project: --python-flag=no_docstrings

# nuitka-project: --noinclude-data-files=qtwebengine_devtools_resources.pak
# nuitka-project: --noinclude-data-files=qtwebengine_devtools_resources.debug.pak
# nuitka-project: --noinclude-data-files=qtwebengine_resources.debug.pak
# nuitka-project: --noinclude-data-files=qtwebengine_resources_100p.debug.pak
# nuitka-project: --noinclude-data-files=qtwebengine_resources_200p.debug.pak

import contextlib
import ctypes
import json
import math
import multiprocessing as mp
import os
import sys
import tempfile
from ctypes import wintypes
from typing import ClassVar

_exe_name = os.path.basename(sys.executable).lower()
_IS_PACKAGED_LAUNCH = (
    "__compiled__" in globals() or bool(getattr(sys, "frozen", False)) or not _exe_name.startswith(("python", "pypy"))
)


def _initial_windows_scale() -> float:
    """Read the system scale before Qt creates its first screen/window."""
    if sys.platform != "win32":
        return 1.0
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        get_dpi = getattr(user32, "GetDpiForSystem", None)
        if get_dpi is not None:
            get_dpi.argtypes = []
            get_dpi.restype = wintypes.UINT
            dpi = int(get_dpi())
            if dpi > 0:
                return dpi / 96.0
    except AttributeError, OSError, TypeError, ValueError:
        pass
    return 1.0


def _enable_per_monitor_dpi_awareness():
    """Declare the process DPI mode before Qt creates any windows.

    Qt 6 normally enables high-DPI scaling itself, but a packaged process can
    inherit a legacy/system DPI context from its launcher.  In that case
    Windows bitmap-scales the WebEngine backing surface when the dashboard is
    shown on a non-100% display, producing blurred text and stale geometry.
    The call is intentionally best-effort: Qt remains the fallback on older
    Windows builds and on non-Windows test runners.
    """
    if sys.platform != "win32":
        return
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        setter = getattr(user32, "SetProcessDpiAwarenessContext", None)
        if setter is not None:
            setter.argtypes = [ctypes.c_void_p]
            setter.restype = wintypes.BOOL
            # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 is (HANDLE)-4.
            if setter(ctypes.c_void_p(-4)):
                return
        # Windows 10 versions without the context API still support the
        # process-wide shcore API.  Failure here is harmless; Qt will choose
        # its normal platform DPI policy.
        shcore = ctypes.WinDLL("shcore", use_last_error=True)
        setter = getattr(shcore, "SetProcessDpiAwareness", None)
        if setter is not None:
            setter.argtypes = [ctypes.c_int]
            setter.restype = ctypes.c_long
            setter(2)  # PROCESS_PER_MONITOR_DPI_AWARE
    except AttributeError, OSError, TypeError, ValueError:
        # This must never prevent the dashboard from starting.
        return


_enable_per_monitor_dpi_awareness()
# Qt reads this policy while initializing its platform plugin; setting the
# environment value as well as the API below covers packaged launches where
# the plugin may be initialized before the Python-side QApplication call.
os.environ.setdefault("QT_SCALE_FACTOR_ROUNDING_POLICY", "PassThrough")

if _IS_PACKAGED_LAUNCH:
    os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-logging")


def _append_webengine_scale_flag(scale: float):
    """Keep Chromium's device scale aligned with Qt's initial screen DPR.

    QtWebEngine can otherwise derive a different fractional scale from the
    Windows compositor (observed as Qt=1.5, Chromium=1.8).  That mismatch makes
    Chromium render a backing surface at the wrong size and lets Windows
    resample the whole dashboard.  Respect an explicit caller override, since
    it is useful for diagnostics and kiosk deployments.
    """
    try:
        scale = float(scale)
    except TypeError, ValueError, OverflowError:
        return
    if not math.isfinite(scale) or scale <= 0:
        return
    flags = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "").strip()
    if "--force-device-scale-factor=" in flags:
        return
    formatted = f"{scale:.4f}".rstrip("0").rstrip(".")
    os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = f"{flags} --force-device-scale-factor={formatted}".strip()


# Chromium reads this during QtWebEngine startup, before QApplication has a
# primary QScreen that Python can inspect.  Configure it at import time so the
# first renderer surface uses the same scale as Qt instead of being resampled.
_append_webengine_scale_flag(_initial_windows_scale())

from PyQt6.QtCore import (
    QAbstractNativeEventFilter,
    QEvent,
    QObject,
    Qt,
    QThread,
    QTimer,
    QtMsgType,
    QUrl,
    pyqtSignal,
    pyqtSlot,
    qInstallMessageHandler,
)
from PyQt6.QtGui import QAction, QGuiApplication, QIcon
from PyQt6.QtNetwork import QLocalServer, QLocalSocket
from PyQt6.QtWebChannel import QWebChannel
from PyQt6.QtWebEngineCore import QWebEnginePage
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWidgets import (
    QApplication,
    QMainWindow,
    QMenu,
    QMessageBox,
    QSystemTrayIcon,
)

from app_logging import (
    LOG_DEBUG_ENV,
    LOG_ENABLED_ENV,
    LOG_PATH_ENV,
    configure_logging,
    get_logger,
)
from audio_capture import AudioCaptureThread
from native_overlay import NativeOverlay

# RESOURCE_DIR contains bundled, read-only assets. When packaged (Nuitka
# onefile), __file__ resolves inside the deployed bundle, whose payload root
# holds the dashboard_v2/ and assets/ dirs the --include-data-dir options
# unpack. In the dev layout sources live in src/ while those resources stay at
# the repository root, one level above this module.
if _IS_PACKAGED_LAUNCH:
    RESOURCE_DIR = os.path.dirname(os.path.abspath(__file__))
else:
    RESOURCE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Nuitka onefile executes the Python payload from a temporary extraction
# directory.  Persisted user data must instead follow the launcher executable.
IS_PACKAGED = _IS_PACKAGED_LAUNCH
if IS_PACKAGED:
    # Nuitka onefile exposes the launch directory explicitly because the
    # payload's __file__/sys.executable can point at the temporary extraction.
    DATA_DIR = os.environ.get("NUITKA_ONEFILE_DIRECTORY") or os.path.dirname(os.path.abspath(sys.executable))
else:
    DATA_DIR = RESOURCE_DIR

PROFILES_FILE = os.path.join(DATA_DIR, "profiles.json")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
LIFECYCLE_LOG_FILE = os.path.join(DATA_DIR, "lifecycle.log")
_inherited_log_enabled = os.environ.get(LOG_ENABLED_ENV)
_inherited_log_debug = os.environ.get(LOG_DEBUG_ENV)
_logging_enabled = (
    _inherited_log_enabled == "1" if _inherited_log_enabled in ("0", "1") else not IS_PACKAGED or "--debug" in sys.argv
)
_debug_logging = _inherited_log_debug == "1" if _inherited_log_debug in ("0", "1") else "--debug" in sys.argv
os.environ[LOG_ENABLED_ENV] = "1" if _logging_enabled else "0"
os.environ[LOG_DEBUG_ENV] = "1" if _debug_logging else "0"
LIFECYCLE_LOG_FILE = os.environ.get(LOG_PATH_ENV, LIFECYCLE_LOG_FILE)
os.environ[LOG_PATH_ENV] = LIFECYCLE_LOG_FILE
configure_logging(
    LIFECYCLE_LOG_FILE,
    enabled=_logging_enabled,
    debug_enabled=_debug_logging,
    console_enabled=_logging_enabled and __name__ == "__main__",
)
logger = get_logger("main")
console_logger = get_logger("console")


def _qt_message_handler(mode, context, message):
    level = {
        QtMsgType.QtDebugMsg: logger.debug,
        QtMsgType.QtInfoMsg: logger.info,
        QtMsgType.QtWarningMsg: logger.warning,
        QtMsgType.QtCriticalMsg: logger.error,
        QtMsgType.QtFatalMsg: logger.critical,
    }.get(mode, logger.warning)
    level("Qt message: %s", message)


qInstallMessageHandler(_qt_message_handler)

# Resolved against RESOURCE_DIR so it works both in dev and inside the
# packaged .exe.
DASHBOARD_FILE = os.path.join(RESOURCE_DIR, "dashboard_v2", "index.html")
if __name__ == "__main__":
    logger.debug("dashboard path=%s exists=%s", DASHBOARD_FILE, os.path.exists(DASHBOARD_FILE))

# App icon (window/taskbar). Use the PNG, which Qt core handles without an image
# format plugin in the packaged executable. Nuitka uses the ICO for the executable
# file icon during the build.
APP_ICON = os.path.join(RESOURCE_DIR, "assets", "icon.png")
TRAY_ICON = os.path.join(RESOURCE_DIR, "assets", "icon.ico")

# ── Version + project links ─────────────────────────────────────────────────
# APP_VERSION is the release tag without its leading "v" (e.g. tag "v0.2.31"
# matches APP_VERSION "0.2.31"). The comparison strips "vV" on both sides.
APP_VERSION = "0.2.31"
REPO_URL = "https://github.com/ErtisT127/VisualAudioOverlay"
# Latest-release JSON (no auth needed; 60 req/hr per IP is plenty for one check
# per launch). Used by the in-app update check to reach users who already have
# the app installed - we have no telemetry/emails, so this is the only channel.
REPO_LATEST_RELEASE_API = "https://api.github.com/repos/ErtisT127/VisualAudioOverlay/releases/latest"
SINGLE_INSTANCE_NAME = "VisualAudioOverlay.SingleInstance"
_UPDATE_CHECK_STARTED = False
# Footstep bands rev. 2026-07-02, based on spectral analysis of the actual CS2
# footstep assets (extracted from pak01_dir.vpk) plus published EQ guidance for
# the other games. Key findings: footstep energy spans ~150Hz-4kHz (soft/wet
# surfaces and cloth movement sit at 900Hz-8kHz, which the old 800-1000Hz caps
# cut off entirely), while rifle fire concentrates ABOVE 4kHz - so a 4kHz cap
# keeps gunshots out of band and max_amp handles their loudness. The 150Hz low
# cut sheds rumble/explosion low end.
#
# Every preset must list ALL five audio parameters. A preset that sets only some
# of them inherits the rest from whatever was selected before it, which leaks in
# one direction only: saved profiles do set all five, so switching from a profile
# to a preset used to carry the profile's sensitivity along with it.
#
# Sensitivity and gain are the same in every entry on purpose. The spectral work
# above measured frequency bands and the loudness gate; it says nothing about
# level, and how loud you run your system is a property of your headset, not of
# the game. They are spelled out per preset anyway so a future entry can override
# them without reintroducing the leak.
_PRESET_LEVEL_DEFAULTS = {"sensitivity": 0.005, "gain": 1.0}
SOUND_PRESETS = {
    "All Sounds": {
        **_PRESET_LEVEL_DEFAULTS,
        "freq_low": 20,
        "freq_high": 20000,
        "max_amp": 1.0,
    }
}
INITIAL_PROFILE_PRESETS = {
    "Footsteps - CS2": {"freq_low": 150, "freq_high": 4000, "max_amp": 0.15},
    "Footsteps - Valorant": {"freq_low": 150, "freq_high": 4000, "max_amp": 0.12},
    "Footsteps - Fortnite": {"freq_low": 150, "freq_high": 5000, "max_amp": 0.18},
    "Footsteps - General": {"freq_low": 150, "freq_high": 4000, "max_amp": 0.15},
}

# Saved profiles store SLIDER POSITIONS (that is what AR.addPreset reads out of
# the DOM), while audio_settings stores the real values the capture thread uses.
# These factors are the inverse of the conversions in dashboard_v2/script.js -
# setSensitivity divides by 10000, setGain by 10, setMaxAmp by 100 - so keep the
# two in step. The frequency sliders are already in real Hz.
PROFILE_SLIDER_SCALE = {
    "sensitivity": 10000,
    "gain": 10,
    "max_amp": 100,
    "freq_low": 1,
    "freq_high": 1,
}

AUDIO_PARAM_LIMITS = {
    "sensitivity": (float, 0.0001, 0.05),
    "gain": (float, 1.0, 50.0),
    "freq_low": (int, 20, 20000),
    "freq_high": (int, 20, 20000),
    "max_amp": (float, 0.01, 1.0),
}
PROFILE_PARAM_LIMITS = {
    "sensitivity": (int, 1, 500),
    "gain": (int, 10, 500),
    "freq_low": (int, 20, 20000),
    "freq_high": (int, 20, 20000),
    "max_amp": (int, 1, 100),
}


def _bounded_number(value, cast, minimum, maximum):
    if isinstance(value, bool):
        return None
    try:
        numeric = float(value)
    except TypeError, ValueError:
        return None
    if not math.isfinite(numeric) or not minimum <= numeric <= maximum:
        return None
    if cast is int:
        if not numeric.is_integer():
            return None
        return int(numeric)
    return float(numeric)


def _normalize_audio_settings(values, fallback):
    """Return a full valid runtime audio mapping based on `fallback`."""
    result = dict(fallback)
    if not isinstance(values, dict):
        return result
    for key, (cast, minimum, maximum) in AUDIO_PARAM_LIMITS.items():
        if key not in values:
            continue
        normalized = _bounded_number(values[key], cast, minimum, maximum)
        if normalized is not None:
            result[key] = normalized
    if result["freq_low"] > result["freq_high"]:
        result["freq_low"] = fallback["freq_low"]
        result["freq_high"] = fallback["freq_high"]
    return result


def _normalize_profile(values):
    """Validate one complete profile in dashboard slider units."""
    if not isinstance(values, dict):
        return None
    name = values.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    result = {"name": name.strip()}
    for key, (cast, minimum, maximum) in PROFILE_PARAM_LIMITS.items():
        if key not in values:
            return None
        normalized = _bounded_number(values[key], cast, minimum, maximum)
        if normalized is None:
            return None
        result[key] = normalized
    if result["freq_low"] > result["freq_high"]:
        return None
    return result


def _normalize_hex_color(value, fallback="#9751F2"):
    if not isinstance(value, str):
        return fallback
    candidate = value.strip()
    if len(candidate) != 7 or not candidate.startswith("#"):
        return fallback
    try:
        int(candidate[1:], 16)
    except ValueError:
        return fallback
    return candidate.upper()


def _initial_profiles() -> dict:
    """Build the editable profiles written on a genuinely fresh install."""
    profiles = {}
    for name, band in INITIAL_PROFILE_PRESETS.items():
        values = {**_PRESET_LEVEL_DEFAULTS, **band}
        profiles[name] = {
            "name": name,
            "sensitivity": round(values["sensitivity"] * PROFILE_SLIDER_SCALE["sensitivity"]),
            "gain": round(values["gain"] * PROFILE_SLIDER_SCALE["gain"]),
            "freq_low": values["freq_low"],
            "freq_high": values["freq_high"],
            "max_amp": round(values["max_amp"] * PROFILE_SLIDER_SCALE["max_amp"]),
        }
    return profiles


def _load_json_mapping(path: str) -> dict:
    """Read a JSON object, returning an empty mapping for missing/bad files."""
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            logger.debug("configuration loaded path=%s", path)
            return data
        logger.warning("Ignoring non-object config file: %s", path)
    except FileNotFoundError:
        pass
    except Exception:
        logger.exception("Config read skipped for %s", path)
    return {}


def _save_json_mapping(path: str, data: dict) -> bool:
    """Atomically write a JSON object, keeping a failed write from truncating it."""
    temporary_path = None
    try:
        directory = os.path.dirname(path) or "."
        os.makedirs(directory, exist_ok=True)
        fd, temporary_path = tempfile.mkstemp(prefix=os.path.basename(path) + ".", suffix=".tmp", dir=directory)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
        logger.debug("configuration saved path=%s", path)
        return True
    except Exception:
        logger.exception("Config write skipped for %s", path)
        return False
    finally:
        if temporary_path:
            with contextlib.suppress(OSError):
                os.unlink(temporary_path)


def _find_screen_at_centre(screens, cx: float, cy: float):
    """Screen whose geometry contains (cx, cy), or None when off every screen.

    QRect.contains() only accepts whole pixels, so the fractional centre is
    rounded before the point test (PyQt6 raises on a raw float)."""
    for screen in screens:
        if screen.geometry().contains(round(cx), round(cy)):
            return screen
    return None


# ── Bridge ─────────────────────────────────────────────────────────────────
# This object is injected into the JS context as `window.bridge`.
# JS calls Python methods via:  bridge.start_radar()
# Python pushes updates to JS via signals, which JS subscribes to:
#   bridge.statusChanged.connect(function(msg, isActive) { ... })


def _parse_version(tag: str):
    """'v0.2.1' or '0.2.1' -> (0, 2, 1). Non-numeric parts become 0 so a weird
    tag never crashes the check. Returns () if nothing parseable."""
    nums = []
    for part in tag.lstrip("vV").split("."):
        digits = "".join(c for c in part if c.isdigit())
        if digits == "":
            break
        nums.append(int(digits))
    return tuple(nums)


class UpdateCheckThread(QThread):
    """Fetches the latest GitHub release tag on a background thread and, if it is
    newer than APP_VERSION, emits (version, html_url). Fails silently on any
    error (offline, rate-limited, GitHub down) so it is never intrusive."""

    updateFound = pyqtSignal(str, str)  # (latest_version, release_page_url)

    def run(self):
        try:
            import json as _json
            import urllib.request

            req = urllib.request.Request(
                REPO_LATEST_RELEASE_API,
                headers={
                    "Accept": "application/vnd.github+json",
                    "User-Agent": f"VisualAudioOverlay/{APP_VERSION}",
                },
            )
            with urllib.request.urlopen(req, timeout=6) as resp:
                data = _json.loads(resp.read().decode("utf-8"))

            tag = (data.get("tag_name") or "").strip()
            url = data.get("html_url") or REPO_URL + "/releases/latest"
            if not self.isInterruptionRequested() and tag and _parse_version(tag) > _parse_version(APP_VERSION):
                self.updateFound.emit(tag.lstrip("vV"), url)
        except Exception as exc:  # noqa: BLE001 - update checks are optional.
            # Optional network checks stay silent in the UI, but remain diagnosable.
            logger.warning("update check failed: %s", exc)


class ProgramListThread(QThread):
    """Enumerate audio sessions off the GUI thread (COM/WASAPI may block)."""

    result = pyqtSignal(str)

    def run(self):
        try:
            from process_loopback import list_audio_programs

            names = [p["name"] for p in list_audio_programs()]
        except Exception:
            logger.exception("Program enumeration failed")
            names = []
        if not self.isInterruptionRequested():
            self.result.emit(json.dumps(names))


class MonoDeviceListThread(QThread):
    """Enumerate soundcard devices without blocking the GUI event loop."""

    result = pyqtSignal(str)

    def run(self):
        try:
            from mono_output import output_device_state

            state = output_device_state()
        except Exception:
            logger.exception("Mono device enumeration failed")
            state = {"devices": [], "default": None, "cable": None}
        if not self.isInterruptionRequested():
            self.result.emit(json.dumps(state))


class GlobalHotkeyFilter(QAbstractNativeEventFilter):
    """Register one Windows global hotkey and forward WM_HOTKEY to the app."""

    _WM_HOTKEY = 0x0312
    _MOD_NOREPEAT = 0x4000
    _MODIFIERS: ClassVar[dict[str, int]] = {
        "CTRL": 0x0002,
        "ALT": 0x0001,
        "SHIFT": 0x0004,
    }
    _VK_NAMES: ClassVar[dict[str, int]] = {
        **{f"F{i}": 0x6F + i for i in range(1, 25)},
        **{chr(code): code for code in range(ord("A"), ord("Z") + 1)},
        **{str(number): ord(str(number)) for number in range(10)},
        "SPACE": 0x20,
        "INSERT": 0x2D,
        "DELETE": 0x2E,
        "HOME": 0x24,
        "END": 0x23,
        "PAGEUP": 0x21,
        "PAGEDOWN": 0x22,
        "UP": 0x26,
        "DOWN": 0x28,
        "LEFT": 0x25,
        "RIGHT": 0x27,
    }
    _TEXT_KEYS: ClassVar[set[str]] = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789") | {"SPACE"}
    _RESERVED: ClassVar[set[str]] = {"ALT+F4", "CTRL+ALT+DELETE"}

    def __init__(self, owner):
        super().__init__()
        self.owner = owner
        self.hotkey_id = 0x5641
        self.registered = False
        self._user32 = ctypes.windll.user32 if sys.platform == "win32" else None

    @classmethod
    def parse(cls, combo):
        parts = [p.strip().upper() for p in str(combo or "").split("+") if p.strip()]
        if len(parts) < 1:
            return None
        key = parts[-1]
        if key not in cls._VK_NAMES:
            return None
        mods = 0
        seen = set()
        for modifier in parts[:-1]:
            if modifier not in cls._MODIFIERS or modifier in seen:
                return None
            seen.add(modifier)
            mods |= cls._MODIFIERS[modifier]
        normalized = "+".join([m for m in cls._MODIFIERS if m in seen] + [key])
        if normalized in cls._RESERVED:
            return None
        if key in cls._TEXT_KEYS and not mods & (cls._MODIFIERS["CTRL"] | cls._MODIFIERS["ALT"]):
            return None
        return mods, cls._VK_NAMES[key], normalized

    def unregister(self):
        if self.registered and self._user32:
            self._user32.UnregisterHotKey(int(self.owner.winId()), self.hotkey_id)
            controller = getattr(self.owner, "hotkey_controller", None)
            logger.info("global hotkey unregistered combo=%s", getattr(controller, "combo", ""))
        self.registered = False

    def register(self, combo):
        parsed = self.parse(combo)
        if not parsed or not self._user32:
            logger.warning("global hotkey registration skipped combo=%r", combo)
            return False
        self.unregister()
        modifiers, vk, _ = parsed
        ok = bool(
            self._user32.RegisterHotKey(
                int(self.owner.winId()),
                self.hotkey_id,
                modifiers | self._MOD_NOREPEAT,
                vk,
            )
        )
        self.registered = ok
        if ok:
            logger.info("global hotkey registered combo=%s", parsed[2])
        else:
            logger.warning(
                "global hotkey registration failed combo=%s winerror=%s",
                parsed[2],
                ctypes.get_last_error(),
            )
        return ok

    def nativeEventFilter(self, event_type, message):
        if (
            event_type
            in (
                b"windows_generic_MSG",
                "windows_generic_MSG",
                b"windows_dispatcher_MSG",
                "windows_dispatcher_MSG",
            )
            and message
            and self._user32
        ):
            msg = ctypes.cast(int(message), ctypes.POINTER(wintypes.MSG)).contents
            if msg.message == self._WM_HOTKEY and msg.wParam == self.hotkey_id:
                controller = getattr(self.owner, "hotkey_controller", None)
                if controller is not None:
                    controller.handle_trigger()
                return True, 0
        return False, 0


class HotkeyController:
    """Single source of truth for shortcut configuration and registration state."""

    def __init__(self, app, configured_combo):
        self.app = app
        self.filter = GlobalHotkeyFilter(app)
        self.combo = ""
        self.status = "unbound"
        self.editing = False
        self.registered = False
        self._edit_combo = ""
        self._edit_status = "unbound"
        self._startup_registration_done = False
        self._load(configured_combo)

    @property
    def state(self):
        return {
            "combo": self.combo,
            "status": self.status,
            "editing": self.editing,
            "registered": self.registered,
            "error": "",
        }

    def state_json(self, error=""):
        state = dict(self.state)
        state["error"] = error or ""
        return json.dumps(state)

    def _publish(self, error=""):
        if hasattr(self.app, "bridge"):
            self.app.bridge.hotkeyStateChanged.emit(self.state_json(error))
        return self.state_json(error)

    def _load(self, configured_combo):
        raw = str(configured_combo or "").strip()
        if not raw:
            self.status = "unbound"
            logger.info("hotkey loaded state=unbound")
            return
        parsed = GlobalHotkeyFilter.parse(raw)
        if not parsed:
            self.status = "unbound"
            logger.warning("invalid saved hotkey ignored combo=%r", raw)
            return
        self.combo = parsed[2]
        if self.filter.register(self.combo):
            self.registered = True
            self.status = "active"
            logger.info("hotkey loaded combo=%s state=active", self.combo)
        else:
            self.status = "unavailable"
            logger.warning("hotkey loaded combo=%s state=unavailable", self.combo)

    def ensure_registered(self):
        """Retry registration once the Qt window and native filter are ready."""
        if self.editing or not self.combo:
            return self.state_json()
        if self._startup_registration_done and self.registered:
            return self.state_json()
        self.filter.unregister()
        self.registered = False
        if self.filter.register(self.combo):
            self.registered = True
            self.status = "active"
            self._startup_registration_done = True
            logger.info("hotkey registration ready combo=%s state=active", self.combo)
            return self._publish()
        self.status = "unavailable"
        self.registered = False
        self._startup_registration_done = True
        logger.warning("hotkey registration retry unavailable combo=%s", self.combo)
        return self._publish("That shortcut is already in use by another application.")

    def _set_persisted_combo(self, combo):
        self.app.settings["hotkey"] = combo
        self.app._save_settings()

    def handle_trigger(self):
        if self.editing:
            logger.debug("hotkey ignored during editing combo=%s", self.combo)
            return
        logger.info("hotkey invoked combo=%s", self.combo)
        self.app.toggle_radar()

    def begin_edit(self):
        if self.editing:
            return self.state_json()
        self._edit_combo = self.combo
        self._edit_status = self.status
        self.editing = True
        self.filter.unregister()
        self.registered = False
        self.status = "editing"
        logger.info("hotkey edit started combo=%s", self._edit_combo)
        return self._publish()

    def commit(self, combo):
        if not self.editing:
            return self._publish("No hotkey edit is active.")
        parsed = GlobalHotkeyFilter.parse(combo)
        if not parsed:
            logger.warning("hotkey commit rejected combo=%r", combo)
            return self._publish("Use F1-F24 or a navigation key. Letters, numbers, and Space need Ctrl or Alt.")
        normalized = parsed[2]
        if not self.filter.register(normalized):
            logger.warning("hotkey commit unavailable combo=%s", normalized)
            return self._publish("That shortcut is already in use by another application.")
        self.combo = normalized
        self.registered = True
        self.status = "active"
        self.editing = False
        self._set_persisted_combo(normalized)
        logger.info("hotkey committed combo=%s state=active", normalized)
        return self._publish()

    def cancel(self):
        if not self.editing:
            return self._publish()
        self.combo = self._edit_combo
        self.editing = False
        self.registered = False
        if self.combo and self.filter.register(self.combo):
            self.registered = True
            self.status = "active"
        elif self.combo:
            self.status = "unavailable"
            logger.warning("hotkey cancel restore unavailable combo=%s", self.combo)
        else:
            self.status = "unbound"
        logger.info("hotkey edit cancelled restored_combo=%s state=%s", self.combo, self.status)
        return self._publish()

    def clear(self):
        self.filter.unregister()
        self.combo = ""
        self.editing = False
        self.registered = False
        self.status = "unbound"
        self._set_persisted_combo("")
        logger.info("hotkey cleared state=unbound")
        return self._publish()


class Bridge(QObject):
    # Signals → pushed to JS
    statusChanged = pyqtSignal(str, bool)  # (message, isActive)
    deviceChanged = pyqtSignal(str)  # detected device name
    profilesChanged = pyqtSignal(str)  # full profiles dict as JSON
    monitorsChanged = pyqtSignal(str)  # list of monitors as JSON
    selectedMonitorChanged = pyqtSignal(int)
    presetsChanged = pyqtSignal(str)  # permanent builtin catalog as JSON
    programsChanged = pyqtSignal(str)  # running audio programs as JSON
    overlayPositionChanged = pyqtSignal(str)  # overlay position/state as JSON
    monoStateChanged = pyqtSignal(str)  # mono-output devices + cable state as JSON
    updateAvailable = pyqtSignal(str, str)  # (latest_version, release_page_url)
    appearanceChanged = pyqtSignal(str)  # saved overlay accent colour + thickness as JSON
    selectedPresetChanged = pyqtSignal(str)  # internal preset option key
    audioSettingsChanged = pyqtSignal(str)  # all five live audio params, so JS moves the sliders
    operationBusyChanged = pyqtSignal(bool)  # Start/End lifecycle lock
    operationStateChanged = pyqtSignal(str)  # idle/starting/running/restarting/stopping/closing
    hotkeyStateChanged = pyqtSignal(str)

    def __init__(self, app: AudioRadarApp):
        super().__init__()
        self._app = app

    # ── Lifecycle ─────────────────────────────────────────────────────
    @pyqtSlot()
    def start_radar(self):
        self._app.start_radar()

    @pyqtSlot()
    def stop_radar(self):
        self._app.stop_radar()

    @pyqtSlot(result=str)
    def get_hotkey_state(self):
        return self._app.hotkey_controller.state_json()

    @pyqtSlot(result=str)
    def begin_hotkey_edit(self):
        return self._app.hotkey_controller.begin_edit()

    @pyqtSlot(str, result=str)
    def commit_hotkey(self, combo: str):
        return self._app.hotkey_controller.commit(combo)

    @pyqtSlot(result=str)
    def cancel_hotkey_edit(self):
        return self._app.hotkey_controller.cancel()

    @pyqtSlot(result=str)
    def clear_hotkey(self):
        return self._app.hotkey_controller.clear()

    # ── Audio Settings ────────────────────────────────────────────────
    @pyqtSlot(float)
    def set_sensitivity(self, val: float):
        self._app.set_audio_param("sensitivity", val)

    @pyqtSlot(float)
    def set_gain(self, val: float):
        self._app.set_audio_param("gain", val)

    @pyqtSlot(int, int)
    def set_freq_range(self, low: int, high: int):
        self._app.set_audio_params({"freq_low": low, "freq_high": high})

    @pyqtSlot(float)
    def set_max_amplitude(self, val: float):
        self._app.set_audio_param("max_amp", val)

    @pyqtSlot()
    def commit_audio_settings(self):
        self._app.commit_audio_settings()

    @pyqtSlot(str)
    def apply_preset(self, name: str):
        self._app.apply_preset(name)

    @pyqtSlot(str)
    def set_selected_preset(self, name: str):
        self._app.set_selected_preset(name)

    @pyqtSlot(str)
    def set_preset_state(self, json_str: str):
        try:
            state = json.loads(json_str)
            if isinstance(state, dict):
                logger.debug(
                    "preset state changed id=%s dirty=%s",
                    state.get("id", "builtin:all-sounds"),
                    bool(state.get("dirty", False)),
                )
                self._app.settings["preset_state"] = {
                    "id": str(state.get("id", "builtin:all-sounds")),
                    "dirty": bool(state.get("dirty", False)),
                }
                self._app.preset_state = dict(self._app.settings["preset_state"])
                # Keep the preset state on the same debounced persistence path as
                # live audio settings. This is important when Reset clears dirty:
                # an in-memory-only update would return after the next restart.
                self._app._queue_settings_save()
            else:
                logger.warning("invalid preset state shape ignored")
        except TypeError, ValueError, json.JSONDecodeError:
            logger.warning("invalid preset state payload ignored")

    @pyqtSlot(bool)
    def set_invert(self, invert: bool):
        logger.info("invert direction changed enabled=%s", bool(invert))
        self._app.invert_direction = invert

    @pyqtSlot(int)
    def set_monitor(self, idx: int):
        logger.info("monitor changed index=%s", idx)
        self._app.set_monitor(idx)

    @pyqtSlot(str)
    def set_program(self, value: str):
        """Capture target chosen in the UI. 'all' (or empty) = whole-system audio."""
        self._app.set_program(None if value in ("", "all") else value)

    @pyqtSlot()
    def refresh_programs(self):
        """Re-enumerate running audio programs. JS calls this when the user opens
        the Program dropdown, so the list is live (a program only appears once it
        is actually playing audio)."""
        self._app.emit_programs()

    @pyqtSlot()
    def refresh_monitors(self):
        """Refresh the monitor list when its dropdown is opened."""
        self._app.emit_monitors()

    # ── Mono output (single-sided listeners) ──────────────────────────
    @pyqtSlot(bool)
    def set_mono_enabled(self, enabled: bool):
        """Turn the in-app mono down-mix on/off. Applies live while running."""
        self._app.set_mono_enabled(enabled)

    @pyqtSlot(str)
    def set_mono_output(self, device: str):
        """Choose which real device the mono mix plays to. '' = system default."""
        self._app.set_mono_output(device)

    @pyqtSlot()
    def refresh_mono_devices(self):
        self._app.emit_mono_state()

    @pyqtSlot()
    def install_vbcable(self):
        """Launch the bundled VB-CABLE installer (UAC-elevated) so mono output can
        route the game away from the headphones. Falls back to the download page
        if the installer isn't bundled in this build."""
        self._app.install_vbcable()

    # ── Version / external links ──────────────────────────────────────
    @pyqtSlot(result=str)
    def get_app_version(self) -> str:
        return APP_VERSION

    @pyqtSlot(str)
    def open_url(self, url: str):
        """Open an https link in the user's real browser, not inside the webview."""
        if isinstance(url, str) and url.startswith("https://"):
            import webbrowser

            webbrowser.open(url)

    @pyqtSlot(bool)
    def set_overlay_drag_enabled(self, enabled: bool):
        logger.info("overlay drag changed enabled=%s", bool(enabled))
        self._app.set_overlay_drag_enabled(enabled)

    @pyqtSlot(int, int)
    def set_overlay_position(self, x: int, y: int):
        logger.info("overlay position requested x=%s y=%s", x, y)
        self._app.move_overlay(x, y)

    @pyqtSlot(int, int)
    def nudge_overlay(self, dx: int, dy: int):
        logger.debug("overlay nudge requested dx=%s dy=%s", dx, dy)
        self._app.nudge_overlay(dx, dy)

    @pyqtSlot()
    def reset_overlay_position(self):
        logger.info("overlay position reset requested")
        self._app.reset_overlay_position()

    # ── Overlay Appearance ────────────────────────────────────────────
    @pyqtSlot(str)
    def set_accent_color(self, hex_color: str):
        self._app.set_accent_color(hex_color)

    @pyqtSlot(int)
    def set_stroke_width(self, width: int):
        self._app.set_stroke_width(width)

    @pyqtSlot()
    def commit_appearance(self):
        self._app.commit_appearance()

    # ── Profiles ──────────────────────────────────────────────────────
    @pyqtSlot(str)
    def save_profile(self, json_str: str):
        """Persist the audio settings captured by the preset editor."""
        try:
            data = _normalize_profile(json.loads(json_str))
            if data is None:
                logger.warning("invalid profile payload ignored")
                return
            name = data["name"]
            logger.info("preset saved name=%s", name)
            self._app.profiles[name] = data
            self._app._save_profiles()
            self._app.set_selected_preset(f"profile:{name}")
            self.profilesChanged.emit(json.dumps(self._app.profiles))
        except Exception:
            logger.exception("save_profile failed")

    @pyqtSlot(str, result=str)
    def get_profile(self, name: str) -> str:
        """Returns a single profile as JSON string (for loading into UI)."""
        p = self._app.profiles.get(name, {})
        return json.dumps(p)

    @pyqtSlot(str)
    def delete_profile(self, name: str):
        if name in self._app.profiles:
            logger.info("preset deleted name=%s", name)
            del self._app.profiles[name]
            self._app._save_profiles()
            # Deleting the profile that is currently selected would leave a name
            # in settings.json that no dropdown entry can ever match, so nothing
            # would be restored on the next launch and the label would drift from
            # the running band. Fall back to the neutral preset.
            if self._app.selected_preset == f"profile:{name}":
                self._app.set_selected_preset("builtin:all-sounds")
                self._app.emit_selected_preset()
            self.profilesChanged.emit(json.dumps(self._app.profiles))

    # ── Init Data Request ─────────────────────────────────────────────
    @pyqtSlot()
    def request_initial_data(self):
        """
        JS calls this once on page load.
        Python responds by emitting all initial state signals.
        """
        # Monitors
        self._app.emit_monitors()
        self.selectedMonitorChanged.emit(int(self._app.selected_monitor))
        # Preset/profile selection saved from the last session. Emitted BEFORE the
        # two lists that build the dropdown, so JS knows what to re-select as soon
        # as the matching option appears (JS also re-checks on every rebuild, so
        # the order here is a convenience, not a requirement).
        self._app.emit_selected_preset()

        # Live audio parameters (sliders). Restored values, not a preset's - the
        # saved name is only re-selected in the dropdown, never re-applied, so a
        # profile cannot overwrite settings changed after it was chosen.
        self._app.emit_audio_settings()

        self.hotkeyStateChanged.emit(self._app.hotkey_controller.state_json())

        # Profiles
        self.profilesChanged.emit(json.dumps(self._app.profiles))

        # Permanent builtin preset. Other initial presets live in profiles.json.
        self.presetsChanged.emit(
            json.dumps(
                [
                    {
                        "id": "builtin:all-sounds",
                        "name": "All Sounds",
                        "can_delete": False,
                    }
                ]
            )
        )

        # Running audio programs (for per-app capture)
        self._app.emit_programs()

        # Overlay appearance (accent colour + thickness), restored from settings
        self._app.emit_appearance()

        # Overlay position
        self._app.emit_overlay_position()

        # Mono-output devices + VB-CABLE detection
        self._app.emit_mono_state()


# ── Main Application ────────────────────────────────────────────────────────


class AudioRadarApp(QMainWindow):
    DEFAULT_WINDOW_WIDTH = 1100
    DEFAULT_WINDOW_HEIGHT = 720
    MIN_WINDOW_WIDTH = DEFAULT_WINDOW_WIDTH
    MIN_WINDOW_HEIGHT = DEFAULT_WINDOW_HEIGHT

    def __init__(self, single_instance_server=None):
        super().__init__()
        logger.info("application window initialization started")
        self.setWindowTitle("Visual Audio Overlay")
        if os.path.exists(APP_ICON):
            self.setWindowIcon(QIcon(APP_ICON))
        self.setMinimumSize(self.MIN_WINDOW_WIDTH, self.MIN_WINDOW_HEIGHT)
        self.resize(self.DEFAULT_WINDOW_WIDTH, self.DEFAULT_WINDOW_HEIGHT)
        self._closing_for_exit = False
        self._exit_finalized = False
        self._exit_finalize_pending = False
        self.radar_operation_state = "idle"
        self._capture_watchdog = QTimer(self)
        self._capture_watchdog.setSingleShot(True)
        self._capture_watchdog.setInterval(10000)
        self._capture_watchdog.timeout.connect(self._on_capture_timeout)
        self._audio_consume_timer = QTimer(self)
        self._audio_consume_timer.setInterval(15)
        self._audio_consume_timer.timeout.connect(self._consume_latest_audio)
        # Chromium keeps its renderer/GPU context alive while a WebEngine view is
        # merely hidden.  Freeze the page after a short debounce when the
        # dashboard is minimized or sent to the tray, and reactivate it when the
        # window returns.  The debounce avoids repeatedly toggling lifecycle state
        # when a user briefly minimizes/restores the window.
        self._dashboard_frozen = False
        self._dashboard_freeze_timer = QTimer(self)
        self._dashboard_freeze_timer.setSingleShot(True)
        self._dashboard_freeze_timer.setInterval(600)
        self._dashboard_freeze_timer.timeout.connect(self._freeze_dashboard_if_hidden)
        self._program_list_thread = None
        self._mono_device_thread = None
        self._mono_refresh_pending = False
        self._capture_generation = 0
        self._active_capture_generation = None
        self._watchdog_generation = None
        self._restart_requested = False
        self._capture_terminal_error = None
        self._single_instance_server = single_instance_server
        if self._single_instance_server is not None:
            self._single_instance_server.newConnection.connect(self._on_single_instance_connection)
        self._setup_tray_icon()

        self.invert_direction = False
        self.selected_monitor = 0
        self._selected_monitor_name = None
        self.selected_program = None  # None = whole-system audio; else a program name
        fresh_install = not os.path.exists(PROFILES_FILE) and not os.path.exists(SETTINGS_FILE)
        self.profiles = self._load_profiles()
        self.settings = self._load_settings()
        if "hotkey" not in self.settings:
            self.settings["hotkey"] = "F8"
            self._save_settings()
        # Monitor and program are session-only choices; never restore stale
        # values from a previous run.
        removed_session_keys = any(
            self.settings.pop(key, None) is not None for key in ("selected_monitor", "selected_program")
        )
        if fresh_install:
            self.profiles = _initial_profiles()
            self._save_profiles()
            self.settings["audio_settings"] = dict(SOUND_PRESETS["All Sounds"])
            self._save_settings()
        elif removed_session_keys:
            self._save_settings()
        self.radar_active = False

        # Live audio parameters, held here as the source of truth so they survive
        # thread recreation. The capture thread is rebuilt fresh (with library
        # defaults) on every Stop and on every mid-session restart, so these are
        # re-applied in _start_capture_thread - otherwise a preset would silently
        # revert to "all frequencies" after the first stop/start. Defaults match
        # the dashboard's initial slider positions.
        self.audio_settings = {
            "sensitivity": 0.005,  # sens slider 50 / 10000
            "gain": 1.0,  # gain slider 10 / 10
            "freq_low": 20,  # All Sounds default
            "freq_high": 20000,
            "max_amp": 1.0,  # max-amp slider 100 / 100
        }
        # ...and restored from settings.json on top of those defaults, so a tuned
        # slider survives a restart. Without this the only thing that came back
        # was the preset NAME, which then had to be re-applied to mean anything -
        # and re-applying a saved profile overwrote the colour and thickness the
        # user had changed since.
        self._load_audio_settings()

        # Slider drags fire one bridge call per pixel of movement, so writing
        # settings.json on every one would hammer the disk the way persisting
        # every drag frame did for the overlay (issue #3). Coalesce into a single
        # write once the user stops moving.
        self._settings_save_timer = QTimer(self)
        self._settings_save_timer.setSingleShot(True)
        self._settings_save_timer.setInterval(600)
        self._settings_save_timer.timeout.connect(self._write_pending_saves)

        # Which entry of the preset/profile dropdown was last selected. Persisted
        # in settings.json and re-selected by the dashboard on load, so the choice
        # survives a restart instead of silently falling back to the first option.
        # It is a LABEL only - the values it stands for are restored separately via
        # audio_settings above, because re-applying the entry would clobber
        # anything the user changed after picking it. Defaults to "All Sounds",
        # which is what an unrestored dropdown already displays.
        saved_preset = self.settings.get("selected_preset")
        self.selected_preset = saved_preset if isinstance(saved_preset, str) and saved_preset else "builtin:all-sounds"
        saved_state = self.settings.get("preset_state")
        self.preset_state = (
            saved_state
            if isinstance(saved_state, dict)
            else {
                "id": self.selected_preset,
                "dirty": False,
            }
        )
        value = self.preset_state.get("id", "")
        valid = value == "builtin:all-sounds" or (
            isinstance(value, str) and value.startswith("profile:") and value[8:] in self.profiles
        )
        if not valid:
            self.preset_state = {"id": "builtin:all-sounds", "dirty": False}
        else:
            self.preset_state = {
                "id": value,
                "dirty": bool(self.preset_state.get("dirty", False)),
            }
        self.selected_preset = self.preset_state["id"]
        self.settings["selected_preset"] = self.selected_preset
        self.settings["preset_state"] = dict(self.preset_state)

        # Mono output (single-sided listeners). Persisted in settings.json so the
        # user's choice survives restarts; applied to the audio thread on Start.
        self.mono_enabled = bool(self.settings.get("mono_enabled", False))
        self.mono_device = self.settings.get("mono_device") or None

        # Native Direct2D/DirectComposition overlay.  There is intentionally no
        # QWidget fallback: a failed native renderer must be visible and must not
        # silently reintroduce the compositor path this backend replaces.
        try:
            self.overlay = NativeOverlay()
        except Exception as exc:
            logger.exception("native overlay initialization failed")
            QMessageBox.critical(
                self,
                "Visual Audio Overlay",
                f"Unable to initialize the native overlay renderer.\n\n{exc}",
            )
            raise RuntimeError("native overlay initialization failed") from exc

        # Overlay appearance (accent colour + stroke). Persisted in settings.json so
        # the look survives restarts; applied to the overlay now and pushed to the
        # dashboard via emit_appearance() on load. Default matches the UI swatch.
        self.accent_color = _normalize_hex_color(self.settings.get("accent_color"))
        self.stroke_width = _bounded_number(self.settings.get("stroke_width", 6), int, 1, 20) or 6
        self.overlay.set_accent_color(self.accent_color)
        self.overlay.set_stroke_width(self.stroke_width)

        # Audio thread (starts idle, no capture yet)
        self._new_audio_thread()

        # Bridge object exposed to JS
        self.bridge = Bridge(self)
        self.overlay.positionChanged.connect(self.on_overlay_position_changed)
        self.overlay.positionPreview.connect(self.on_overlay_position_preview)

        # Keep the monitor model and the persisted overlay position valid when
        # Windows hot-plugs a display or changes projection mode.  QScreen
        # geometry signals are preferred over polling so a frozen dashboard
        # does not hide topology changes from the native overlay.
        self._screen_watch_ids = set()
        self._screen_change_timer = QTimer(self)
        self._screen_change_timer.setSingleShot(True)
        self._screen_change_timer.setInterval(120)
        self._screen_change_timer.timeout.connect(self._reconcile_screen_layout)
        app = QApplication.instance()
        if app is not None:
            try:
                app.screenAdded.connect(self._on_screen_added)
                app.screenRemoved.connect(self._on_screen_removed)
                app.primaryScreenChanged.connect(self._on_primary_screen_changed)
            except AttributeError, TypeError:
                logger.debug("screen topology signals unavailable", exc_info=True)
            for screen in app.screens():
                self._watch_screen_geometry(screen)

        # WebEngine view
        self.view = QWebEngineView()
        # Keep Chromium's CSS viewport at its native zoom.  A stale page zoom
        # survives a renderer restart and is then bitmap-scaled by Windows,
        # which presents as a blurry dashboard with shifted grid edges.
        self.view.setZoomFactor(1.0)

        # WebChannel - registers `bridge` as `window.bridge` in JS
        self.channel = QWebChannel()
        self.channel.registerObject("bridge", self.bridge)
        self.view.page().setWebChannel(self.channel)

        self.setCentralWidget(self.view)
        self._dashboard_window_handle = None
        self._connect_dashboard_screen_signal()

        configured_hotkey = self.settings.get("hotkey", "F8")
        self.hotkey_controller = HotkeyController(self, configured_hotkey)
        QApplication.instance().installNativeEventFilter(self.hotkey_controller.filter)
        # Register once more after the native filter and window event loop are
        # ready.  This avoids losing startup WM_HOTKEY messages on Windows.
        QTimer.singleShot(0, self.hotkey_controller.ensure_registered)

        # Load the dashboard HTML
        self.view.loadFinished.connect(self._redraw_dashboard_preview)
        self.view.setUrl(QUrl.fromLocalFile(DASHBOARD_FILE))

        # Check GitHub for a newer release in the background. If found, the bridge
        # forwards it to JS, which shows a small dismissible "update available"
        # banner. Silent on any failure - never blocks or nags. Kept as an
        # attribute so the QThread isn't garbage-collected mid-run.
        global _UPDATE_CHECK_STARTED
        self.update_thread = None
        if not _UPDATE_CHECK_STARTED:
            _UPDATE_CHECK_STARTED = True
            self.update_thread = UpdateCheckThread(self)
            self.update_thread.updateFound.connect(self.bridge.updateAvailable)
            self.update_thread.finished.connect(self._on_update_thread_finished)
            self.update_thread.start()
        logger.info("application window initialization completed")

    def toggle_radar(self):
        if self.radar_operation_state in ("starting", "running", "restarting"):
            self.stop_radar()
        else:
            self.start_radar()

    def _setup_tray_icon(self):
        icon_path = TRAY_ICON if os.path.exists(TRAY_ICON) else APP_ICON
        self.tray_icon = QSystemTrayIcon(QIcon(icon_path), self)
        self.tray_icon.setToolTip("Visual Audio Overlay")

        menu = QMenu(self)
        show_action = QAction("Show", self)
        show_action.triggered.connect(self._restore_from_tray)
        exit_action = QAction("Exit", self)
        exit_action.triggered.connect(self._exit_from_tray)
        menu.addAction(show_action)
        menu.addAction(exit_action)
        self.tray_icon.setContextMenu(menu)
        self.tray_icon.activated.connect(self._on_tray_activated)
        self.tray_icon.show()

    def _restore_from_tray(self):
        self._set_dashboard_frozen(False)
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _freeze_dashboard_if_hidden(self):
        """Freeze WebEngine only if the dashboard stayed hidden/minimized."""
        if self._closing_for_exit or (self.isVisible() and not self.isMinimized()):
            return
        self._set_dashboard_frozen(True)

    def _schedule_dashboard_freeze(self):
        """Debounce a transition to the hidden/minimized dashboard state."""
        if self._closing_for_exit:
            return
        self._dashboard_freeze_timer.start()

    def _set_dashboard_frozen(self, frozen: bool):
        """Set the WebEngine page lifecycle state without affecting app state.

        The dashboard's source of truth remains on ``AudioRadarApp``/``Bridge``;
        after thawing we replay the same initial-state signals used on first page
        load so controls cannot remain stale after being paused.
        """
        frozen = bool(frozen)
        self._dashboard_freeze_timer.stop()
        if frozen == self._dashboard_frozen:
            return

        view = getattr(self, "view", None)
        page = view.page() if view is not None else None
        lifecycle = getattr(QWebEnginePage, "LifecycleState", None)
        target = getattr(lifecycle, "Frozen" if frozen else "Active", None)
        setter = getattr(page, "setLifecycleState", None)
        if target is None or setter is None:
            logger.debug("WebEngine lifecycle state is unavailable; frozen=%s", frozen)
            return
        try:
            setter(target)
        except Exception:
            # Freezing is an optional resource optimization.  Never let an
            # unsupported QtWebEngine build break dashboard visibility or exit.
            logger.exception("failed to set WebEngine lifecycle state frozen=%s", frozen)
            return

        self._dashboard_frozen = frozen
        logger.debug("dashboard WebEngine lifecycle=%s", "Frozen" if frozen else "Active")
        # Keep the page-side refresh scheduler in sync with the native lifecycle
        # state.  ``document.hidden`` is not guaranteed to change for tray hides,
        # so this explicit hook is the source of truth for dropdown polling.
        runner = getattr(page, "runJavaScript", None)
        if runner is not None:
            try:
                runner(f"if (window.setDashboardFrozen) window.setDashboardFrozen({'true' if frozen else 'false'});")
            except Exception:
                logger.debug("dashboard refresh scheduler sync skipped", exc_info=True)
        if not frozen:
            # Signals are no-ops until the page has established its WebChannel;
            # request_initial_data() remains the canonical synchronization path.
            bridge = getattr(self, "bridge", None)
            if bridge is not None:
                QTimer.singleShot(0, bridge.request_initial_data)
            # A frozen Chromium page can resume with a discarded 2D canvas
            # backing surface even though the DOM and WebChannel state survive.
            # Redraw after activation so the customization preview cannot stay
            # blank until the next user interaction.
            redraw = getattr(self, "_redraw_dashboard_preview", None)
            if redraw is not None:
                QTimer.singleShot(40, redraw)
            refresh = getattr(self, "_refresh_dashboard_viewport", None)
            if refresh is not None:
                QTimer.singleShot(100, refresh)

    def _redraw_dashboard_preview(self, ok=True):
        """Repaint the dashboard canvas after load or WebEngine thaw."""
        if ok is False or self._closing_for_exit:
            return
        view = getattr(self, "view", None)
        page = view.page() if view is not None else None
        runner = getattr(page, "runJavaScript", None)
        if runner is None:
            return
        try:
            frozen = bool(getattr(self, "_dashboard_frozen", False))
            runner(f"if (window.setDashboardFrozen) window.setDashboardFrozen({'true' if frozen else 'false'});")
            if frozen:
                return
            runner("if (typeof drawPreview === 'function') window.requestAnimationFrame(drawPreview);")
            self._log_dashboard_metrics(page)
        except Exception:
            logger.debug("dashboard preview redraw skipped", exc_info=True)

    def _log_dashboard_metrics(self, page=None):
        """Record the browser's effective scale/layout after a page redraw."""
        if page is None:
            view = getattr(self, "view", None)
            page = view.page() if view is not None else None
        runner = getattr(page, "runJavaScript", None)
        if runner is None:
            return
        script = (
            "(() => { const a=document.getElementById('app'); "
            "const c=document.getElementById('preview-canvas'); "
            "const r=a?a.getBoundingClientRect():null; "
            "const cr=c?c.getBoundingClientRect():null; "
            "return JSON.stringify({dpr:window.devicePixelRatio,"
            "zoom:visualViewport&&visualViewport.scale,"
            "inner:[innerWidth,innerHeight],"
            "app:r&&[r.x,r.y,r.width,r.height],"
            "canvas:cr&&[cr.x,cr.y,cr.width,cr.height],"
            "fonts:document.fonts&&document.fonts.status}); })()"
        )
        try:

            def report(value):
                logger.info("dashboard metrics=%s", value)
                try:
                    metrics = json.loads(value)
                    browser_dpr = float(metrics.get("dpr", 0.0))
                    view = getattr(self, "view", None)
                    qt_dpr = float(view.devicePixelRatioF()) if view is not None else 0.0
                    if qt_dpr > 0 and abs(browser_dpr - qt_dpr) > 0.05:
                        logger.error(
                            "dashboard DPI mismatch: qt_dpr=%.3f browser_dpr=%.3f metrics=%s",
                            qt_dpr,
                            browser_dpr,
                            value,
                        )
                except TypeError, ValueError, AttributeError, RuntimeError:
                    return

            runner(script, report)
        except TypeError, RuntimeError:
            # Test doubles and older bindings may expose only the one-argument
            # overload; diagnostics must never affect rendering.
            return

    def _on_dashboard_screen_changed(self, _screen=None):
        """Re-synchronize Chromium after the top-level window crosses displays."""
        self._schedule_screen_reconcile()
        if self._closing_for_exit or getattr(self, "_dashboard_frozen", False):
            return
        QTimer.singleShot(120, self._refresh_dashboard_viewport)

    def _refresh_dashboard_viewport(self):
        """Force a layout/paint pass after a DPI or compositor transition.

        WebEngine keeps the DOM alive across a monitor switch, but its viewport
        can briefly retain the old device scale.  A resize event is not
        guaranteed when only the screen DPI changes, so explicitly request a
        browser resize/reflow and redraw the preview once the new surface is
        active.  No CSS zoom is applied here.
        """
        if self._closing_for_exit or getattr(self, "_dashboard_frozen", False):
            return
        view = getattr(self, "view", None)
        if view is None:
            return
        try:
            # Re-assert the neutral page zoom after Chromium recreates its
            # renderer for a new monitor.  This forces a fresh layout scale
            # without introducing a CSS transform or changing any controls.
            view.setZoomFactor(1.0)
            view.updateGeometry()
            view.update()
            window_handle = view.windowHandle()
            screen = window_handle.screen() if window_handle is not None else None
            if screen is None:
                screen = self.screen()
            logger.debug(
                "dashboard Qt viewport: view=%sx%s dpr=%.3f zoom=%.3f screen=%s screen_dpr=%.3f",
                view.width(),
                view.height(),
                float(view.devicePixelRatioF()),
                float(view.zoomFactor()),
                screen.name() if screen is not None else None,
                float(screen.devicePixelRatio()) if screen is not None else 0.0,
            )
            page = view.page()
            runner = getattr(page, "runJavaScript", None)
            if runner is not None:
                runner(
                    "void document.documentElement?.offsetWidth;"
                    "window.dispatchEvent(new Event('resize'));"
                    "if (typeof redrawPreview === 'function') "
                    "window.requestAnimationFrame(drawPreview);"
                )
                # The local Google Sans font can finish after loadFinished.
                # Redraw once it is ready so the final glyph metrics, grid
                # widths and canvas position are calculated from the same font
                # used for the visible page instead of the temporary fallback.
                runner(
                    "if (document.fonts && document.fonts.ready) "
                    "document.fonts.ready.then(() => {"
                    "window.dispatchEvent(new Event('resize'));"
                    "if (typeof drawPreview === 'function') drawPreview();"
                    "});"
                )
                if _debug_logging:
                    runner(
                        "JSON.stringify((() => {"
                        "const c=document.getElementById('preview-canvas');"
                        "const a=document.getElementById('app');"
                        "const cr=c?.getBoundingClientRect();"
                        "const ar=a?.getBoundingClientRect();"
                        "const cs=a?getComputedStyle(a):null;"
                        "return {dpr:window.devicePixelRatio,"
                        "visualScale:window.visualViewport?.scale||1,"
                        "innerWidth:window.innerWidth,innerHeight:window.innerHeight,"
                        "clientWidth:document.documentElement.clientWidth,"
                        "clientHeight:document.documentElement.clientHeight,"
                        "appRect:ar?{x:ar.x,y:ar.y,w:ar.width,h:ar.height}:null,"
                        "canvasRect:cr?{x:cr.x,y:cr.y,w:cr.width,h:cr.height}:null,"
                        "canvasBitmap:c?{w:c.width,h:c.height}:null,"
                        "appFont:cs?cs.fontFamily:null};})())",
                        lambda value: logger.debug("dashboard viewport=%s", value),
                    )
        except Exception:
            logger.debug("dashboard viewport refresh skipped", exc_info=True)

    def _connect_dashboard_screen_signal(self):
        """Attach to the native window once Qt has created its QWindow handle."""
        handle = self.windowHandle()
        if handle is None or handle is self._dashboard_window_handle:
            return
        try:
            handle.screenChanged.connect(self._on_dashboard_screen_changed)
            self._dashboard_window_handle = handle
        except AttributeError, TypeError:
            logger.debug("dashboard screenChanged signal unavailable", exc_info=True)

    def showEvent(self, event):
        super().showEvent(event)
        self._connect_dashboard_screen_signal()
        # The first WebEngine surface can be allocated before the top-level
        # window has acquired its final screen/DPI.  Rebind it once after the
        # native window is visible; this is a no-op while the page is frozen.
        QTimer.singleShot(150, self._refresh_dashboard_viewport)
        try:
            screen = self.screen()
            logger.info(
                "dashboard display ready: qt_size=%sx%s qt_dpr=%.3f screen=%s screen_dpr=%.3f",
                self.width(),
                self.height(),
                float(self.devicePixelRatioF()),
                screen.name() if screen else None,
                float(screen.devicePixelRatio()) if screen else 0.0,
            )
        except AttributeError, RuntimeError, TypeError, ValueError:
            logger.debug("dashboard display metrics unavailable", exc_info=True)
        if _debug_logging:
            try:
                screen = self.screen()
                logger.debug(
                    "dashboard Qt geometry=%s size=%s dpr=%s screen=%s screen_geo=%s",
                    self.geometry().getRect(),
                    (self.width(), self.height()),
                    self.devicePixelRatioF(),
                    screen.name() if screen else None,
                    screen.geometry().getRect() if screen else None,
                )
            except AttributeError, RuntimeError, TypeError:
                logger.debug("dashboard Qt metrics unavailable", exc_info=True)

    def changeEvent(self, event):
        """Freeze WebEngine after minimize and thaw it on restore."""
        super().changeEvent(event)
        if event.type() != QEvent.Type.WindowStateChange:
            return
        if self.isMinimized():
            self._schedule_dashboard_freeze()
        elif self.isVisible():
            self._set_dashboard_frozen(False)

    def hideEvent(self, event):
        """Also cover explicit tray/close-to-tray hides (no state change event)."""
        super().hideEvent(event)
        if not self._closing_for_exit and hasattr(self, "_dashboard_freeze_timer"):
            self._schedule_dashboard_freeze()

    def _on_single_instance_connection(self):
        while self._single_instance_server.hasPendingConnections():
            socket = self._single_instance_server.nextPendingConnection()
            if socket is None:
                continue
            socket.waitForReadyRead(200)
            socket.readAll()
            socket.disconnectFromServer()
            self._restore_from_tray()

    @pyqtSlot(QSystemTrayIcon.ActivationReason)
    def _on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self._restore_from_tray()

    def _exit_from_tray(self):
        logger.info(
            "tray exit requested state=%s thread_running=%s",
            self.radar_operation_state,
            self.audio_thread is not None and self.audio_thread.isRunning(),
        )
        self._closing_for_exit = True
        self.close()

    # ── Audio Callbacks ───────────────────────────────────────────────
    def on_audio_data(self, angle: float, intensity: float, generation=None):
        if self.invert_direction:
            angle = -angle
        self.overlay.update_audio_data(angle, intensity, generation)

    def on_device_info(self, name: str, channels: int):
        label = f"{name}  ({channels}ch)"
        self.bridge.deviceChanged.emit(label)

    def on_capture_status(self, message: str):
        """Non-terminal capture status intended for the UI."""
        self.bridge.statusChanged.emit(message, self.radar_active)

    def _capture_sender_generation(self):
        sender = self.sender()
        if sender is not self.audio_thread:
            return None
        return getattr(sender, "_capture_generation", None)

    def _consume_latest_audio(self):
        """Consume at most one current-generation sample per GUI timer tick."""
        thread = self.audio_thread
        if thread is None:
            return
        generation = getattr(thread, "_capture_generation", None)
        if generation != self._active_capture_generation:
            thread.clear_latest_audio()
            return
        if self.radar_operation_state not in ("starting", "running"):
            thread.clear_latest_audio()
            return
        if self._closing_for_exit or not self.overlay.isVisible():
            thread.clear_latest_audio()
            return
        sample = thread.take_latest_audio()
        if sample is not None:
            self.on_audio_data(*sample, generation=generation)

    def _stop_audio_consumption(self):
        self._audio_consume_timer.stop()
        thread = self.audio_thread
        if thread is not None:
            if _debug_logging:
                logger.debug(
                    "audio delivery performance metrics=%s",
                    thread.performance_metrics(),
                )
            thread.clear_latest_audio()

    def _on_capture_status(self, message):
        if self._capture_sender_generation() != self._active_capture_generation:
            return
        if self.radar_operation_state in ("restarting", "stopping", "closing", "idle"):
            return
        self.on_capture_status(message)

    def _on_capture_device(self, name, channels):
        if self._capture_sender_generation() == self._active_capture_generation:
            self.on_device_info(name, channels)

    def _on_capture_error(self, message):
        logger.error(
            "capture error generation=%s state=%s message=%r",
            self._capture_sender_generation(),
            self.radar_operation_state,
            message,
        )
        if self._capture_sender_generation() != self._active_capture_generation:
            logger.debug("capture error ignored: stale sender")
            return
        if self.radar_operation_state == "closing":
            return
        if self._capture_terminal_error is None:
            self._capture_terminal_error = message
            self.bridge.statusChanged.emit(message, False)
        self.radar_active = False
        self.overlay.hide()
        self._stop_audio_consumption()
        if self.radar_operation_state in ("starting", "running"):
            self._set_operation_state("restarting" if self._restart_requested else "stopping")
            self.audio_thread.request_stop()

    def _new_audio_thread(self):
        """Create an idle thread and bind every callback to its generation."""
        self._capture_generation += 1
        generation = self._capture_generation
        thread = AudioCaptureThread()
        thread._capture_generation = generation
        self.audio_thread = thread
        self._active_capture_generation = generation
        if hasattr(self, "overlay"):
            self.overlay.set_capture_generation(generation)
        thread.device_info_signal.connect(self._on_capture_device)
        thread.status_signal.connect(self._on_capture_status)
        thread.error_signal.connect(self._on_capture_error)
        thread.ready_signal.connect(self._on_capture_ready)
        thread.finished.connect(self._on_capture_finished)
        logger.debug("new capture thread generation=%s", generation)

    def on_overlay_position_changed(self, x: int, y: int):
        x, y = self._clamp_overlay_position(x, y)
        if (x, y) != (int(self.overlay.pos().x()), int(self.overlay.pos().y())):
            self.overlay.move(x, y)
        logger.info("overlay position persisted x=%s y=%s", x, y)
        self._store_overlay_position(x, y)
        self._save_settings()
        self.emit_overlay_position()
        self._follow_overlay_owner(x, y)

    # Each monitor owns its own remembered placement, so a spot arranged on one
    # screen is not clobbered when the overlay is moved or reset on another.
    # settings["overlay_positions"] maps QScreen.name() -> {"x", "y"} in
    # absolute virtual-desktop coordinates. The slot written is the screen the
    # overlay centre actually sits on, not the dashboard selection, so dragging
    # the overlay across a monitor boundary remembers it for the right screen.
    def _overlay_positions_map(self):
        """settings["overlay_positions"] (per-screen slots), initialised to an
        empty map when a fresh settings file has no such key yet."""
        mapping = self.settings.get("overlay_positions")
        if not isinstance(mapping, dict):
            mapping = {}
            self.settings["overlay_positions"] = mapping
        return mapping

    def _screen_owner_name(self, x: int, y: int) -> str:
        """Screen whose slot owns an overlay placed at (x, y): the screen under
        the overlay centre. Falls back to the dashboard selection so a write
        always lands somewhere (the centre is normally clamped onto a screen)."""
        screens = QApplication.screens()
        if screens:
            owner = _find_screen_at_centre(screens, x + self.overlay.width() / 2, y + self.overlay.height() / 2)
            if owner is not None:
                return owner.name()
            if 0 <= self.selected_monitor < len(screens):
                return screens[self.selected_monitor].name()
        return ""

    def _store_overlay_position(self, x: int, y: int):
        mapping = self._overlay_positions_map()
        mapping[self._screen_owner_name(x, y)] = {"x": x, "y": y}

    def _saved_overlay_position_for_screen(self, name):
        """(x, y) remembered for one screen name, or None without a slot."""
        if not name:
            return None
        entry = self._overlay_positions_map().get(name)
        if not isinstance(entry, dict):
            return None
        try:
            return int(entry["x"]), int(entry["y"])
        except KeyError, TypeError, ValueError, OverflowError:
            return None

    def on_overlay_position_preview(self, x: int, y: int):
        """Live drag frames: refresh the UI readout only, no disk write.
        The monitor selection follows the overlay across a screen boundary
        while the drag is in flight. The final position is persisted once on
        mouse release via on_overlay_position_changed (issue #3)."""
        x, y = int(x), int(y)
        state = {
            "x": x,
            "y": y,
            "drag_enabled": bool(self.overlay.drag_enabled),
        }
        self.bridge.overlayPositionChanged.emit(json.dumps(state))
        self._follow_overlay_owner(x, y)

    def emit_overlay_position(self):
        pos = self.overlay.pos()
        state = {
            "x": int(pos.x()),
            "y": int(pos.y()),
            "drag_enabled": bool(self.overlay.drag_enabled),
        }
        self.bridge.overlayPositionChanged.emit(json.dumps(state))

    def _follow_overlay_owner(self, x: int, y: int):
        """Retarget the monitor selection onto the screen the overlay centre
        sits on, so dragging across an extended-desktop boundary moves the
        dashboard dropdown (and CENTER) with the overlay instead of leaving
        the old monitor selected. No-op while the centre is off every screen
        or the owning screen cannot be matched back to an index."""
        screens = QApplication.screens()
        if not screens:
            return
        owner = _find_screen_at_centre(screens, x + self.overlay.width() / 2, y + self.overlay.height() / 2)
        if owner is None or owner.name() == self._selected_monitor_name:
            return
        matching = next((i for i, screen in enumerate(screens) if screen.name() == owner.name()), None)
        if matching is None:
            return
        self.selected_monitor = matching
        self._selected_monitor_name = owner.name()
        self.bridge.selectedMonitorChanged.emit(int(self.selected_monitor))

    # ── Programs (per-app capture) ────────────────────────────────────
    def set_program(self, program):
        """Change the capture target. Applies live when the radar is running."""
        if program == self.selected_program:
            logger.debug("program unchanged name=%s", program)
            return
        logger.info("program changed from=%s to=%s", self.selected_program, program)
        self.selected_program = program
        self._restart_capture_if_active()

    def list_programs(self):
        """Running programs with an audio session (legacy synchronous helper)."""
        try:
            from process_loopback import list_audio_programs

            return list_audio_programs()
        except Exception:
            logger.exception("Program enumeration failed")
            return []

    def emit_monitors(self):
        """Emit the current screen list for the dashboard monitor selector."""
        if getattr(self, "_dashboard_frozen", False):
            logger.debug("monitor list refresh skipped: dashboard frozen")
            return
        screens = QApplication.screens()
        if screens and not self._selected_monitor_name:
            idx = max(0, min(int(self.selected_monitor), len(screens) - 1))
            self.selected_monitor = idx
            self._selected_monitor_name = screens[idx].name()
        monitors = [
            {
                "idx": i,
                "name": s.name(),
                "resolution": f"{s.geometry().width()}x{s.geometry().height()}",
            }
            for i, s in enumerate(screens)
        ]
        self.bridge.monitorsChanged.emit(json.dumps(monitors))

    def set_monitor(self, idx):
        """Select a monitor and move a visible overlay to its remembered spot."""
        screens = QApplication.screens()
        try:
            idx = int(idx)
        except TypeError, ValueError, OverflowError:
            return
        if not screens or not 0 <= idx < len(screens):
            logger.warning("invalid monitor index ignored index=%r", idx)
            return
        self.selected_monitor = idx
        self._selected_monitor_name = screens[idx].name()
        if self.overlay.isVisible():
            # Each monitor has its own slot: restore the spot arranged for it
            # the last time, and centre on first visit. Both cases persist, so
            # switching back and forth no longer clobbers the other screen.
            screen = screens[idx]
            saved = self._saved_overlay_position_for_screen(screen.name())
            if saved is None:
                x, y = self._selected_monitor_center_position()
            else:
                # A spot saved under this name may predate a topology change;
                # pin it to the chosen monitor rather than letting the clamp
                # follow a stale centre onto some other screen.
                x, y = self._clamp_overlay_position(saved[0], saved[1], screen)
            self.overlay.move(x, y)
            self.on_overlay_position_changed(x, y)
        self.bridge.selectedMonitorChanged.emit(idx)

    def emit_programs(self):
        if getattr(self, "_dashboard_frozen", False):
            logger.debug("program list refresh skipped: dashboard frozen")
            return
        if self._program_list_thread is not None:
            # Keep ownership until its queued finished callback releases it.
            # isRunning() can already be false before that callback is delivered.
            logger.debug("program list refresh skipped: previous request pending")
            return
        logger.debug("program list refresh started")
        self._program_list_thread = ProgramListThread(self)
        self._program_list_thread.result.connect(self.bridge.programsChanged)
        self._program_list_thread.finished.connect(self._on_program_list_finished)
        self._program_list_thread.start()

    def _on_program_list_finished(self):
        sender = self.sender()
        thread = self._program_list_thread
        if sender is not None and sender is not thread:
            logger.debug("stale program list finished callback ignored")
            sender.deleteLater()
            return
        self._program_list_thread = None
        if thread is not None:
            thread.deleteLater()
        logger.debug("program list refresh finished")
        self._maybe_finalize_exit()

    def _stop_program_list_thread(self):
        thread = self._program_list_thread
        if thread is None:
            return
        logger.debug("stopping program list thread running=%s", thread.isRunning())
        if thread.isRunning():
            thread.requestInterruption()
            logger.debug("program list thread cancellation requested")
        else:
            self._program_list_thread = None
            thread.deleteLater()

    # ── Overlay appearance (accent colour + stroke) ───────────────────
    def set_accent_color(self, hex_color: str):
        normalized = _normalize_hex_color(hex_color, fallback=None)
        if normalized is None:
            logger.warning("invalid accent color ignored value=%r", hex_color)
            return
        logger.info("accent color changed value=%s", normalized)
        self.accent_color = normalized
        self.overlay.set_accent_color(self.accent_color)
        self.settings["accent_color"] = self.accent_color
        self._queue_settings_save()

    def set_stroke_width(self, width: int):
        normalized = _bounded_number(width, int, 1, 20)
        if normalized is None:
            logger.warning("invalid stroke width ignored value=%r", width)
            return
        logger.debug("stroke width changed value=%s", normalized)
        self.stroke_width = normalized
        self.overlay.set_stroke_width(self.stroke_width)
        self.settings["stroke_width"] = self.stroke_width

    def commit_appearance(self):
        logger.info(
            "appearance settings committed color=%s thickness=%s",
            self.accent_color,
            self.stroke_width,
        )
        self._queue_settings_save()

    def emit_appearance(self):
        """Push the saved accent colour + thickness to the dashboard on load so the
        picker/slider/preview match the overlay (and what was saved last session)."""
        self.bridge.appearanceChanged.emit(
            json.dumps(
                {
                    "color": self.accent_color,
                    "thickness": self.stroke_width,
                }
            )
        )

    # ── Mono output (single-sided listeners) ──────────────────────────
    def set_mono_enabled(self, enabled: bool):
        enabled = bool(enabled)
        changed = enabled != self.mono_enabled
        logger.info("mono output enabled changed from=%s to=%s", self.mono_enabled, enabled)
        self.mono_enabled = enabled
        self.settings["mono_enabled"] = self.mono_enabled
        self._queue_settings_save()
        self.emit_mono_state()
        if changed:
            self._restart_capture_if_active()

    def set_mono_output(self, device: str):
        device = device or None
        changed = device != self.mono_device
        logger.info("mono output device changed from=%s to=%s", self.mono_device, device)
        self.mono_device = device
        self.settings["mono_device"] = self.mono_device
        self._queue_settings_save()
        self.emit_mono_state()
        if changed:
            self._restart_capture_if_active()

    def emit_mono_state(self):
        """Push the playback-device list + VB-CABLE detection + current selection
        to the UI so it can render the mono setup card."""
        if self._mono_device_thread is not None:
            # Do not replace a finished thread before its queued callback clears
            # the reference; otherwise shutdown can lose the new worker.
            self._mono_refresh_pending = True
            return
        self._mono_refresh_pending = False
        thread = MonoDeviceListThread(self)
        self._mono_device_thread = thread
        thread.result.connect(self._on_mono_devices)
        thread.finished.connect(self._on_mono_device_thread_finished)
        thread.start()

    def _on_mono_devices(self, payload):
        if self._closing_for_exit:
            return
        state = json.loads(payload)
        state.update({"enabled": self.mono_enabled, "selected": self.mono_device})
        self.bridge.monoStateChanged.emit(json.dumps(state))

    def _on_mono_device_thread_finished(self):
        sender = self.sender()
        thread = self._mono_device_thread
        if sender is not None and sender is not thread:
            logger.debug("stale mono device finished callback ignored")
            sender.deleteLater()
            return
        self._mono_device_thread = None
        if thread is not None:
            thread.deleteLater()
        if self._mono_refresh_pending and not self._closing_for_exit:
            self.emit_mono_state()
            return
        self._mono_refresh_pending = False
        self._maybe_finalize_exit()

    def _stop_mono_device_thread(self):
        thread = self._mono_device_thread
        self._mono_refresh_pending = False
        if thread is not None and thread.isRunning():
            thread.requestInterruption()

    def install_vbcable(self):
        """Launch the bundled VB-CABLE installer with a UAC prompt. If the build
        doesn't bundle it, open the official download page instead. The installer
        shows its own UI on purpose (donationware terms + trust for the
        anti-cheat-wary audience)."""
        installer = os.path.join(RESOURCE_DIR, "vendor", "VBCABLE", "VBCABLE_Setup_x64.exe")
        if os.path.exists(installer):
            try:
                import ctypes
                import shutil
                import tempfile

                # Copy out of a temporary onefile bundle first so the installer
                # remains available after the app exits.
                tmp = os.path.join(tempfile.gettempdir(), "VBCABLE_Setup_x64.exe")
                shutil.copyfile(installer, tmp)
                ctypes.windll.shell32.ShellExecuteW(None, "runas", tmp, None, None, 1)
                return
            except Exception:
                logger.exception("VB-CABLE launch failed")
        import webbrowser

        webbrowser.open("https://vb-audio.com/Cable/")

    def _resolve_target(self):
        """Map the selected program name to a live PID. Returns (pid, name) or
        (None, None) for whole-system capture / if the program is gone."""
        if not self.selected_program:
            return None, None
        try:
            from process_loopback import resolve_pid

            pid = resolve_pid(self.selected_program)
        except Exception as exc:  # noqa: BLE001 - target process may disappear.
            logger.warning(
                "program target resolution failed name=%s: %s",
                self.selected_program,
                exc,
            )
            pid = None
        if pid is None:
            return None, None
        return pid, self.selected_program

    # ── Audio parameters (source of truth, survive thread recreation) ──
    def _apply_audio_settings_to_thread(self):
        """Push the current audio parameters onto the (possibly freshly created)
        capture thread. Called on every start so a recreated thread doesn't come
        up with library defaults instead of the user's preset/sliders."""
        s = self.audio_settings
        self.audio_thread.set_sensitivity(s["sensitivity"])
        self.audio_thread.set_gain(s["gain"])
        self.audio_thread.set_freq_range(s["freq_low"], s["freq_high"])
        self.audio_thread.set_max_amplitude(s["max_amp"])

    def _load_audio_settings(self):
        """Overlay the saved audio parameters onto the defaults. settings.json is
        hand-editable, so every value is coerced individually and skipped when it
        is not a usable number - a corrupt file costs you your tuning, never a
        crash on launch."""
        saved = self.settings.get("audio_settings")
        normalized = _normalize_audio_settings(saved, self.audio_settings)
        if normalized != saved:
            logger.warning("invalid or incomplete saved audio settings normalized")
        self.audio_settings = normalized

    def _queue_settings_save(self):
        """Stage live settings and debounce the disk write."""
        self.settings["audio_settings"] = dict(self.audio_settings)
        self._settings_save_timer.start()

    def _write_pending_saves(self):
        """The debounced write itself: settings.json only."""
        self._save_settings()

    def _flush_pending_saves(self):
        """Write a pending change now instead of waiting the debounce out. Used on
        close and before switching profiles - a slider moved in the last 600ms
        belongs to the profile it was moved in, not the one being switched to."""
        if self._settings_save_timer.isActive():
            self._settings_save_timer.stop()
            self._write_pending_saves()

    def emit_audio_settings(self):
        """Push the live audio parameters to the dashboard so the sliders show what
        the capture thread is actually using."""
        self.bridge.audioSettingsChanged.emit(json.dumps(self.audio_settings))

    def set_audio_param(self, key: str, value):
        """Update one live audio parameter and apply it immediately.

        Persistence is committed separately when the editing gesture settles.
        """
        self.set_audio_params({key: value})

    def set_audio_params(self, updates):
        """Validate and apply one atomic group of runtime audio parameters."""
        if not isinstance(updates, dict) or any(key not in AUDIO_PARAM_LIMITS for key in updates):
            logger.warning("invalid audio parameter update ignored keys=%r", updates)
            return False
        normalized = dict(self.audio_settings)
        for key in updates:
            cast, minimum, maximum = AUDIO_PARAM_LIMITS[key]
            value = _bounded_number(updates[key], cast, minimum, maximum)
            if value is None:
                logger.warning("invalid audio parameter ignored key=%s value=%r", key, updates[key])
                return False
            normalized[key] = value
        if normalized["freq_low"] > normalized["freq_high"]:
            logger.warning(
                "invalid frequency range ignored low=%s high=%s",
                normalized["freq_low"],
                normalized["freq_high"],
            )
            return False
        previous = {key: self.audio_settings[key] for key in updates}
        if all(normalized[key] == previous[key] for key in updates):
            return False
        self.audio_settings.update({key: normalized[key] for key in updates})
        logger.debug(
            "audio parameters changed keys=%s from=%s to=%s",
            tuple(updates),
            previous,
            {key: self.audio_settings[key] for key in updates},
        )
        self._apply_audio_settings_to_thread()
        return True

    def commit_audio_settings(self):
        logger.info("audio settings committed values=%s", self.audio_settings)
        self._queue_settings_save()

    def set_selected_preset(self, name: str):
        """Remember which dropdown entry (built-in preset or saved profile) is
        selected. Written straight to settings.json: this only fires when the
        user picks an entry, not on slider drags, so it is not a hot path. The
        no-op guard matters anyway - the dashboard replays the restored name
        through the same path a click takes, which would otherwise rewrite the
        file with identical contents on every launch."""
        name = name or "builtin:all-sounds"
        if name == self.selected_preset:
            logger.debug("preset selection unchanged id=%s", name)
            return
        logger.info("preset selected from=%s to=%s", self.selected_preset, name)
        self._flush_pending_saves()
        self.selected_preset = name
        self.preset_state = {"id": name, "dirty": False}
        self.settings["selected_preset"] = name
        self.settings["preset_state"] = dict(self.preset_state)
        self._save_settings()

    def emit_selected_preset(self):
        self.bridge.selectedPresetChanged.emit(
            json.dumps(
                {
                    "id": self.preset_state.get("id", self.selected_preset),
                    "dirty": bool(self.preset_state.get("dirty", False)),
                }
            )
        )

    def apply_preset(self, name: str):
        """Apply a built-in preset and echo the values back to the dashboard so the
        sliders and readouts actually move (otherwise switching presets looks like
        it does nothing).

        Every parameter is overwritten, never just the ones the preset cares about
        - see the note on SOUND_PRESETS for why a partial apply leaks."""
        if name != "builtin:all-sounds":
            logger.warning("unknown preset apply ignored id=%s", name)
            return
        logger.info("preset applied id=%s", name)
        p = SOUND_PRESETS["All Sounds"]
        for key in self.audio_settings:
            self.audio_settings[key] = p[key]
        self._apply_audio_settings_to_thread()
        self._queue_settings_save()
        self.emit_audio_settings()

    # ── Radar Control ─────────────────────────────────────────────────
    def _start_capture_thread(self):
        """Configure the idle thread (target/mono/params are read at thread start)
        and start it. Shared by Start and by mid-session capture restarts."""
        if self._closing_for_exit or self.audio_thread is None:
            logger.debug("capture start skipped: closing or no thread")
            return False
        if self.audio_thread.isRunning():
            logger.debug(
                "capture start skipped: thread already running generation=%s",
                self._active_capture_generation,
            )
            return False
        # Resolve the capture target fresh (PIDs change between launches).
        pid, name = self._resolve_target()
        self.audio_thread.set_target(pid, name)
        self._capture_label = name if pid is not None else (self.selected_program or "system audio")
        self.audio_thread.set_mono(self.mono_enabled, self.mono_device)
        self._apply_audio_settings_to_thread()
        self.audio_thread.clear_latest_audio()

        if not self.audio_thread.isRunning():
            logger.info(
                "starting capture thread generation=%s target_pid=%s target_name=%r",
                self._active_capture_generation,
                pid,
                name,
            )
            self.audio_thread.start()
            self._audio_consume_timer.start()

        # The UI remains in a loading state until the worker emits ready_signal.
        self.bridge.statusChanged.emit("Starting audio capture...", False)
        return True

    def _set_operation_state(self, state):
        previous = self.radar_operation_state
        self.radar_operation_state = state
        logger.info("operation state %s -> %s", previous, state)
        self.bridge.operationBusyChanged.emit(state in ("starting", "restarting", "stopping", "closing"))
        self.bridge.operationStateChanged.emit(state)

    def _on_capture_ready(self):
        logger.debug(
            "capture ready generation=%s active_generation=%s state=%s",
            self._capture_sender_generation(),
            self._active_capture_generation,
            self.radar_operation_state,
        )
        if self._capture_sender_generation() != self._active_capture_generation:
            logger.debug("capture ready ignored: stale sender")
            return
        if self.radar_operation_state != "starting":
            return
        self._capture_watchdog.stop()
        self._watchdog_generation = None
        self._capture_terminal_error = None
        self.radar_active = True
        self._set_operation_state("running")
        if self.selected_program and self._capture_label != "system audio":
            self.bridge.statusChanged.emit(f"Radar active - capturing {self._capture_label}", True)
        else:
            self.bridge.statusChanged.emit("Radar is active", True)

    def _on_capture_timeout(self):
        if self.radar_operation_state != "starting":
            return
        if self._watchdog_generation != self._active_capture_generation:
            return
        label = getattr(self, "_capture_label", None) or self.selected_program or "system audio"
        self._capture_terminal_error = f"Capture of {label} stopped unexpectedly - restart the radar"
        logger.error("%s", self._capture_terminal_error)
        self.radar_active = False
        self.overlay.hide()
        self._stop_audio_consumption()
        self._set_operation_state("stopping")
        if self.audio_thread is not None:
            self.audio_thread.request_stop()

    def _on_capture_finished(self):
        sender = self.sender()
        sender_generation = getattr(sender, "_capture_generation", None)
        logger.debug(
            "capture finished signal sender_generation=%s active_generation=%s state=%s",
            sender_generation,
            self._active_capture_generation,
            self.radar_operation_state,
        )
        if self._capture_sender_generation() != self._active_capture_generation:
            logger.debug("capture finished ignored: stale sender")
            return
        self._capture_watchdog.stop()
        self._watchdog_generation = None
        self._stop_audio_consumption()
        self.radar_active = False
        self.overlay.hide()
        state = self.radar_operation_state
        if state == "closing":
            self._active_capture_generation = None
            logger.info("capture finished during closing; finalizing exit")
            self._finalize_exit()
            return

        restart = self._restart_requested and state == "restarting"
        terminal_error = self._capture_terminal_error
        self._active_capture_generation = None
        self._new_audio_thread()
        if restart:
            self._restart_requested = False
            self._capture_terminal_error = None
            self._begin_capture_start(place_overlay=False)
            return

        self._restart_requested = False
        self._set_operation_state("idle")
        message = terminal_error or (
            "Capture stopped unexpectedly - restart the radar" if state in ("starting", "running") else "Radar stopped"
        )
        self._capture_terminal_error = None
        self.bridge.statusChanged.emit(message, False)
        self.emit_overlay_position()

    def _begin_capture_start(self, place_overlay):
        if self._closing_for_exit:
            logger.debug("start request ignored: application is closing")
            return
        if self.radar_operation_state not in ("idle", "restarting"):
            logger.debug("start request ignored: state=%s", self.radar_operation_state)
            return
        logger.info("radar start requested place_overlay=%s", place_overlay)
        self._set_operation_state("starting")
        self._capture_terminal_error = None
        self.radar_active = False
        # Radar runtime must always be input-transparent.  The dashboard can
        # leave drag mode enabled while positioning the overlay; do not carry
        # that interactive state into the game session.
        self.overlay.set_drag_enabled(False)
        self.overlay.show()
        if place_overlay:
            self._place_overlay_for_start()
        if self._start_capture_thread():
            self._watchdog_generation = self._active_capture_generation
            self._capture_watchdog.start()
            return
        self._capture_terminal_error = "Unable to start audio capture"
        self._stop_audio_consumption()
        self.overlay.hide()
        self._set_operation_state("idle")
        self.bridge.statusChanged.emit(self._capture_terminal_error, False)
        logger.error("radar start failed: unable to start capture thread")
        self._capture_terminal_error = None

    def start_radar(self):
        logger.info("start_radar invoked")
        self._begin_capture_start(place_overlay=True)

    def _restart_capture_if_active(self):
        """Queue a restart so the latest runtime target is applied atomically."""
        if self._closing_for_exit:
            logger.debug("capture restart ignored: application is closing")
            return
        state = self.radar_operation_state
        if state == "restarting":
            logger.debug("capture restart coalesced: restart already pending")
            self._restart_requested = True
            return
        if state not in ("starting", "running"):
            logger.debug("capture restart ignored: state=%s", state)
            return
        logger.info("capture restart requested state=%s", state)
        self._restart_requested = True
        self.radar_active = False
        self.overlay.hide()
        self._stop_audio_consumption()
        self._set_operation_state("restarting")
        self._capture_watchdog.stop()
        self._watchdog_generation = None
        if self.audio_thread is not None:
            self.audio_thread.request_stop()

    def stop_radar(self):
        if self._closing_for_exit:
            logger.debug("stop request ignored: application is closing")
            return
        if self.radar_operation_state not in ("starting", "running", "restarting"):
            logger.debug("stop request ignored: state=%s", self.radar_operation_state)
            return
        logger.info("radar stop requested state=%s", self.radar_operation_state)
        self._restart_requested = False
        self.radar_active = False
        self.overlay.set_drag_enabled(False)
        self.overlay.hide()
        self._stop_audio_consumption()
        self._set_operation_state("stopping")
        self._capture_watchdog.stop()
        self._watchdog_generation = None
        if self.audio_thread is not None:
            self.audio_thread.request_stop()

    def _selected_monitor_center_position(self):
        screens = QApplication.screens()
        if not screens:
            return 0, 0
        idx = self.selected_monitor if 0 <= self.selected_monitor < len(screens) else 0
        # Center on the full screen, not the work area: an overlay centered in
        # the available geometry sits 24px high on a 1080p-class monitor with a
        # 48px taskbar (half the taskbar height), i.e. above the true center.
        geo = screens[idx].geometry()
        return (
            geo.x() + (geo.width() - self.overlay.width()) // 2,
            geo.y() + (geo.height() - self.overlay.height()) // 2,
        )

    def _clamp_overlay_position(self, x, y, screen=None):
        """Clamp the full overlay rectangle inside the active work area."""
        try:
            x, y = int(x), int(y)
        except TypeError, ValueError, OverflowError:
            return self._selected_monitor_center_position()
        screens = QApplication.screens()
        if not screens:
            return x, y
        if screen is None:
            cx = x + self.overlay.width() / 2
            cy = y + self.overlay.height() / 2
            screen = _find_screen_at_centre(screens, cx, cy)
        if screen is None:
            screen = screens[self.selected_monitor] if 0 <= self.selected_monitor < len(screens) else screens[0]
        geo = screen.availableGeometry()
        max_x = max(geo.x(), geo.right() - self.overlay.width() + 1)
        max_y = max(geo.y(), geo.bottom() - self.overlay.height() + 1)
        return max(geo.x(), min(x, max_x)), max(geo.y(), min(y, max_y))

    def _watch_screen_geometry(self, screen):
        key = id(screen)
        if key in self._screen_watch_ids:
            return
        self._screen_watch_ids.add(key)
        for signal_name in (
            "geometryChanged",
            "availableGeometryChanged",
            "logicalDotsPerInchChanged",
            "nameChanged",
        ):
            signal = getattr(screen, signal_name, None)
            if signal is not None:
                try:
                    signal.connect(self._on_screen_geometry_changed)
                except AttributeError, TypeError:
                    logger.debug("unable to watch QScreen.%s", signal_name, exc_info=True)

    def _on_screen_added(self, screen):
        self._watch_screen_geometry(screen)
        self._schedule_screen_reconcile()

    def _on_primary_screen_changed(self, screen):
        self._watch_screen_geometry(screen)
        self._schedule_screen_reconcile()

    def _on_screen_removed(self, screen):
        self._screen_watch_ids.discard(id(screen))
        self._schedule_screen_reconcile()

    def _on_screen_geometry_changed(self, *_args):
        self._schedule_screen_reconcile()

    def _schedule_screen_reconcile(self):
        if not self._closing_for_exit:
            self._screen_change_timer.start()

    def _reconcile_screen_layout(self):
        if self._closing_for_exit:
            return
        screens = QApplication.screens()
        if not screens:
            return
        if self._selected_monitor_name:
            matching = next(
                (i for i, screen in enumerate(screens) if screen.name() == self._selected_monitor_name),
                None,
            )
            if matching is not None:
                self.selected_monitor = matching
        self.selected_monitor = max(0, min(int(self.selected_monitor), len(screens) - 1))
        self._selected_monitor_name = screens[self.selected_monitor].name()
        current = self.overlay.pos()
        x, y = self._clamp_overlay_position(current.x(), current.y())
        # Re-submit even when the logical coordinates did not change: a DPI or
        # projection switch can change the physical mapping and swap-chain size
        # without moving the logical overlay.
        self.overlay.move(x, y)
        if (x, y) != (current.x(), current.y()):
            self.on_overlay_position_changed(x, y)
        bridge = getattr(self, "bridge", None)
        if bridge is not None and not getattr(self, "_dashboard_frozen", False):
            monitors = [
                {"idx": i, "name": s.name(), "resolution": f"{s.geometry().width()}x{s.geometry().height()}"}
                for i, s in enumerate(screens)
            ]
            bridge.monitorsChanged.emit(json.dumps(monitors))
            bridge.selectedMonitorChanged.emit(int(self.selected_monitor))
        # Chromium may discard a canvas backing surface during a display-mode
        # switch even while the dashboard remains visible.  A deferred redraw
        # restores the customization preview after the new compositor device
        # is available, without introducing a persistent timer.
        redraw = getattr(self, "_redraw_dashboard_preview", None)
        if redraw is not None and not getattr(self, "_dashboard_frozen", False):
            QTimer.singleShot(80, redraw)
            QTimer.singleShot(120, self._refresh_dashboard_viewport)

    def _saved_overlay_position_is_visible(self, pos):
        try:
            x, y = pos
            x, y = int(x), int(y)
        except TypeError, ValueError, OverflowError:
            return False
        width = self.overlay.width()
        height = self.overlay.height()

        for screen in QApplication.screens():
            geo = screen.geometry()
            visible_x = x + width > geo.x() and x < geo.x() + geo.width()
            visible_y = y + height > geo.y() and y < geo.y() + geo.height()
            if visible_x and visible_y:
                return True
        return False

    def _place_overlay_for_start(self):
        screens = QApplication.screens()
        selected = screens[self.selected_monitor] if screens and 0 <= self.selected_monitor < len(screens) else None
        if selected is not None:
            # Restore the placement remembered for the selected screen; the
            # first session on a fresh monitor starts centred. The centre must
            # sit on the selected screen (names can repeat across adapters), so
            # a stale spot from a physically different screen still falls back.
            saved = self._saved_overlay_position_for_screen(selected.name())
            if (
                saved is not None
                and selected.geometry().contains(
                    saved[0] + self.overlay.width() // 2,
                    saved[1] + self.overlay.height() // 2,
                )
                and self._saved_overlay_position_is_visible(saved)
            ):
                x, y = self._clamp_overlay_position(saved[0], saved[1], selected)
            else:
                x, y = self._selected_monitor_center_position()
            self.overlay.move(x, y)
        self.emit_overlay_position()

    def set_overlay_drag_enabled(self, enabled: bool):
        enabled = bool(enabled)
        logger.info("overlay drag changed enabled=%s", enabled)
        if enabled and not self.overlay.isVisible():
            self.overlay.show()
            self._place_overlay_for_start()

        self.overlay.set_drag_enabled(enabled)

        if enabled:
            self.emit_overlay_position()
            return

        pos = self.overlay.pos()
        self.on_overlay_position_changed(pos.x(), pos.y())
        if not self.radar_active:
            self.overlay.hide()

    def move_overlay(self, x: int, y: int):
        if not self.overlay.isVisible():
            self.overlay.show()
            if not self.radar_active:
                self.overlay.set_drag_enabled(True)

        x, y = self._clamp_overlay_position(x, y)
        self.overlay.move(x, y)
        self.on_overlay_position_changed(x, y)

    def nudge_overlay(self, dx: int, dy: int):
        pos = self.overlay.pos()
        self.move_overlay(pos.x() + int(dx), pos.y() + int(dy))

    def reset_overlay_position(self):
        x, y = self._selected_monitor_center_position()
        logger.info("overlay position reset x=%s y=%s", x, y)
        self.move_overlay(x, y)

    # ── Profiles ──────────────────────────────────────────────────────
    def _load_profiles(self) -> dict:
        profiles = _load_json_mapping(PROFILES_FILE)
        normalized = {}
        for name, profile in profiles.items():
            if isinstance(profile, dict) and "name" not in profile:
                profile = {**profile, "name": name}
            valid = _normalize_profile(profile)
            if valid is None or not isinstance(name, str) or valid["name"] != name:
                logger.warning("Ignoring invalid profile name=%r", name)
                continue
            normalized[name] = valid
        profiles = normalized
        if not os.path.exists(PROFILES_FILE):
            _save_json_mapping(PROFILES_FILE, profiles)
        return profiles

    def _save_profiles(self):
        _save_json_mapping(PROFILES_FILE, self.profiles)

    def _load_settings(self) -> dict:
        settings = _load_json_mapping(SETTINGS_FILE)
        if not isinstance(settings, dict):
            logger.warning("Ignoring incompatible settings file: %s", SETTINGS_FILE)
            settings = {}
        if not os.path.exists(SETTINGS_FILE):
            _save_json_mapping(SETTINGS_FILE, settings)
        return settings

    def _save_settings(self):
        _save_json_mapping(SETTINGS_FILE, self.settings)

    # ── Lifecycle ─────────────────────────────────────────────────────
    def closeEvent(self, event):
        logger.info(
            "closeEvent closing_for_exit=%s finalized=%s state=%s thread_running=%s",
            self._closing_for_exit,
            self._exit_finalized,
            self.radar_operation_state,
            self.audio_thread is not None and self.audio_thread.isRunning(),
        )
        if not self._closing_for_exit:
            self._flush_pending_saves()
            self.hide()
            self._schedule_dashboard_freeze()
            event.ignore()
            return

        if hasattr(self, "hotkey_controller"):
            self.hotkey_controller.filter.unregister()
            QApplication.instance().removeNativeEventFilter(self.hotkey_controller.filter)

        self._flush_pending_saves()
        self._dashboard_freeze_timer.stop()
        if self._exit_finalized:
            logger.debug("closeEvent accepted after exit finalized")
            event.accept()
            return
        if self.radar_operation_state == "closing":
            logger.debug("closeEvent ignored while exit is still stopping")
            event.ignore()
            return
        event.ignore()
        self._closing_for_exit = True
        self._restart_requested = False
        self.radar_active = False
        self.overlay.hide()
        self._stop_audio_consumption()
        self._capture_watchdog.stop()
        self._watchdog_generation = None
        self._stop_program_list_thread()
        self._stop_mono_device_thread()
        self._set_operation_state("closing")
        if self.audio_thread is not None and self.audio_thread.isRunning():
            logger.info(
                "requesting capture stop generation=%s",
                self._active_capture_generation,
            )
            self.audio_thread.request_stop()
        else:
            logger.info("capture thread already stopped; finalizing exit")
            self._finalize_exit()

    def _finalize_exit(self):
        self._exit_finalize_pending = False
        if self._exit_finalized:
            logger.debug("finalize exit skipped: already finalized")
            return
        self._stop_program_list_thread()
        self._stop_mono_device_thread()
        if self.update_thread is not None and self.update_thread.isRunning():
            self.update_thread.requestInterruption()
        if any(
            thread is not None and thread.isRunning()
            for thread in (
                self._program_list_thread,
                self._mono_device_thread,
                self.update_thread,
            )
        ):
            logger.debug("finalize exit waiting for background threads")
            return
        self._exit_finalized = True
        logger.info("finalizing application exit")
        console_logger.info("Visual Audio Overlay stopped")
        self._capture_watchdog.stop()
        self._stop_audio_consumption()
        if self.tray_icon:
            self.tray_icon.hide()
        if self._single_instance_server is not None:
            self._single_instance_server.close()
        self.overlay.close()
        app = QApplication.instance()
        if app:
            app.quit()

    def _on_update_thread_finished(self):
        thread = self.update_thread
        self.update_thread = None
        if thread is not None:
            thread.deleteLater()
        self._maybe_finalize_exit()

    def _maybe_finalize_exit(self):
        if (
            self._closing_for_exit
            and not self._exit_finalize_pending
            and (self.audio_thread is None or not self.audio_thread.isRunning())
        ):
            self._exit_finalize_pending = True
            QTimer.singleShot(0, self._finalize_exit)


def _acquire_single_instance():
    """Return the listening server for the first process, or None for a client."""
    probe = QLocalSocket()
    probe.connectToServer(SINGLE_INSTANCE_NAME)
    if probe.waitForConnected(300):
        probe.write(b"restore")
        probe.flush()
        probe.waitForBytesWritten(200)
        probe.disconnectFromServer()
        return None

    server = QLocalServer()
    if not server.listen(SINGLE_INSTANCE_NAME):
        # A crashed process can leave the name behind. Remove only the local
        # endpoint and retry; an active server would have accepted the probe.
        QLocalServer.removeServer(SINGLE_INSTANCE_NAME)
        if not server.listen(SINGLE_INSTANCE_NAME):
            return None
    return server


if __name__ == "__main__":
    mp.freeze_support()
    console_logger.info(
        "\n"
        " __     ___                 _      _             _ _       \n"
        " \\ \\   / (_)___ _   _  __ _| |    / \\  _   _  __| (_) ___  \n"
        "  \\ \\ / /| / __| | | |/ _` | |   / _ \\| | | |/ _` | |/ _ \\ \n"
        "   \\ V / | \\__ \\ |_| | (_| | |  / ___ \\ |_| | (_| | | (_) |\n"
        "    \\_/  |_|___/\\__,_|\\__,_|_| /_/   \\_\\__,_|\\__,_|_|\\___/ \n"
        "\n"
        "              ___                 _             \n"
        "             / _ \\__   _____ _ __| | __ _ _   _ \n"
        "            | | | \\ \\ / / _ \\ '__| |/ _` | | | |\n"
        "            | |_| |\\ V /  __/ |  | | (_| | |_| |\n"
        "             \\___/  \\_/ \\___|_|  |_|\\__,_|\\__, |\n"
        "                                           \\___| \n"
        "\n"
        "                 [ SIGNAL IN ] -> [ RADAR OUT ]"
    )
    console_logger.info(
        "Starting Visual Audio Overlay (logging=%s, level=%s)",
        "enabled" if _logging_enabled else "disabled",
        "DEBUG" if _debug_logging else "INFO",
    )
    # Windows groups taskbar buttons (and picks their icon) by AppUserModelID.
    # Without an explicit ID, a `python main.py` launch shows the generic Python
    # icon in the taskbar even though setWindowIcon is set. Declaring our own ID
    # makes Windows treat this as a standalone app and use our icon there too.
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("VisualAudioOverlay.App")
        except Exception as exc:  # noqa: BLE001 - Windows shell integration is optional.
            logger.debug("AppUserModelID setup failed: %s", exc)

    qt_args = [arg for arg in sys.argv if arg != "--debug"]
    # Keep fractional monitor scales (125%, 150%, ... ) instead of allowing
    # Qt to round them to a neighbouring integer scale.  Rounding is especially
    # visible in QtWebEngine: Chromium allocates a backing surface for the
    # rounded DPR and Windows then stretches it to the actual monitor, making
    # text look soft and leaving CSS geometry one or two pixels out of phase.
    # This must be set before QApplication is constructed. Older Qt 6 builds may
    # not expose the policy; their default policy is still preferable to
    # preventing the dashboard from starting.
    with contextlib.suppress(AttributeError, TypeError):
        QGuiApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(qt_args)
    primary_screen = app.primaryScreen()
    if primary_screen is not None:
        logger.debug(
            "QtWebEngine scale configured initial_dpr=%.4f screen_dpr=%.4f flags=%s",
            _initial_windows_scale(),
            float(primary_screen.devicePixelRatio()),
            os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", ""),
        )
    single_instance_server = _acquire_single_instance()
    if single_instance_server is None:
        sys.exit(0)
    if os.path.exists(APP_ICON):
        app.setWindowIcon(QIcon(APP_ICON))
    window = AudioRadarApp(single_instance_server)
    window.show()
    sys.exit(app.exec())
