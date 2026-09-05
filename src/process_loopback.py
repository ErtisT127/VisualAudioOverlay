"""
Per-application audio capture via the Windows WASAPI **process loopback** API.

The regular loopback path (soundcard, in audio_capture.py) records the whole
system mix - so Discord voice, browser tabs, and your game all land in the same
signal. This module captures the audio of **one program** (and its child
processes) instead, so the radar only reacts to the game you picked.

It is Windows-only and needs Windows 10 build 20348 / Windows 11 or newer, the
first releases that expose `ActivateAudioInterfaceAsync` with
`AUDIOCLIENT_ACTIVATION_PARAMS`. On anything older (or if activation fails) the
caller is expected to fall back to whole-system capture.

Design notes
------------
* We reuse pycaw's `IAudioClient` / `WAVEFORMATEX` COM definitions and only hand-
  roll the few interfaces pycaw doesn't ship: the async-activation operation,
  its completion handler, and `IAudioCaptureClient`.
* Capture is event-driven shared mode: Windows sets an event each time a packet
  is ready, so `read()` blocks on that event instead of busy-spinning.
* Output matches what soundcard gives us - a float32 numpy array shaped
  (frames, channels) - so audio_capture.py's direction math is unchanged.
"""

import sys

# comtypes calls CoInitializeEx at *import* time. Its default is STA, but the
# `soundcard` library (used by audio_capture.py for whole-system loopback) puts
# the process into MTA. Importing comtypes after soundcard would then raise
# RPC_E_CHANGED_MODE. Requesting MTA here makes comtypes match soundcard's
# apartment, so both libraries coexist and the existing capture path is
# undisturbed. Must run before comtypes is first imported.
sys.coinit_flags = 0  # COINIT_MULTITHREADED

import ctypes
import threading
from ctypes import POINTER, byref, c_uint32, c_uint64, c_void_p, wintypes
from typing import ClassVar

import numpy as np
from comtypes import COMMETHOD, GUID, HRESULT, COMObject, IUnknown
from pycaw.api.audioclient import WAVEFORMATEX as _PycawWAVEFORMATEX

# NB: only IAudioClient is borrowed from pycaw. pycaw's own WAVEFORMATEX is wrong
# for this use - it types nSamplesPerSec / nAvgBytesPerSec as 16-bit, so a format
# we *construct* and pass into Initialize gets corrupted (-> E_INVALIDARG). We
# define a correct 18-byte WAVEFORMATEX below and cast to pycaw's pointer type.
from pycaw.api.audioclient import IAudioClient

from app_logging import get_logger

logger = get_logger("process_loopback")


class WAVEFORMATEX(ctypes.Structure):
    """Layout-correct WAVEFORMATEX (18 bytes), packed to match the Win32 header."""

    _pack_ = 1
    _fields_ = [
        ("wFormatTag", wintypes.WORD),
        ("nChannels", wintypes.WORD),
        ("nSamplesPerSec", wintypes.DWORD),
        ("nAvgBytesPerSec", wintypes.DWORD),
        ("nBlockAlign", wintypes.WORD),
        ("wBitsPerSample", wintypes.WORD),
        ("cbSize", wintypes.WORD),
    ]


# ── Constants ──────────────────────────────────────────────────────────────
AUDCLNT_SHAREMODE_SHARED = 0
AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
AUDCLNT_STREAMFLAGS_EVENTCALLBACK = 0x00040000
AUDCLNT_BUFFERFLAGS_SILENT = 0x2

WAVE_FORMAT_IEEE_FLOAT = 0x0003
VT_BLOB = 0x41  # 65

COINIT_MULTITHREADED = 0x0

# AUDIOCLIENT_ACTIVATION_TYPE
AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK = 1
# PROCESS_LOOPBACK_MODE
PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE = 0

# The magic device path that routes activation through the process-loopback APO.
VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK = "VAD\\Process_Loopback"

_kernel32 = ctypes.windll.kernel32
_mmdevapi = ctypes.windll.Mmdevapi
_ole32 = ctypes.windll.ole32

_kernel32.CreateEventW.restype = wintypes.HANDLE
_kernel32.CreateEventW.argtypes = [
    ctypes.c_void_p,
    wintypes.BOOL,
    wintypes.BOOL,
    wintypes.LPCWSTR,
]
_kernel32.WaitForSingleObject.restype = wintypes.DWORD
_kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_kernel32.CloseHandle.restype = wintypes.BOOL
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

