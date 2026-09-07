"""
Per-application audio capture via the Windows WASAPI **process loopback** API.

The regular loopback path (soundcard, in audio_capture.py) records the whole
system mix - so Discord voice, browser tabs, and your game all land in the same
signal. This module captures the audio of **one program** (and its child
processes) instead, so the radar only reacts to the game you picked.

It is Windows-only and needs Windows 10 build 20348 / Windows 11 or newer, the
first releases that expose `ActivateAudioInterfaceAsync` with
`AUDIOCLIENT_ACTIVATION_PARAMS`. On anything older, per-app capture is
unsupported: the dashboard disables the program dropdown and whole-system
capture is only ever chosen explicitly by the user - never silently. If
activation still fails at capture time, that is an error, not a fallback.

Design notes
------------
* We reuse pycaw's `IAudioClient` / `WAVEFORMATEX` COM definitions and only hand-
  roll the few interfaces pycaw doesn't ship: the async-activation operation,
  its completion handler, and `IAudioCaptureClient`.
* Capture is event-driven shared mode: Windows sets an event each time a packet
  is ready, so `read()` blocks on that event instead of busy-spinning.
* Output matches what soundcard gives us - a float32 numpy array shaped
  (frames, channels) - so audio_capture.py's direction math is unchanged.
* Importing this module runs comtypes, which initializes COM as MTA (see the
  coinit_flags comment at the top). The Qt GUI thread is STA, so a first import
  there raises RPC_E_CHANGED_MODE. First imports must therefore happen on a Qt
  worker thread (ProgramListThread) or in a capture worker process; the GUI
  thread only reads the capability result and calls the pure-ctypes helpers
  below (is_supported, find_process_pid, is_process_alive).
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


class WAVEFORMATEXTENSIBLE(ctypes.Structure):
    """Layout-correct WAVEFORMATEXTENSIBLE (40 bytes). The leading 18 bytes are
    the WAVEFORMATEX header; the rest carries the channel mask and subformat
    that WASAPI needs to describe a multichannel wire. Passed to Initialize as
    a WAVEFORMATEX pointer - the callee only reads what cbSize promises."""

    _pack_ = 1
    _fields_ = [
        ("wFormatTag", wintypes.WORD),
        ("nChannels", wintypes.WORD),
        ("nSamplesPerSec", wintypes.DWORD),
        ("nAvgBytesPerSec", wintypes.DWORD),
        ("nBlockAlign", wintypes.WORD),
        ("wBitsPerSample", wintypes.WORD),
        ("cbSize", wintypes.WORD),
        ("wValidBitsPerSample", wintypes.WORD),
        ("dwChannelMask", wintypes.DWORD),
        ("SubFormat", wintypes.BYTE * 16),
    ]


# ── Constants ──────────────────────────────────────────────────────────────
AUDCLNT_SHAREMODE_SHARED = 0
AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
AUDCLNT_STREAMFLAGS_EVENTCALLBACK = 0x00040000
# Ask the engine to convert the endpoint mix to our requested format instead of
# failing on a non-48k default endpoint - the same pair soundcard passes.
AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM = 0x80000000
AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY = 0x08000000
AUDCLNT_BUFFERFLAGS_SILENT = 0x2

WAVE_FORMAT_IEEE_FLOAT = 0x0003
WAVE_FORMAT_EXTENSIBLE = 0xFFFE
VT_BLOB = 0x41  # 65

# KSDATAFORMAT_SUBTYPE_IEEE_FLOAT as raw bytes, for WAVEFORMATEXTENSIBLE
# requests built from a mix format whose own subformat could not be read.
IEEE_FLOAT_SUBFORMAT = bytes.fromhex("0300000000001000800000aa00389b71")

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

# ── Process snapshot (CreateToolhelp32Snapshot) for PID lookup/alive checks ──
TH32CS_SNAPPROCESS = 0x2
STILL_ACTIVE = 259
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(wintypes.ULONG)),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


_kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
_kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
_kernel32.Process32FirstW.restype = wintypes.BOOL
_kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
_kernel32.Process32NextW.restype = wintypes.BOOL
_kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.GetExitCodeProcess.restype = wintypes.BOOL
_kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]

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
        # Fallback only: start() overwrites this with the endpoint's real mix
        # format channel count when the GetMixFormat probe succeeds.
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
            spec = self._resolve_mix_spec(self._client)
            self._init_stream(self._client, spec)
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

    def _resolve_mix_spec(self, client: IAudioClient):
        """Read the wire's real channel layout from the endpoint's shared-mode
        mix format, so a multichannel wire (e.g. 7.1 routed to VB-CABLE) is
        captured with all of its channels instead of a hard-coded stereo pair.
        Returns a {"channels", "mask", "subformat"} spec, or None when nothing
        can be resolved - the caller then falls back to stereo. A failed probe
        must never break capture.

        The process-loopback virtual client answers GetMixFormat with
        E_NOTIMPL, so the spec normally comes from the render endpoint the
        target's audio session actually plays to (same format the engine
        mixes into)."""
        pwfx = None
        try:
            pwfx = client.GetMixFormat()
            spec = _spec_from_wave_format(pwfx)
            logger.info("mix format reports %d channel(s) for pid=%s", spec["channels"], self.pid)
            return spec
        except Exception as exc:  # noqa: BLE001 - the probe must never break capture.
            spec = _session_endpoint_mix_spec(self.pid)
            if spec is not None:
                logger.info("endpoint mix format reports %d channel(s) for pid=%s", spec["channels"], self.pid)
                return spec
            logger.warning("mix format query failed; keeping %d channel(s): %s", self.channels, exc)
            return None
        finally:
            # GetMixFormat allocates with CoTaskMemAlloc and comtypes does not
            # manage the buffer - free it ourselves after reading.
            if pwfx:
                _ole32.CoTaskMemFree(pwfx)

    def _init_stream(self, client: IAudioClient, spec):
        """Open the capture stream. A multichannel wire needs a
        WAVEFORMATEXTENSIBLE request - the process-loopback client rejects
        plain-header (18-byte WAVEFORMATEX) requests above 2 channels with
        E_INVALIDARG - so when the spec says >2ch, replay the endpoint's own
        extensible fields (channel mask, subformat) at float32/our samplerate
        with AUTOCONVERTPCM doing any conversion. If the engine still refuses
        (some endpoints only offer stereo), retry the original plain stereo
        request so capture always opens. `self.channels` ends up as the real
        captured channel count either way."""
        if spec is not None and spec["channels"] > 2:
            try:
                self._open_stream(client, spec["channels"], spec)
                self.channels = spec["channels"]
                logger.info("capturing %d-channel wire (extensible format)", spec["channels"])
                return
            except Exception as exc:  # noqa: BLE001 - fall back rather than break capture.
                if self._event:
                    _kernel32.CloseHandle(self._event)
                    self._event = None
                logger.warning("multichannel request refused (%s); retrying stereo", exc)
        self._open_stream(client, 2, None)
        self.channels = 2

    def _open_stream(self, client: IAudioClient, channels, ext):
        """One Initialize attempt: `ext` None -> plain float32 WAVEFORMATEX
        (stereo fallback, the original behaviour); `ext` a mix spec -> a
        WAVEFORMATEXTENSIBLE float32 request with the wire's own channel mask
        and subformat. Raises on failure."""
        wfx = WAVEFORMATEX()
        wfx.wFormatTag = WAVE_FORMAT_IEEE_FLOAT
        wfx.nChannels = channels
        wfx.nSamplesPerSec = self.samplerate
        wfx.wBitsPerSample = 32
        wfx.nBlockAlign = channels * 4
        wfx.nAvgBytesPerSec = self.samplerate * wfx.nBlockAlign
        wfx.cbSize = 0
        self._block_align = wfx.nBlockAlign

        wfx_ptr = ctypes.cast(ctypes.byref(wfx), POINTER(_PycawWAVEFORMATEX))
        if ext is not None:
            extensible = WAVEFORMATEXTENSIBLE()
            extensible.wFormatTag = WAVE_FORMAT_EXTENSIBLE
            extensible.nChannels = channels
            extensible.nSamplesPerSec = self.samplerate
            extensible.wBitsPerSample = 32
            extensible.nBlockAlign = channels * 4
            extensible.nAvgBytesPerSec = self.samplerate * extensible.nBlockAlign
            extensible.cbSize = ctypes.sizeof(WAVEFORMATEXTENSIBLE) - ctypes.sizeof(WAVEFORMATEX)
            extensible.wValidBitsPerSample = 32
            extensible.dwChannelMask = ext["mask"]
            extensible.SubFormat = (wintypes.BYTE * 16).from_buffer_copy(ext["subformat"] or IEEE_FLOAT_SUBFORMAT)
            self._block_align = extensible.nBlockAlign
            wfx_ptr = ctypes.cast(ctypes.byref(extensible), POINTER(_PycawWAVEFORMATEX))

        # Event-driven shared mode: both durations MUST be 0. The Initialize
        # COMMETHOD expects pycaw's WAVEFORMATEX pointer type, so cast our
        # (correctly sized) struct to it.
        client.Initialize(
            AUDCLNT_SHAREMODE_SHARED,
            AUDCLNT_STREAMFLAGS_LOOPBACK
            | AUDCLNT_STREAMFLAGS_EVENTCALLBACK
            | AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM
            | AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY,
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


# ── Mix-format resolution for process-loopback capture ─────────────────────
def _spec_from_wave_format(pwfx) -> dict:
    """Extract a {"channels", "mask", "subformat"} spec from a GetMixFormat
    buffer. The channel count lives at byte offset 2 of the WAVEFORMATEX
    header; channel mask and subformat only exist on extensible formats
    (cbSize >= 22, starting at bytes 20 and 24 of the buffer)."""
    wfx = pwfx.contents
    mask, subformat = 0, None
    if wfx.cbSize >= 22:
        base = ctypes.addressof(wfx)
        mask = int(ctypes.cast(base + 20, POINTER(wintypes.DWORD))[0])
        subformat = bytes((wintypes.BYTE * 16).from_address(base + 24))
    return {"channels": int(wfx.nChannels), "mask": mask, "subformat": subformat}


def _endpoint_mix_spec(dev):
    """Mix-format spec of a render endpoint, read off an IAudioClient
    activated on the endpoint itself. None on failure."""
    import comtypes

    pwfx = None
    try:
        client = dev.Activate(IAudioClient._iid_, comtypes.CLSCTX_ALL, None).QueryInterface(IAudioClient)
        pwfx = client.GetMixFormat()
        return _spec_from_wave_format(pwfx)
    except Exception as exc:  # noqa: BLE001 - endpoint probing is best effort.
        logger.warning("endpoint mix format query failed: %s", exc)
        return None
    finally:
        if pwfx:
            _ole32.CoTaskMemFree(pwfx)


def _session_endpoint_mix_spec(pid: int):
    """Mix-format spec of the render endpoint that the target process's audio
    session plays to. None when no session matches.

    The process-loopback virtual client cannot answer GetMixFormat itself, but
    the endpoint it renders to can - so find that endpoint by session and ask
    it directly. Mirrors the scan in _sessions_all_render_devices; first match
    wins when a process holds sessions on several endpoints."""
    import comtypes
    from pycaw.api.audiopolicy import IAudioSessionControl2, IAudioSessionManager2
    from pycaw.api.mmdeviceapi import IMMDeviceEnumerator
    from pycaw.constants import DEVICE_STATE, CLSID_MMDeviceEnumerator, EDataFlow

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
        except Exception:  # noqa: BLE001 - endpoint enumeration is best effort.
            continue  # some endpoints refuse a session manager - skip them
        for j in range(session_enum.GetCount()):
            ctl = session_enum.GetSession(j)
            if ctl is None:
                continue
            try:
                ctl2 = ctl.QueryInterface(IAudioSessionControl2)
                session_pid = int(ctl2.GetProcessId())
            except Exception:  # noqa: BLE001 - session metadata is optional.
                continue
            if session_pid != int(pid):
                continue
            spec = _endpoint_mix_spec(dev)
            if spec is not None:
                return spec
    return None


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


def find_process_pid(process_name: str) -> int | None:
    """Find the PID of a running process by image name (case-insensitive,
    tolerating a missing `.exe`). None when it is not running or the snapshot
    fails.

    This is the primary target resolver. Dropdown names round-trip to image
    names (list_audio_programs strips the .exe; this re-appends it), so the
    process table alone resolves every entry - an audio session is not
    required. Pure ctypes, safe on the Qt GUI thread, unlike the session
    enumeration above which needs comtypes COM."""
    if not process_name:
        return None
    if not process_name:
        return None
    image = process_name.lower()
    if not image.endswith(".exe"):
        image += ".exe"
    snapshot = _kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        found = _kernel32.Process32FirstW(snapshot, byref(entry))
        while found:
            if entry.szExeFile.lower() == image:
                return int(entry.th32ProcessID)
            found = _kernel32.Process32NextW(snapshot, byref(entry))
    finally:
        _kernel32.CloseHandle(snapshot)
    return None


def is_process_alive(pid: int) -> bool:
    """True while a process with `pid` exists. A failed handle or a collected
    exit code counts as dead - callers stop capture when this goes False."""
    handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return False
    try:
        exit_code = wintypes.DWORD()
        if not _kernel32.GetExitCodeProcess(handle, byref(exit_code)):
            return False
        return exit_code.value == STILL_ACTIVE
    finally:
        _kernel32.CloseHandle(handle)
