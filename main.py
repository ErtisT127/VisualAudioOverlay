# nuitka-project: --onefile
# nuitka-project: --enable-plugin=pyqt6
# nuitka-project: --include-qt-plugins=all
# nuitka-project: --windows-console-mode=disable
# nuitka-project: --windows-icon-from-ico=assets/icon.ico
# nuitka-project: --output-dir=dist/nuitka
# nuitka-project: --output-filename=VisualAudioOverlay.exe
# nuitka-project: --include-data-dir=dashboard_v2=dashboard_v2
# nuitka-project: --include-data-dir=assets=assets
# nuitka-project: --include-module=audio_capture
# nuitka-project: --include-module=direction
# nuitka-project: --include-module=overlay
# nuitka-project: --include-module=mono_output
# nuitka-project: --include-module=process_loopback
# nuitka-project: --include-package=comtypes
# nuitka-project: --include-package=pycaw
# nuitka-project: --include-package=soundcard
# nuitka-project: --include-package=psutil

import json
import multiprocessing as mp
import os
import sys
import tempfile

from PyQt6.QtCore import QObject, QThread, QTimer, QUrl, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QAction, QIcon
from PyQt6.QtNetwork import QLocalServer, QLocalSocket
from PyQt6.QtWebChannel import QWebChannel
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWidgets import QApplication, QMainWindow, QMenu, QSystemTrayIcon

from audio_capture import AudioCaptureThread
from overlay import OverlayRadar

# RESOURCE_DIR contains bundled, read-only assets. Nuitka resolves __file__ inside
# the deployed bundle; user data belongs next to the executable when packaged.
RESOURCE_DIR = os.path.dirname(os.path.abspath(__file__))
# Nuitka onefile executes the Python payload from a temporary extraction
# directory.  Persisted user data must instead follow the launcher executable.
_exe_name = os.path.basename(sys.executable).lower()
IS_PACKAGED = (
    "__compiled__" in globals()
    or bool(getattr(sys, "frozen", False))
    or not _exe_name.startswith(("python", "pypy"))
)
if IS_PACKAGED:
    # Nuitka onefile exposes the launch directory explicitly because the
    # payload's __file__/sys.executable can point at the temporary extraction.
    DATA_DIR = os.environ.get("NUITKA_ONEFILE_DIRECTORY") or os.path.dirname(
        os.path.abspath(sys.executable)
    )
else:
    DATA_DIR = RESOURCE_DIR

PROFILES_FILE = os.path.join(DATA_DIR, "profiles.json")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")

# Resolved against RESOURCE_DIR so it works both in dev and inside the
# packaged .exe.
DASHBOARD_FILE = os.path.join(RESOURCE_DIR, "dashboard_v2", "index.html")
if __name__ == "__main__":
    print(f"Dashboard: {DASHBOARD_FILE}  (exists: {os.path.exists(DASHBOARD_FILE)})")

# App icon (window/taskbar). Use the PNG, which Qt core handles without an image
# format plugin in the packaged executable. Nuitka uses the ICO for the executable
# file icon during the build.
APP_ICON = os.path.join(RESOURCE_DIR, "assets", "icon.png")
TRAY_ICON = os.path.join(RESOURCE_DIR, "assets", "icon.ico")

# ── Version + project links ─────────────────────────────────────────────────
# APP_VERSION must match the GitHub release tag (without the leading "v") for the
# update check to compare correctly. Bump this for every release you tag.
APP_VERSION = "0.2.2"
REPO_URL = "https://github.com/ErtisT127/VisualAudioOverlay"
# Latest-release JSON (no auth needed; 60 req/hr per IP is plenty for one check
# per launch). Used by the in-app update check to reach users who already have
# the app installed - we have no telemetry/emails, so this is the only channel.
REPO_LATEST_RELEASE_API = (
    "https://api.github.com/repos/ErtisT127/VisualAudioOverlay/releases/latest"
)
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
    name: {**_PRESET_LEVEL_DEFAULTS, **band}
    for name, band in {
        "All Sounds": {"freq_low": 20, "freq_high": 20000, "max_amp": 1.0},
        "Footsteps - CS2": {"freq_low": 150, "freq_high": 4000, "max_amp": 0.15},
        "Footsteps - Valorant": {"freq_low": 150, "freq_high": 4000, "max_amp": 0.12},
        "Footsteps - Fortnite": {"freq_low": 150, "freq_high": 5000, "max_amp": 0.18},
        "Footsteps - General": {"freq_low": 150, "freq_high": 4000, "max_amp": 0.15},
        "Custom": {"freq_low": 150, "freq_high": 4000, "max_amp": 1.0},
    }.items()
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