# comtypes' CoInitializeEx forces an STA apartment, but ActivateAudioInterfaceAsync
# requires MTA - so we initialize COM ourselves via raw ole32.
RPC_E_CHANGED_MODE = 0x80010106
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258
WAIT_FAILED = 0xFFFFFFFF


def _co_initialize_mta() -> bool:
    """Initialize this thread's COM apartment as MTA. Returns True if we did the
    init (caller should later CoUninitialize), False if it was already set up."""
    hr = _ole32.CoInitializeEx(None, COINIT_MULTITHREADED)
    if hr & 0xFFFFFFFF == RPC_E_CHANGED_MODE:
        return False  # already STA on this thread - process loopback will fail
    return hr >= 0


# ── Activation structs (PROPVARIANT-wrapped) ───────────────────────────────
class AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS(ctypes.Structure):
    _fields_ = [
        ("TargetProcessId", wintypes.DWORD),
        ("ProcessLoopbackMode", ctypes.c_int),
    ]


class AUDIOCLIENT_ACTIVATION_PARAMS(ctypes.Structure):
    _fields_ = [
        ("ActivationType", ctypes.c_int),
        ("ProcessLoopbackParams", AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS),
    ]


class _BLOB(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.ULONG), ("pBlobData", c_void_p)]


class PROPVARIANT(ctypes.Structure):
    """Minimal PROPVARIANT - just enough to carry a VT_BLOB payload."""

    _fields_ = [
        ("vt", wintypes.WORD),
        ("wReserved1", wintypes.WORD),
        ("wReserved2", wintypes.WORD),
        ("wReserved3", wintypes.WORD),
        ("blob", _BLOB),
    ]


# ── COM interfaces pycaw doesn't provide ───────────────────────────────────
class IActivateAudioInterfaceAsyncOperation(IUnknown):
    _iid_ = GUID("{72A22D78-CDE4-431D-B8CC-843A71199B6D}")
    _methods_: ClassVar[list] = [
        COMMETHOD(
            [],
            HRESULT,
            "GetActivateResult",
            (["out"], POINTER(HRESULT), "activateResult"),
            (["out"], POINTER(POINTER(IUnknown)), "activatedInterface"),
        ),
    ]


class IAgileObject(IUnknown):
    """Marker interface (no methods). ActivateAudioInterfaceAsync marshals the
    completion handler onto the audio engine's own thread; unless the handler
    advertises itself as agile, that activation fails with E_ILLEGAL_METHOD_CALL.
    Implementing IAgileObject tells COM the object is free-threaded - no proxy
    needed - which is exactly true for our event-signalling handler."""

    _iid_ = GUID("{94EA2B94-E9CC-49E0-C0FF-EE64CA8F5B90}")
    _methods_: ClassVar[list] = []


class IActivateAudioInterfaceCompletionHandler(IUnknown):
    _iid_ = GUID("{41D949AB-9862-444A-80F6-C261334DA5EB}")
    _methods_: ClassVar[list] = [
        COMMETHOD(
            [],
            HRESULT,
            "ActivateCompleted",
            (
                ["in"],
                POINTER(IActivateAudioInterfaceAsyncOperation),
                "activateOperation",
            ),
        ),
    ]


class IAudioCaptureClient(IUnknown):
    _iid_ = GUID("{C8ADBD64-E71E-48A0-A4DE-185C395CD317}")
    _methods_: ClassVar[list] = [
        COMMETHOD(
            [],
            HRESULT,
            "GetBuffer",
            (["out"], POINTER(POINTER(wintypes.BYTE)), "ppData"),
            (["out"], POINTER(c_uint32), "pNumFramesToRead"),
            (["out"], POINTER(wintypes.DWORD), "pdwFlags"),
            (["out"], POINTER(c_uint64), "pu64DevicePosition"),
            (["out"], POINTER(c_uint64), "pu64QPCPosition"),
        ),
        COMMETHOD([], HRESULT, "ReleaseBuffer", (["in"], c_uint32, "NumFramesRead")),
        COMMETHOD(
            [],
            HRESULT,
            "GetNextPacketSize",
            (["out"], POINTER(c_uint32), "pNumFramesInNextPacket"),
        ),
    ]


