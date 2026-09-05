"""Hardware-free tests for the capture thread's analysis and level gates."""

import numpy as np

from audio_capture import AudioCaptureThread

SR = 48000
N = 960


def stereo_tone(left_amp: float, right_amp: float) -> np.ndarray:
    time = np.arange(N) / SR
    tone = np.sin(2 * np.pi * 300 * time)
    return np.stack([left_amp * tone, right_amp * tone], axis=1)


def capture_with(*, sensitivity=0.005, gain=1.0, max_amplitude=1.0):
    params = [sensitivity, gain, 100, 900, max_amplitude]
    return AudioCaptureThread(_manager=False, shared_params=params)


def emitted_audio(capture, data):
    received = []
    capture.audio_data_signal.connect(lambda angle, level: received.append((angle, level)))
    capture._process_chunk(data, use_surround=False)
    return received


def test_stereo_chunk_emits_its_direction_and_level():
    received = emitted_audio(capture_with(), stereo_tone(0.1, 0.4))

    assert len(received) == 1
    angle, level = received[0]
    assert angle > 0  # louder right channel appears on the right of the radar
    assert level > 0.2


def test_sensitivity_gate_suppresses_quiet_chunks():
    received = emitted_audio(capture_with(sensitivity=0.1), stereo_tone(0.01, 0.02))

    assert received == []


def test_max_amplitude_gate_suppresses_loud_chunks():
    received = emitted_audio(capture_with(max_amplitude=0.2), stereo_tone(0.1, 0.4))

    assert received == []