def _load_json_mapping(path: str) -> dict:
    """Read a JSON object, returning an empty mapping for missing/bad files."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            return data
        print(f"Ignoring non-object config file: {path}")
    except FileNotFoundError:
        pass
    except Exception as exc:
        print(f"Config read skipped for {path}: {exc}")
    return {}


def _save_json_mapping(path: str, data: dict) -> bool:
    """Atomically write a JSON object, keeping a failed write from truncating it."""
    temporary_path = None
    try:
        directory = os.path.dirname(path) or "."
        os.makedirs(directory, exist_ok=True)
        fd, temporary_path = tempfile.mkstemp(
            prefix=os.path.basename(path) + ".", suffix=".tmp", dir=directory
        )
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
        return True
    except Exception as exc:
        print(f"Config write skipped for {path}: {exc}")
        return False
    finally:
        if temporary_path:
            try:
                os.unlink(temporary_path)
            except OSError:
                pass


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
            if tag and _parse_version(tag) > _parse_version(APP_VERSION):
                self.updateFound.emit(tag.lstrip("vV"), url)
        except Exception:
            # Silent by design - a failed update check must never bother the user.
            pass


class ProgramListThread(QThread):
    """Enumerate audio sessions off the GUI thread (COM/WASAPI may block)."""

    result = pyqtSignal(str)

    def run(self):
        try:
            from process_loopback import list_audio_programs

            names = [p["name"] for p in list_audio_programs()]
        except Exception as exc:
            print(f"Program enumeration failed: {exc}")
            names = []
        self.result.emit(json.dumps(names))


class Bridge(QObject):
    # Signals → pushed to JS
    statusChanged = pyqtSignal(str, bool)  # (message, isActive)
    deviceChanged = pyqtSignal(str)  # detected device name
    profilesChanged = pyqtSignal(str)  # full profiles dict as JSON
    monitorsChanged = pyqtSignal(str)  # list of monitors as JSON
    presetsChanged = pyqtSignal(str)  # list of preset names as JSON
    programsChanged = pyqtSignal(str)  # running audio programs as JSON
    overlayPositionChanged = pyqtSignal(str)  # overlay position/state as JSON
    monoStateChanged = pyqtSignal(str)  # mono-output devices + cable state as JSON
    updateAvailable = pyqtSignal(str, str)  # (latest_version, release_page_url)
    appearanceChanged = pyqtSignal(
        str
    )  # saved overlay accent colour + thickness as JSON
    selectedPresetChanged = pyqtSignal(
        str
    )  # preset/profile name to restore in the dropdown
    audioSettingsChanged = pyqtSignal(
        str
    )  # all five live audio params, so JS moves the sliders
    operationBusyChanged = pyqtSignal(bool)  # Start/End lifecycle lock
    operationStateChanged = pyqtSignal(str)  # idle/starting/stopping

    def __init__(self, app: "AudioRadarApp"):
        super().__init__()
        self._app = app

    # ── Lifecycle ─────────────────────────────────────────────────────
    @pyqtSlot()
    def start_radar(self):
        self._app.start_radar()

    @pyqtSlot()
    def stop_radar(self):
        self._app.stop_radar()

    # ── Audio Settings ────────────────────────────────────────────────
    @pyqtSlot(float)
    def set_sensitivity(self, val: float):
        self._app.set_audio_param("sensitivity", val)

    @pyqtSlot(float)
    def set_gain(self, val: float):
        self._app.set_audio_param("gain", val)

    @pyqtSlot(int, int)
    def set_freq_range(self, low: int, high: int):
        self._app.set_audio_param("freq_low", low)
        self._app.set_audio_param("freq_high", high)

    @pyqtSlot(float)
    def set_max_amplitude(self, val: float):
        self._app.set_audio_param("max_amp", val)

    @pyqtSlot(str)
    def apply_preset(self, name: str):
        self._app.apply_preset(name)

    @pyqtSlot(str)
    def set_selected_preset(self, name: str):
        self._app.set_selected_preset(name)

    @pyqtSlot(bool)
    def set_invert(self, invert: bool):
        self._app.invert_direction = invert

    @pyqtSlot(int)
    def set_monitor(self, idx: int):
        self._app.selected_monitor = idx
        self._app._queue_settings_save()

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
        self._app.set_overlay_drag_enabled(enabled)

    @pyqtSlot(int, int)
    def set_overlay_position(self, x: int, y: int):
        self._app.move_overlay(x, y)

    @pyqtSlot(int, int)
    def nudge_overlay(self, dx: int, dy: int):
        self._app.nudge_overlay(dx, dy)

    @pyqtSlot()
    def reset_overlay_position(self):
        self._app.reset_overlay_position()

    # ── Overlay Appearance ────────────────────────────────────────────
    @pyqtSlot(str)
    def set_accent_color(self, hex_color: str):
        self._app.set_accent_color(hex_color)

    @pyqtSlot(int)
    def set_stroke_width(self, width: int):
        self._app.set_stroke_width(width)

    # ── Profiles ──────────────────────────────────────────────────────
    @pyqtSlot(str)
    def save_profile(self, json_str: str):
        """Expects JSON with at least { name } plus whatever the UI captures:
        sensitivity, gain, preset, freq_low, freq_high, max_amp, invert, and
        (since richer profiles) program, monitor, mono_enabled, mono_device,
        accent_color, thickness. Older profiles missing keys still load."""
        try:
            data = json.loads(json_str)
            name = data.get("name", "").strip()
            if not name:
                return
            self._app.profiles[name] = data
            self._app._save_profiles()
            # The profile you just made is the one you are working in - selecting
            # it is what puts later changes on the auto-update path. The dashboard
            # selects it in the dropdown off the same name (see AR.addPreset).
            self._app.set_selected_preset(name)
            self.profilesChanged.emit(json.dumps(self._app.profiles))
        except Exception as e:
            print(f"save_profile error: {e}")

    @pyqtSlot(str, result=str)
    def get_profile(self, name: str) -> str:
        """Returns a single profile as JSON string (for loading into UI)."""
        p = self._app.profiles.get(name, {})
        return json.dumps(p)

    @pyqtSlot(str)
    def delete_profile(self, name: str):
        if name in self._app.profiles:
            del self._app.profiles[name]
            self._app._save_profiles()
            # Deleting the profile that is currently selected would leave a name
            # in settings.json that no dropdown entry can ever match, so nothing
            # would be restored on the next launch and the label would drift from
            # the running band. Fall back to the neutral preset.
            if self._app.selected_preset == name:
                self._app.set_selected_preset("All Sounds")
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
        screens = QApplication.screens()
        monitors = [
            {
                "idx": i,
                "name": s.name(),
                "resolution": f"{s.geometry().width()}×{s.geometry().height()}",
            }
            for i, s in enumerate(screens)
        ]
        self.monitorsChanged.emit(json.dumps(monitors))

        # Preset/profile selection saved from the last session. Emitted BEFORE the
        # two lists that build the dropdown, so JS knows what to re-select as soon
        # as the matching option appears (JS also re-checks on every rebuild, so
        # the order here is a convenience, not a requirement).
        self._app.emit_selected_preset()

        # Live audio parameters (sliders). Restored values, not a preset's - the
        # saved name is only re-selected in the dropdown, never re-applied, so a
        # profile cannot overwrite settings changed after it was chosen.
        self._app.emit_audio_settings()

        # Profiles
        self.profilesChanged.emit(json.dumps(self._app.profiles))

        # Presets
        self.presetsChanged.emit(json.dumps(list(SOUND_PRESETS.keys())))

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
    def __init__(self, single_instance_server=None):
        super().__init__()
        self.setWindowTitle("Visual Audio Overlay")
        if os.path.exists(APP_ICON):
            self.setWindowIcon(QIcon(APP_ICON))
        self.resize(1100, 720)
        self._closing_for_exit = False
        self._exit_finalized = False
        self.radar_operation_state = "idle"
        self._capture_watchdog = QTimer(self)
        self._capture_watchdog.setSingleShot(True)
        self._capture_watchdog.setInterval(10000)
        self._capture_watchdog.timeout.connect(self._on_capture_timeout)
        self._program_list_thread = None
        self._pending_capture_restart = False
        self._capture_terminal_error = None
        self._single_instance_server = single_instance_server
        if self._single_instance_server is not None:
            self._single_instance_server.newConnection.connect(
                self._on_single_instance_connection
            )
        self._setup_tray_icon()

        self.invert_direction = False
        self.selected_monitor = 0
        self.selected_program = None  # None = whole-system audio; else a program name
        self.profiles = self._load_profiles()
        self.settings = self._load_settings()
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
            "freq_low": 150,  # freq slider default (matches SOUND_PRESETS)
            "freq_high": 4000,
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
        self.selected_preset = (
            saved_preset
            if isinstance(saved_preset, str) and saved_preset
            else "All Sounds"
        )

        # Mono output (single-sided listeners). Persisted in settings.json so the
        # user's choice survives restarts; applied to the audio thread on Start.
        self.mono_enabled = bool(self.settings.get("mono_enabled", False))
        self.mono_device = self.settings.get("mono_device") or None

        # Overlay (PyQt6 transparent window - unchanged)
        self.overlay = OverlayRadar()

        # Overlay appearance (accent colour + stroke). Persisted in settings.json so
        # the look survives restarts; applied to the overlay now and pushed to the
        # dashboard via emit_appearance() on load. Default matches the UI swatch.
        saved_color = self.settings.get("accent_color")
        self.accent_color = (
            saved_color if isinstance(saved_color, str) and saved_color else "#9751F2"
        )
        try:
            self.stroke_width = int(self.settings.get("stroke_width", 6))
        except (TypeError, ValueError):
            self.stroke_width = 6
        self.overlay.set_accent_color(self.accent_color)
        self.overlay.set_stroke_width(self.stroke_width)

        # Audio thread (starts idle, no capture yet)
        self._new_audio_thread()

        # Bridge object exposed to JS
        self.bridge = Bridge(self)
        self.overlay.positionChanged.connect(self.on_overlay_position_changed)
        self.overlay.positionPreview.connect(self.on_overlay_position_preview)

        # WebEngine view
        self.view = QWebEngineView()

        # WebChannel - registers `bridge` as `window.bridge` in JS
        self.channel = QWebChannel()
        self.channel.registerObject("bridge", self.bridge)
        self.view.page().setWebChannel(self.channel)

        self.setCentralWidget(self.view)

        # Load the dashboard HTML
        self.view.setUrl(QUrl.fromLocalFile(DASHBOARD_FILE))

        # Check GitHub for a newer release in the background. If found, the bridge
        # forwards it to JS, which shows a small dismissible "update available"
        # banner. Silent on any failure - never blocks or nags. Kept as an
        # attribute so the QThread isn't garbage-collected mid-run.
        global _UPDATE_CHECK_STARTED
        self.update_thread = None
        if not _UPDATE_CHECK_STARTED:
            _UPDATE_CHECK_STARTED = True
            self.update_thread = UpdateCheckThread()
            self.update_thread.updateFound.connect(self.bridge.updateAvailable)
            self.update_thread.start()

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
        self.showNormal()
        self.raise_()
        self.activateWindow()

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
        self._closing_for_exit = True
        self.close()

    # ── Audio Callbacks ───────────────────────────────────────────────
    def on_audio_data(self, angle: float, intensity: float):
        if self.invert_direction:
            angle = -angle
        self.overlay.update_audio_data(angle, intensity)

    def on_device_info(self, name: str, channels: int):
        label = f"{name}  ({channels}ch)"
        self.bridge.deviceChanged.emit(label)

    def on_capture_status(self, message: str):
        """Non-terminal capture status intended for the UI."""
        self.bridge.statusChanged.emit(message, self.radar_active)

    def _on_capture_error(self, message: str):
        """Remember terminal capture errors so cleanup cannot overwrite them."""
        self._capture_terminal_error = message
        self.radar_active = False
        self.overlay.hide()
        self.bridge.statusChanged.emit(message, False)

    def _new_audio_thread(self):
        """QThreads aren't restartable, so a fresh (idle) thread is created here
        on init, after every Stop, and on every mid-session capture restart."""
        self.audio_thread = AudioCaptureThread()
        self.audio_thread.audio_data_signal.connect(self.on_audio_data)
        self.audio_thread.device_info_signal.connect(self.on_device_info)
        self.audio_thread.status_signal.connect(self.on_capture_status)
        self.audio_thread.error_signal.connect(self._on_capture_error)
        self.audio_thread.ready_signal.connect(self._on_capture_ready)
        self.audio_thread.finished.connect(self._on_capture_finished)

    def on_overlay_position_changed(self, x: int, y: int):
        self.settings["overlay_position"] = {"x": int(x), "y": int(y)}
        self._save_settings()
        self.emit_overlay_position()

    def on_overlay_position_preview(self, x: int, y: int):
        """Live drag frames: refresh the UI readout only, no disk write.
        The final position is persisted once on mouse release via
        on_overlay_position_changed (issue #3)."""
        state = {
            "x": int(x),
            "y": int(y),
            "drag_enabled": bool(self.overlay.drag_enabled),
        }
        self.bridge.overlayPositionChanged.emit(json.dumps(state))

    def emit_overlay_position(self):
        pos = self.overlay.pos()
        state = {
            "x": int(pos.x()),
            "y": int(pos.y()),
            "drag_enabled": bool(self.overlay.drag_enabled),
        }
        self.bridge.overlayPositionChanged.emit(json.dumps(state))

    # ── Programs (per-app capture) ────────────────────────────────────
    def set_program(self, program):
        """Change the capture target. Applies live when the radar is running."""
        if program == self.selected_program:
            return
        self.selected_program = program
        self._queue_settings_save()
        self._restart_capture_if_active()

    def list_programs(self):
        """Running programs with an audio session (legacy synchronous helper)."""
        try:
            from process_loopback import list_audio_programs

            return list_audio_programs()
        except Exception as e:
            print(f"Program enumeration failed: {e}")
            return []

    def emit_programs(self):
        if (
            self._program_list_thread is not None
            and self._program_list_thread.isRunning()
        ):
            return
        self._program_list_thread = ProgramListThread(self)
        self._program_list_thread.result.connect(self.bridge.programsChanged)
        self._program_list_thread.finished.connect(self._on_program_list_finished)
        self._program_list_thread.start()

    def _on_program_list_finished(self):
        thread = self._program_list_thread
        self._program_list_thread = None
        if thread is not None:
            thread.deleteLater()

    # ── Overlay appearance (accent colour + stroke) ───────────────────
    def set_accent_color(self, hex_color: str):
        self.accent_color = hex_color or "#9751F2"
        self.overlay.set_accent_color(self.accent_color)
        self.settings["accent_color"] = self.accent_color
        self._queue_settings_save()

    def set_stroke_width(self, width: int):
        self.stroke_width = int(width)
        self.overlay.set_stroke_width(self.stroke_width)
        self.settings["stroke_width"] = self.stroke_width
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
        self.mono_enabled = enabled
        self.settings["mono_enabled"] = self.mono_enabled
        self._queue_settings_save()
        self.emit_mono_state()
        if changed:
            self._restart_capture_if_active()

    def set_mono_output(self, device: str):
        device = device or None
        changed = device != self.mono_device
        self.mono_device = device
        self.settings["mono_device"] = self.mono_device
        self._queue_settings_save()
        self.emit_mono_state()
        if changed:
            self._restart_capture_if_active()

    def emit_mono_state(self):
        """Push the playback-device list + VB-CABLE detection + current selection
        to the UI so it can render the mono setup card."""
        try:
            from mono_output import (
                default_output_name,
                detect_virtual_cable,
                list_output_devices,
            )

            devices = list_output_devices()
            default = default_output_name()
            cable = detect_virtual_cable()
        except Exception as e:
            print(f"Mono device enumeration failed: {e}")
            devices, default, cable = [], None, None

        state = {
            "devices": devices,
            "default": default,
            "cable": cable,  # None until VB-CABLE is installed
            "enabled": self.mono_enabled,
            "selected": self.mono_device,
        }
        self.bridge.monoStateChanged.emit(json.dumps(state))

    def install_vbcable(self):
        """Launch the bundled VB-CABLE installer with a UAC prompt. If the build
        doesn't bundle it, open the official download page instead. The installer
        shows its own UI on purpose (donationware terms + trust for the
        anti-cheat-wary audience)."""
        installer = os.path.join(
            RESOURCE_DIR, "vendor", "VBCABLE", "VBCABLE_Setup_x64.exe"
        )
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
            except Exception as e:
                print(f"VB-CABLE launch failed: {e}")
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
        except Exception:
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
        if not isinstance(saved, dict):
            return
        for key, cast in (
            ("sensitivity", float),
            ("gain", float),
            ("freq_low", int),
            ("freq_high", int),
            ("max_amp", float),
        ):
            if key in saved:
                try:
                    self.audio_settings[key] = cast(saved[key])
                except (TypeError, ValueError):
                    pass

    def _queue_settings_save(self):
        """Stage the live audio parameters and debounce the write (see the timer).
        Also the entry point for the profile auto-update, so every setter that
        changes something a profile stores should call this."""
        self.settings["audio_settings"] = dict(self.audio_settings)
        self._settings_save_timer.start()

    def _write_pending_saves(self):
        """The debounced write itself: settings.json, then the selected profile."""
        self._save_settings()
        self._sync_selected_profile()

    def _flush_pending_saves(self):
        """Write a pending change now instead of waiting the debounce out. Used on
        close and before switching profiles - a slider moved in the last 600ms
        belongs to the profile it was moved in, not the one being switched to."""
        if self._settings_save_timer.isActive():
            self._settings_save_timer.stop()
            self._write_pending_saves()

    def _sync_selected_profile(self):
        """Auto-update: while one of the user's own profiles is selected, live
        changes are written back into it, so switching away and back returns the
        values you last had rather than the snapshot taken when it was created.

        Built-in presets are read-only and fall straight through - `profiles` only
        ever holds user-made entries, so the lookup is the whole check."""
        prof = self.profiles.get(self.selected_preset)
        if prof is None:
            return
        updated = {
            k: round(self.audio_settings[k] * scale)
            for k, scale in PROFILE_SLIDER_SCALE.items()
        }
        updated.update(
            {
                "program": self.selected_program or "all",
                "monitor": self.selected_monitor,
                "mono_enabled": self.mono_enabled,
                "mono_device": self.mono_device or "",
                "accent_color": self.accent_color,
                "thickness": self.stroke_width,
            }
        )
        if all(prof.get(k) == v for k, v in updated.items()):
            return  # nothing moved: no disk write, no rebuild
        prof.update(updated)
        self._save_profiles()
        # The dashboard caches profiles to feed applyProfileValues, so it has to
        # see the new values or switching back would replay the stale ones.
        self.bridge.profilesChanged.emit(json.dumps(self.profiles))

    def emit_audio_settings(self):
        """Push the live audio parameters to the dashboard so the sliders show what
        the capture thread is actually using."""
        self.bridge.audioSettingsChanged.emit(json.dumps(self.audio_settings))

    def set_audio_param(self, key: str, value):
        """Update one live audio parameter. Stored on the app (so it survives a
        thread restart), applied to the running thread immediately, and queued for
        persistence so it also survives a restart."""
        self.audio_settings[key] = value
        self._apply_audio_settings_to_thread()
        self._queue_settings_save()

    def set_selected_preset(self, name: str):
        """Remember which dropdown entry (built-in preset or saved profile) is
        selected. Written straight to settings.json: this only fires when the
        user picks an entry, not on slider drags, so it is not a hot path. The
        no-op guard matters anyway - the dashboard replays the restored name
        through the same path a click takes, which would otherwise rewrite the
        file with identical contents on every launch."""
        name = name or ""
        if name == self.selected_preset:
            return
        self._flush_pending_saves()
        self.selected_preset = name
        self.settings["selected_preset"] = name
        self._save_settings()

    def emit_selected_preset(self):
        self.bridge.selectedPresetChanged.emit(self.selected_preset)

    def apply_preset(self, name: str):
        """Apply a built-in preset and echo the values back to the dashboard so the
        sliders and readouts actually move (otherwise switching presets looks like
        it does nothing).

        Every parameter is overwritten, never just the ones the preset cares about
        - see the note on SOUND_PRESETS for why a partial apply leaks."""
        p = SOUND_PRESETS.get(name, SOUND_PRESETS["All Sounds"])
        for key in self.audio_settings:
            self.audio_settings[key] = p[key]
        self._apply_audio_settings_to_thread()
        self._queue_settings_save()
        self.emit_audio_settings()

    # ── Radar Control ─────────────────────────────────────────────────
    def _start_capture_thread(self):
        """Configure the idle thread (target/mono/params are read at thread start)
        and start it. Shared by Start and by mid-session capture restarts."""
        # Resolve the capture target fresh (PIDs change between launches).
        pid, name = self._resolve_target()
        self.audio_thread.set_target(pid, name)
        self._capture_label = (
            name if pid is not None else (self.selected_program or "system audio")
        )
        self.audio_thread.set_mono(self.mono_enabled, self.mono_device)
        self._apply_audio_settings_to_thread()

        if not self.audio_thread.isRunning():
            self.audio_thread.start()

        # The UI remains in a loading state until the worker emits ready_signal.
        self.bridge.statusChanged.emit("Starting audio capture...", False)

    def _set_operation_state(self, state):
        self.radar_operation_state = state
        self.bridge.operationBusyChanged.emit(state != "idle")
        self.bridge.operationStateChanged.emit(state)

    def _on_capture_ready(self):
        if self.radar_operation_state != "starting":
            return
        self._capture_watchdog.stop()
        self._capture_terminal_error = None
        self.radar_active = True
        self._set_operation_state("idle")
        if self.selected_program and self._capture_label != "system audio":
            self.bridge.statusChanged.emit(
                f"Radar active - capturing {self._capture_label}", True
            )
        else:
            self.bridge.statusChanged.emit("Radar is active", True)

    def _on_capture_timeout(self):
        if self.radar_operation_state != "starting":
            return
        label = (
            getattr(self, "_capture_label", None)
            or self.selected_program
            or "system audio"
        )
        self._capture_terminal_error = (
            f"Capture of {label} stopped unexpectedly - restart the radar"
        )
        print(self._capture_terminal_error)
        self.radar_active = False
        self.overlay.hide()
        self._set_operation_state("stopping")
        self.audio_thread.request_stop()

    def _on_capture_finished(self):
        if self.radar_operation_state not in ("starting", "stopping"):
            return
        self._capture_watchdog.stop()
        self.radar_active = False
        self.overlay.hide()
        if self._closing_for_exit:
            self._finalize_exit()
            return
        if self._pending_capture_restart:
            self._pending_capture_restart = False
            self._capture_terminal_error = None
            self._new_audio_thread()
            self._set_operation_state("starting")
            self._start_capture_thread()
            self._capture_watchdog.start()
            return
        self._new_audio_thread()
        self._set_operation_state("idle")
        message = self._capture_terminal_error or "Radar stopped"
        self._capture_terminal_error = None
        self.bridge.statusChanged.emit(message, False)
        self.emit_overlay_position()

    def start_radar(self):
        if self.radar_operation_state != "idle":
            return
        self._set_operation_state("starting")
        self._capture_terminal_error = None
        self.radar_active = False
        self.overlay.show()
        self._place_overlay_for_start()
        self._start_capture_thread()
        self._capture_watchdog.start()
        # Refresh the program list so newly launched apps show up next time.
        self.emit_programs()

    def _restart_capture_if_active(self):
        """Program/mono choices only take effect when the capture thread starts,
        so apply a mid-session change by restarting the thread in place. The
        overlay stays up; only the capture source blips out for a moment."""
        if not self.radar_active:
            return
        self.audio_thread.request_stop()
        self._set_operation_state("stopping")
        self._pending_capture_restart = True

    def stop_radar(self):
        if self.radar_operation_state != "idle":
            return
        self.radar_active = False
        self.overlay.set_drag_enabled(False)
        self.overlay.hide()
        self._set_operation_state("stopping")
        self._capture_watchdog.stop()
        self.audio_thread.request_stop()

    def _selected_monitor_center_position(self):
        screens = QApplication.screens()
        idx = self.selected_monitor if self.selected_monitor < len(screens) else 0
        geo = screens[idx].geometry()
        return (
            geo.x() + (geo.width() - self.overlay.width()) // 2,
            geo.y() + (geo.height() - self.overlay.height()) // 2,
        )

    def _saved_overlay_position_is_visible(self, pos):
        if not isinstance(pos, dict) or "x" not in pos or "y" not in pos:
            return False

        x = int(pos["x"])
        y = int(pos["y"])
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
        pos = self.settings.get("overlay_position")
        if self._saved_overlay_position_is_visible(pos):
            self.overlay.move(int(pos["x"]), int(pos["y"]))
        else:
            x, y = self._selected_monitor_center_position()
            self.overlay.move(x, y)
        self.emit_overlay_position()

    def set_overlay_drag_enabled(self, enabled: bool):
        enabled = bool(enabled)
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

        self.overlay.move(int(x), int(y))
        self.on_overlay_position_changed(int(x), int(y))

    def nudge_overlay(self, dx: int, dy: int):
        pos = self.overlay.pos()
        self.move_overlay(pos.x() + int(dx), pos.y() + int(dy))

    def reset_overlay_position(self):
        x, y = self._selected_monitor_center_position()
        self.move_overlay(x, y)

    # ── Profiles ──────────────────────────────────────────────────────
    def _load_profiles(self) -> dict:
        profiles = _load_json_mapping(PROFILES_FILE)
        if not os.path.exists(PROFILES_FILE):
            _save_json_mapping(PROFILES_FILE, profiles)
        return profiles

    def _save_profiles(self):
        _save_json_mapping(PROFILES_FILE, self.profiles)

    def _load_settings(self) -> dict:
        settings = _load_json_mapping(SETTINGS_FILE)
        if not os.path.exists(SETTINGS_FILE):
            _save_json_mapping(SETTINGS_FILE, settings)
        return settings

    def _save_settings(self):
        _save_json_mapping(SETTINGS_FILE, self.settings)

    # ── Lifecycle ─────────────────────────────────────────────────────
    def closeEvent(self, event):
        if not self._closing_for_exit:
            self._flush_pending_saves()
            self.hide()
            event.ignore()
            return

        self._flush_pending_saves()
        event.ignore()
        self.radar_active = False
        self.overlay.hide()
        self._capture_watchdog.stop()
        if self.audio_thread.isRunning():
            self._set_operation_state("stopping")
            self.audio_thread.request_stop()
        else:
            self._finalize_exit()

    def _finalize_exit(self):
        if self._exit_finalized:
            return
        self._exit_finalized = True
        if self.tray_icon:
            self.tray_icon.hide()
        if self._single_instance_server is not None:
            self._single_instance_server.close()
        self.overlay.close()
        app = QApplication.instance()
        if app:
            app.quit()


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
    # Windows groups taskbar buttons (and picks their icon) by AppUserModelID.
    # Without an explicit ID, a `python main.py` launch shows the generic Python
    # icon in the taskbar even though setWindowIcon is set. Declaring our own ID
    # makes Windows treat this as a standalone app and use our icon there too.
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "VisualAudioOverlay.App"
            )
        except Exception:
            pass

    app = QApplication(sys.argv)
    single_instance_server = _acquire_single_instance()
    if single_instance_server is None:
        sys.exit(0)
    if os.path.exists(APP_ICON):
        app.setWindowIcon(QIcon(APP_ICON))
    window = AudioRadarApp(single_instance_server)
    window.show()
    sys.exit(app.exec())