class _CompletionHandler(COMObject):
    """Signals a Python Event when ActivateAudioInterfaceAsync finishes."""

    _com_interfaces_: ClassVar[list] = [
        IActivateAudioInterfaceCompletionHandler,
        IAgileObject,
    ]

    def __init__(self):
        super().__init__()
        self.done = threading.Event()

    def ActivateCompleted(self, this, activateOperation):
        self.done.set()
        return 0  # S_OK


# ActivateAudioInterfaceAsync(LPCWSTR, REFIID, PROPVARIANT*, handler, op**)
_ActivateAudioInterfaceAsync = _mmdevapi.ActivateAudioInterfaceAsync
_ActivateAudioInterfaceAsync.restype = ctypes.HRESULT
_ActivateAudioInterfaceAsync.argtypes = [
    ctypes.c_wchar_p,
    POINTER(GUID),
    POINTER(PROPVARIANT),
    POINTER(IActivateAudioInterfaceCompletionHandler),
    POINTER(POINTER(IActivateAudioInterfaceAsyncOperation)),
]


def is_supported() -> bool:
    """Process loopback needs Windows 10 build 20348+ (Windows 11 included)."""
    import sys

    try:
        wv = sys.getwindowsversion()
        return wv.major > 10 or (wv.major == 10 and wv.build >= 20348)
    except Exception as exc:  # noqa: BLE001 - platform capability probe is best effort.
        logger.warning("Windows version check failed: %s", exc)
        return False


