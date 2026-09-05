import multiprocessing as mp
import os
import threading
import time

import numpy as np
from PyQt6.QtCore import QThread, pyqtSignal

from app_logging import LOG_DEBUG_ENV, get_logger
from direction import band_rms, stereo_angle, surround_angle

logger = get_logger("audio_capture")

# Keep the capture cadence in one place. 960 frames at 48 kHz gives the radar a
# roughly 20 ms analysis window without changing the capture/DSP threading model.
AUDIO_BLOCK_FRAMES = 960


class AudioCaptureThread(QThread):
    audio_data_signal = pyqtSignal(float, float)
    device_info_signal = pyqtSignal(str, int)
    # Emitted only after the capture source has been opened and a first block
    # has been read.  The GUI uses this to end the asynchronous Start phase.
    ready_signal = pyqtSignal()
    # User-facing terminal failure; low-level details stay in worker stderr.
    error_signal = pyqtSignal(str)
    # Human-readable capture problems (device gone, no loopback, fallbacks).
    # The app forwards these to the dashboard status line so failures are
    # visible to the user, not just printed to a console nobody sees.
    status_signal = pyqtSignal(str)

    def __init__(
        self,
        sensitivity=0.005,
        gain=1.0,
        freq_low=20,
        freq_high=20000,
        max_amplitude=1.0,
        target_pid=None,
        target_name=None,
        mono_enabled=False,
        mono_device=None,
        _manager=True,
        shared_params=None,
        metrics_enabled=None,
    ):
        super().__init__()
        self._manager = _manager
        self.sensitivity = sensitivity
        self.gain = gain
        self.freq_low = freq_low
        self.freq_high = freq_high
        self.max_amplitude = max_amplitude  # ignore sounds louder than this (1.0 = no limit)
        self.running = True
        self.samplerate = 48000
        # When target_pid is set, capture only that program (and its children)
        # via WASAPI process loopback. None = whole-system loopback (soundcard).
        self.target_pid = target_pid
        self.target_name = target_name
        # Mono output: when enabled, the raw (unfiltered) captured audio is also
        # summed to mono and played to `mono_device` for single-sided listeners.
        # Read at thread start (like target); toggle via the app before Start.
        self.mono_enabled = bool(mono_enabled)
        self.mono_device = mono_device or None
        self._mono = None
        self._process = None
        self._recv_conn = None
        self._audio_mailbox_lock = threading.Lock()
        self._latest_audio = None
        self._performance_metrics = {
            "pipe_audio_received": 0,
            "mailbox_published": 0,
            "mailbox_overwritten": 0,
            "mailbox_consumed": 0,
            "max_mailbox_age_ms": 0.0,
        }
        self._metrics_enabled = (
            os.environ.get(LOG_DEBUG_ENV) == "1" if metrics_enabled is None else bool(metrics_enabled)
        )
        self._shared_params = shared_params or mp.get_context("spawn").Array(
            "d", [sensitivity, gain, freq_low, freq_high, max_amplitude]
        )

    def publish_latest_audio(self, angle, intensity):
        """Replace the pending GUI sample instead of queueing stale audio."""
        published_ns = time.monotonic_ns() if self._metrics_enabled else None
        sample = (float(angle), float(intensity), published_ns)
        with self._audio_mailbox_lock:
            if self._metrics_enabled:
                self._performance_metrics["mailbox_published"] += 1
                if self._latest_audio is not None:
                    self._performance_metrics["mailbox_overwritten"] += 1
            self._latest_audio = sample

    def take_latest_audio(self):
        """Atomically consume the newest pending sample, if one exists."""
        with self._audio_mailbox_lock:
            sample = self._latest_audio
            self._latest_audio = None
            if sample is None:
                return None
            if self._metrics_enabled:
                self._performance_metrics["mailbox_consumed"] += 1
                age_ms = (time.monotonic_ns() - sample[2]) / 1_000_000
                self._performance_metrics["max_mailbox_age_ms"] = max(
                    self._performance_metrics["max_mailbox_age_ms"], age_ms
                )
        return sample[0], sample[1]

    def clear_latest_audio(self):
        with self._audio_mailbox_lock:
            self._latest_audio = None

    def performance_metrics(self):
        with self._audio_mailbox_lock:
            metrics = dict(self._performance_metrics)
            metrics["mailbox_pending"] = int(self._latest_audio is not None)
        return metrics

    def set_target(self, pid, name=None):
        """Choose the capture source. Only takes effect before the thread starts."""
        self.target_pid = pid
        self.target_name = name

    def set_mono(self, enabled, device=None):
        """Enable/disable the mono down-mix output and pick its playback device.
        Only takes effect before the thread starts (the thread is recreated on
        each Start, so the app re-applies this in start_radar)."""
        self.mono_enabled = bool(enabled)
        self.mono_device = device or None

    def set_sensitivity(self, sensitivity):
        self.sensitivity = sensitivity
        self._shared_params[0] = sensitivity

    def set_gain(self, gain):
        self.gain = gain
        self._shared_params[1] = gain

    def set_freq_range(self, low, high):
        self.freq_low = low
        self.freq_high = high
        self._shared_params[2] = low
        self._shared_params[3] = high

    def set_max_amplitude(self, max_amp):
        self.max_amplitude = max_amp
        self._shared_params[4] = max_amp

    def _user_failure_message(self):
        label = self.target_name or (f"PID {self.target_pid}" if self.target_pid else "system audio")
        return f"Capture of {label} stopped unexpectedly - restart the radar"

    def run(self):
        logger.debug(
            "capture thread run entered manager=%s target_pid=%s target_name=%r",
            self._manager,
            self.target_pid,
            self.target_name,
        )
        if self._manager:
            self._run_manager()
            logger.debug("capture thread run returned (manager)")
            return
        # Per-app capture takes the process-loopback path; otherwise capture the
        # whole system mix the way we always have.
        self._start_mono()
        try:
            if self.target_pid:
                self._run_process_loopback()
            else:
                self._run_system_loopback()
        finally:
            self._stop_mono()
            logger.debug("capture thread run returned (worker)")

    def _run_manager(self):
        logger.debug("capture manager starting worker process")
        config = {
            "sensitivity": self.sensitivity,
            "gain": self.gain,
            "freq_low": self.freq_low,
            "freq_high": self.freq_high,
            "max_amplitude": self.max_amplitude,
            "target_pid": self.target_pid,
            "target_name": self.target_name,
            "mono_enabled": self.mono_enabled,
            "mono_device": self.mono_device,
            "shared_params": self._shared_params,
        }
        ctx = mp.get_context("spawn")
        recv_conn, send_conn = ctx.Pipe(duplex=False)
        proc = ctx.Process(target=_capture_worker_entry, args=(send_conn, config), daemon=True)
        self._process = proc
        self._recv_conn = recv_conn
        try:
            proc.start()
        except Exception:
            logger.exception("Audio helper start failed")
            self.error_signal.emit(self._user_failure_message())
            recv_conn.close()
            send_conn.close()
            self._process = None
            self._recv_conn = None
            return
        logger.info("capture worker process started pid=%s", proc.pid)
        send_conn.close()
        received_error = False
        received_ready = False
        try:
            done = False
            while self.running and not done:
                if recv_conn.poll(0.1):
                    # Drain the currently available batch. Audio messages replace
                    # one bounded mailbox value; control messages remain ordered.
                    while True:
                        kind = self._handle_manager_message(recv_conn.recv())
                        if kind == "error":
                            received_error = True
                        elif kind == "ready":
                            received_ready = True
                        elif kind == "done":
                            done = True
                            break
                        if not recv_conn.poll():
                            break
                elif not proc.is_alive():
                    break
        except EOFError, OSError:
            pass
        finally:
            logger.debug(
                "capture manager cleaning up running=%s worker_alive=%s received_ready=%s received_error=%s",
                self.running,
                proc.is_alive(),
                received_ready,
                received_error,
            )
            if self.running and not received_ready and not received_error:
                label = self.target_name or "system audio"
                self.error_signal.emit(f"Capture of {label} stopped unexpectedly - restart the radar")
            if proc.is_alive():
                proc.terminate()
            proc.join(timeout=1.0)
            if proc.is_alive():
                logger.warning("capture worker ignored terminate; forcing kill pid=%s", proc.pid)
                proc.kill()
                proc.join(timeout=1.0)
            if proc.is_alive():
                logger.error("capture worker still alive after kill pid=%s", proc.pid)
            recv_conn.close()
            self._process = None
            self._recv_conn = None
            if self._metrics_enabled:
                logger.debug("capture performance metrics=%s", self.performance_metrics())
            logger.debug("capture manager cleanup complete")

    def _handle_manager_message(self, message):
        """Dispatch one worker message and return its kind for loop control."""
        kind, *payload = message
        if kind == "audio":
            if self._metrics_enabled:
                with self._audio_mailbox_lock:
                    self._performance_metrics["pipe_audio_received"] += 1
            self.publish_latest_audio(payload[0], payload[1])
        elif kind == "device":
            self.device_info_signal.emit(str(payload[0]), int(payload[1]))
        elif kind == "status":
            self.status_signal.emit(str(payload[0]))
        elif kind == "error":
            self.error_signal.emit(str(payload[0]))
        elif kind == "ready":
            self.ready_signal.emit()
        return kind

    # ── Mono down-mix output ───────────────────────────────────────────
    def _start_mono(self):
        if not self.mono_enabled:
            return
        try:
            from mono_output import MonoMixThread

            self._mono = MonoMixThread(device_name=self.mono_device, samplerate=self.samplerate)
            self._mono.failed.connect(lambda msg: logger.error("Mono output error: %s", msg))
            self._mono.start()
            logger.info("Mono output started device=%s", self.mono_device or "default device")
        except Exception as e:  # noqa: BLE001 - optional mono output must degrade gracefully.
            logger.warning("Mono output unavailable; continuing without it: %s", e)
            self._mono = None

    def _feed_mono(self, data):
        """Send the RAW (pre-bandpass) chunk to the mono player so the user hears
        the full game audio, not just the filtered footstep band."""
        if self._mono is not None:
            self._mono.feed(data)

    def _stop_mono(self):
        if self._mono is not None:
            try:
                if not self._mono.stop():
                    return
            except Exception:
                logger.exception("Mono output stop failed")
            self._mono = None

    def _run_process_loopback(self):
        """Capture only the selected program; never fall back to system audio."""
        try:
            from process_loopback import ProcessLoopbackCapture
        except Exception as e:  # noqa: BLE001 - optional process loopback may be unavailable.
            message = self._user_failure_message()
            logger.error("Process loopback unavailable: %s", e)
            self.error_signal.emit(message)
            return

        cap = ProcessLoopbackCapture(self.target_pid, samplerate=self.samplerate, channels=2)
        try:
            cap.start()
        except Exception:
            message = self._user_failure_message()
            logger.exception("Process loopback failed")
            self.error_signal.emit(message)
            return

        label = self.target_name or f"PID {self.target_pid}"
        logger.info("Capturing app audio: %s (per-app, Stereo L/R)", label)
        self.device_info_signal.emit(f"{label} (per-app)", 2)
        try:
            first_data = cap.read(AUDIO_BLOCK_FRAMES)
            self.ready_signal.emit()
            self._feed_mono(first_data)
            self._process_chunk(first_data, use_surround=False)
            while self.running:
                data = cap.read(AUDIO_BLOCK_FRAMES)
                self._feed_mono(data)
                self._process_chunk(data, use_surround=False)
        except Exception:
            logger.exception("Process loopback capture error")
            if self.running:
                message = self._user_failure_message()
                self.error_signal.emit(message)
        finally:
            cap.close()

    def _run_system_loopback(self):
        try:
            # Import soundcard only inside the isolated worker.  Importing it in
            # the GUI process initializes COM as MTA before Qt can OleInitialize.
            import soundcard as sc

            mics = sc.all_microphones(include_loopback=True)
            loopbacks = [m for m in mics if m.isloopback]

            if not loopbacks:
                logger.error("No loopback device found")
                message = self._user_failure_message()
                self.error_signal.emit(message)
                return

            # Pick device by priority
            device = None
            try:
                default_name = sc.default_speaker().name
                for lb in loopbacks:
                    if default_name in lb.name:
                        device = lb
                        break
            except Exception as exc:  # noqa: BLE001 - device enumeration is best effort.
                logger.debug("default speaker lookup failed: %s", exc)

            if not device:
                for lb in loopbacks:
                    if "Microphone" not in lb.name:
                        device = lb
                        break

            if not device:
                device = loopbacks[0]

            logger.info("Using loopback device: %s", device.name)
            self._capture_loop(device)

        except Exception:
            logger.exception("Error in audio capture")
            if self.running:
                message = self._user_failure_message()
                self.error_signal.emit(message)

    def _capture_loop(self, device):
        try:
            with device.recorder(samplerate=self.samplerate) as mic:
                first_data = mic.record(numframes=AUDIO_BLOCK_FRAMES)
                raw_channels = first_data.shape[1]

                use_surround = False
                if raw_channels >= 6:
                    surround_max = max(
                        float(np.max(np.abs(first_data[:, ch]))) for ch in range(2, min(raw_channels, 6))
                    )
                    if surround_max > 0.0001:
                        use_surround = True

                effective = raw_channels if use_surround else min(raw_channels, 2)
                mode = "360° Surround" if use_surround else "Stereo L/R"
                logger.info("Channels: %s | Mode: %s", raw_channels, mode)
                self.device_info_signal.emit(device.name, effective)
                self.ready_signal.emit()

                self._feed_mono(first_data)
                self._process_chunk(first_data, use_surround)

                while self.running:
                    data = mic.record(numframes=AUDIO_BLOCK_FRAMES)
                    self._feed_mono(data)
                    self._process_chunk(data, use_surround)

        except Exception:
            logger.exception("Device '%s' failed", device.name)
            if self.running:
                message = self._user_failure_message()
                self.error_signal.emit(message)

    def _process_chunk(self, data, use_surround):
        self.sensitivity = float(self._shared_params[0])
        self.gain = float(self._shared_params[1])
        self.freq_low = int(self._shared_params[2])
        self.freq_high = int(self._shared_params[3])
        self.max_amplitude = float(self._shared_params[4])
        # Band-limited per-channel RMS (Hann-windowed FFT + Parseval; the
        # direction math only ever needs levels, never a filtered waveform).
        rms = band_rms(data, self.samplerate, self.freq_low, self.freq_high) * self.gain

        angle_deg = 0.0

        if use_surround and data.shape[1] >= 6:
            fl, fr, c = float(rms[0]), float(rms[1]), float(rms[2])
            rl, rr = float(rms[4]), float(rms[5])
            intensity = max(fl, fr, c, rl, rr)
            if intensity > self.sensitivity and intensity < self.max_amplitude:
                angle_deg = surround_angle(fl, fr, c, rl, rr)

        elif data.shape[1] >= 2:
            left_rms, right_rms = float(rms[0]), float(rms[1])
            intensity = max(left_rms, right_rms)
            if intensity > self.sensitivity and intensity < self.max_amplitude:
                angle_deg = stereo_angle(left_rms, right_rms)
        else:
            intensity = float(rms[0])

        if intensity > self.sensitivity and intensity < self.max_amplitude:
            self.audio_data_signal.emit(float(angle_deg), float(intensity))

    def request_stop(self):
        """Stop the isolated worker without waiting on a driver call."""
        proc = self._process
        logger.debug(
            "capture stop requested running=%s worker_pid=%s worker_alive=%s",
            self.running,
            getattr(proc, "pid", None),
            proc is not None and proc.is_alive(),
        )
        self.running = False
        self.clear_latest_audio()
        if proc is not None and proc.is_alive():
            proc.terminate()
            logger.debug("capture worker terminate sent pid=%s", proc.pid)

    def stop(self):
        """Backward-compatible alias for a non-blocking stop request."""
        self.request_stop()


def _capture_worker_entry(send_conn, config):
    """Run all soundcard/WASAPI work outside the GUI process."""
    capture = AudioCaptureThread(**config, _manager=False)

    def send(message):
        try:
            send_conn.send(message)
        except BrokenPipeError, EOFError, OSError:
            capture.running = False

    capture.audio_data_signal.connect(lambda a, i: send(("audio", a, i)))
    capture.device_info_signal.connect(lambda n, c: send(("device", n, c)))
    capture.status_signal.connect(lambda msg: send(("status", msg)))
    capture.error_signal.connect(lambda msg: send(("error", msg)))
    capture.ready_signal.connect(lambda: send(("ready",)))
    try:
        capture.run()
    except Exception:
        logger.exception("Audio capture worker failed")
        send(("error", capture._user_failure_message()))
    finally:
        send(("done",))
        send_conn.close()