class ProcessLoopbackCapture:
    """
    Captures the audio of a single process tree.

    Usage mirrors soundcard's recorder:
        cap = ProcessLoopbackCapture(pid)
        cap.start()
        frames = cap.read(2400)   # -> (2400, channels) float32
        cap.close()
    """

    def __init__(self, pid: int, samplerate: int = 48000, channels: int = 2):
        self.pid = int(pid)
        self.samplerate = samplerate
        self.channels = channels
        self._client = None
        self._capture = None
        self._event = None
        self._leftover = None
        self._started = False
        self._com_inited = False
        self._wait_timeouts = 0
        self._state_lock = threading.Lock()

    # ── Setup ──────────────────────────────────────────────────────────
    def start(self):
        """Activate + initialize the stream. Raises RuntimeError on any failure."""
        self._com_inited = _co_initialize_mta()

        try:
            self._client = self._activate_client()
            self._init_stream(self._client)
            self._client.Start()
            self._started = True
        except Exception as e:
            self.close()
            raise RuntimeError(f"process loopback failed: {e}") from e

    def _activate_client(self) -> IAudioClient:
        params = AUDIOCLIENT_ACTIVATION_PARAMS()
        params.ActivationType = AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK
        params.ProcessLoopbackParams.TargetProcessId = self.pid
        params.ProcessLoopbackParams.ProcessLoopbackMode = PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE

        pv = PROPVARIANT()
        pv.vt = VT_BLOB
        pv.blob.cbSize = ctypes.sizeof(params)
        pv.blob.pBlobData = ctypes.cast(byref(params), c_void_p)

        handler = _CompletionHandler()
        op = POINTER(IActivateAudioInterfaceAsyncOperation)()

        hr = _ActivateAudioInterfaceAsync(
            VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK,
            byref(IAudioClient._iid_),
            byref(pv),
            handler,
            byref(op),
        )
        if hr != 0:
            raise RuntimeError(f"ActivateAudioInterfaceAsync HRESULT 0x{hr & 0xFFFFFFFF:08X}")
        if not op:
            raise RuntimeError("ActivateAudioInterfaceAsync returned no operation")
        # `params`/`pv` must outlive the call above; they do (locals held to here).

        if not handler.done.wait(timeout=3.0):
            raise RuntimeError("activation timed out")

        activate_hr, unknown = op.GetActivateResult()
        if activate_hr != 0:
            raise RuntimeError(f"GetActivateResult HRESULT 0x{activate_hr & 0xFFFFFFFF:08X}")
        return unknown.QueryInterface(IAudioClient)

    def _init_stream(self, client: IAudioClient):
        wfx = WAVEFORMATEX()
        wfx.wFormatTag = WAVE_FORMAT_IEEE_FLOAT
        wfx.nChannels = self.channels
        wfx.nSamplesPerSec = self.samplerate
        wfx.wBitsPerSample = 32
        wfx.nBlockAlign = self.channels * 4
        wfx.nAvgBytesPerSec = self.samplerate * wfx.nBlockAlign
        wfx.cbSize = 0
        self._block_align = wfx.nBlockAlign

        # Event-driven shared mode: both durations MUST be 0. The Initialize
        # COMMETHOD expects pycaw's WAVEFORMATEX pointer type, so cast our
        # (correctly sized) struct to it.
        wfx_ptr = ctypes.cast(ctypes.byref(wfx), POINTER(_PycawWAVEFORMATEX))
        client.Initialize(
            AUDCLNT_SHAREMODE_SHARED,
            AUDCLNT_STREAMFLAGS_LOOPBACK | AUDCLNT_STREAMFLAGS_EVENTCALLBACK,
            0,
            0,
            wfx_ptr,
            None,
        )

        self._event = _kernel32.CreateEventW(None, False, False, None)
        if not self._event:
            raise RuntimeError("CreateEventW failed")
        client.SetEventHandle(self._event)

        ptr = client.GetService(byref(IAudioCaptureClient._iid_))
        self._capture = ptr.QueryInterface(IAudioCaptureClient)

    # ── Capture ────────────────────────────────────────────────────────
    def read(self, numframes: int) -> np.ndarray:
        """
        Block until ~numframes are available, then return exactly numframes rows.
        Short reads are zero-padded so the caller's timing/RMS stays stable even
        when the target app is silent (no packets arriving).
        """
        data = np.zeros((numframes, self.channels), dtype=np.float32)
        have = 0
        if self._leftover is not None:
            take = min(len(self._leftover), numframes)
            data[:take] = self._leftover[:take]
            have = take
            if take < len(self._leftover):
                self._leftover = self._leftover[take:]
            else:
                self._leftover = None

        while have < numframes:
            # Keep shutdown responsive even when the target is silent.
            with self._state_lock:
                event = self._event
                if not self._started or not event:
                    break
                wait_result = _kernel32.WaitForSingleObject(event, 50)
                if wait_result == WAIT_FAILED:
                    raise OSError(ctypes.get_last_error(), "audio capture event wait failed")
                if wait_result == WAIT_TIMEOUT:
                    self._wait_timeouts += 1
                    break
                if wait_result != WAIT_OBJECT_0:
                    raise RuntimeError(f"unexpected audio wait result {wait_result}")
                drained = self._drain_packets()
            if drained is not None:
                take = min(len(drained), numframes - have)
                data[have : have + take] = drained[:take]
                have += take
                if take < len(drained):
                    self._leftover = drained[take:].copy()
            else:
                break  # remaining rows are already zero-filled
        return data

    def _drain_packets(self) -> np.ndarray | None:
        out = []
        pkt = self._capture.GetNextPacketSize()
        while pkt and pkt > 0:
            data_ptr, nframes, flags, _dpos, _qpc = self._capture.GetBuffer()
            try:
                if nframes:
                    if flags & AUDCLNT_BUFFERFLAGS_SILENT:
                        arr = np.zeros((nframes, self.channels), dtype=np.float32)
                    else:
                        fptr = ctypes.cast(data_ptr, POINTER(ctypes.c_float))
                        arr = (
                            np.ctypeslib.as_array(fptr, shape=(nframes * self.channels,))
                            .reshape(nframes, self.channels)
                            .copy()
                        )
                    out.append(arr)
            finally:
                # WASAPI keeps the packet locked until ReleaseBuffer, including
                # when conversion or array handling raises an exception.
                self._capture.ReleaseBuffer(nframes)
            pkt = self._capture.GetNextPacketSize()
        if not out:
            return None
        return np.concatenate(out, axis=0)

    # ── Teardown ───────────────────────────────────────────────────────
    def close(self):
        with self._state_lock:
            self._started = False
            client = self._client
            event = self._event
            self._capture = None
            self._client = None
            self._event = None
        try:
            if client is not None:
                client.Stop()
        except Exception as exc:  # noqa: BLE001 - COM cleanup must not mask shutdown.
            logger.debug("audio client stop failed during cleanup: %s", exc)
        if event:
            try:
                _kernel32.CloseHandle(event)
            except Exception as exc:  # noqa: BLE001 - handle cleanup is best effort.
                logger.debug("capture event close failed: %s", exc)
        if self._com_inited:
            try:
                _ole32.CoUninitialize()
            except Exception as exc:  # noqa: BLE001 - COM cleanup must not mask shutdown.
                logger.debug("COM uninitialization failed: %s", exc)
            self._com_inited = False


# ── Program enumeration ────────────────────────────────────────────────────
def _sessions_all_render_devices():
    """Yield AudioSession objects across *every* active render endpoint.

    pycaw's AudioUtilities.GetAllSessions() only looks at the default playback
    device, so an app routed elsewhere (e.g. a game sent to VB-CABLE for the mono
    path) never shows up. We enumerate all ACTIVE render endpoints and pull the
    sessions from each so the app appears regardless of which output it plays to.
    """
    import comtypes
    from pycaw.api.audiopolicy import IAudioSessionControl2, IAudioSessionManager2
    from pycaw.api.mmdeviceapi import IMMDeviceEnumerator
    from pycaw.constants import DEVICE_STATE, CLSID_MMDeviceEnumerator, EDataFlow
    from pycaw.utils import AudioSession

    enumerator = comtypes.CoCreateInstance(CLSID_MMDeviceEnumerator, IMMDeviceEnumerator, comtypes.CLSCTX_INPROC_SERVER)
    devices = enumerator.EnumAudioEndpoints(EDataFlow.eRender.value, DEVICE_STATE.ACTIVE.value)

    for i in range(devices.GetCount()):
        dev = devices.Item(i)
        if dev is None:
            continue
        try:
            mgr = dev.Activate(IAudioSessionManager2._iid_, comtypes.CLSCTX_ALL, None).QueryInterface(
                IAudioSessionManager2
            )
            session_enum = mgr.GetSessionEnumerator()
        except Exception as exc:  # noqa: BLE001 - endpoint enumeration is best effort.
            logger.debug("audio session manager unavailable for endpoint %s: %s", i, exc)
            continue  # some endpoints refuse a session manager - skip them
        for j in range(session_enum.GetCount()):
            ctl = session_enum.GetSession(j)
            if ctl is None:
                continue
            try:
                ctl2 = ctl.QueryInterface(IAudioSessionControl2)
            except Exception as exc:  # noqa: BLE001 - session metadata is optional.
                logger.debug("audio session query failed endpoint=%s session=%s: %s", i, j, exc)
                continue
            if ctl2 is not None:
                yield AudioSession(ctl2)


def list_audio_programs() -> list[dict]:
    """
    Running programs that currently have an audio session, as
    [{"name": "Chrome", "pid": 1234}, ...] sorted by name.

    Scans every active render endpoint (not just the default device) so a program
    routed to a non-default output - e.g. a game sent to VB-CABLE for the mono
    path - still appears. Note: a program only shows up once it has opened an
    audio stream. The PID is a hint; resolve it freshly at capture time since
    PIDs can change.
    """
    try:
        sessions = list(_sessions_all_render_devices())
    except Exception as exc:  # noqa: BLE001 - endpoint enumeration is best effort.
        logger.warning("all-endpoint audio enumeration failed; using default endpoint: %s", exc)
        # Fall back to the default-device-only enumeration if the multi-device
        # scan fails for any reason, so the dropdown never goes empty.
        try:
            from pycaw.utils import AudioUtilities

            sessions = AudioUtilities.GetAllSessions()
        except Exception as exc:  # noqa: BLE001 - session enumeration is best effort.
            logger.error("default audio enumeration failed: %s", exc)
            return []

    found = {}
    for s in sessions:
        proc = getattr(s, "Process", None)
        if not proc:
            continue  # system sounds / no owning process
        try:
            name = proc.name()
        except Exception as exc:  # noqa: BLE001 - process metadata is optional.
            logger.debug("audio process name lookup failed: %s", exc)
            continue
        if not name:
            continue
        friendly = name[:-4] if name.lower().endswith(".exe") else name
        # First PID seen for a given program name wins (dedupe multi-process apps
        # and the same app appearing on more than one endpoint).
        process_id = getattr(s, "ProcessId", None)
        if process_id is None:
            process_id = getattr(proc, "pid", None)
        if process_id is None:
            continue
        found.setdefault(friendly, process_id)

    return [{"name": k, "pid": v} for k, v in sorted(found.items(), key=lambda kv: kv[0].lower())]


def resolve_pid(program_name: str) -> int | None:
    """Look up the current PID for a program name from live audio sessions."""
    for p in list_audio_programs():
        if p["name"].lower() == program_name.lower():
            return p["pid"]
    return None
