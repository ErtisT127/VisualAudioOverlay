"""Hardware-free tests for the capture thread's analysis and level gates."""

import math

import numpy as np
import pytest

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


# ── 8-channel fold (A2): side speakers reach the rear pair ──────────────


def surround_block(channel_amps):
    time = np.arange(N) / SR
    tone = np.sin(2 * np.pi * 300 * time)
    data = np.zeros((N, 8))
    for ch, amp in channel_amps.items():
        data[:, ch] = amp * tone
    return data


def test_surround_side_right_speaker_emits_rear_right_blip():
    # 7.1 side-R (index 7) alone: rl/rr get it via the fold, so it must emit
    # a blip on the rear-right side of the radar - the A2 regression.
    capture = capture_with()
    received = []
    capture.audio_data_signal.connect(lambda angle, level: received.append((angle, level)))
    capture._process_chunk(surround_block({7: 0.4}), use_surround=True)

    assert len(received) == 1
    angle, level = received[0]
    assert level == pytest.approx(0.4 / math.sqrt(2), rel=0.05)
    assert angle >= 90.0


def test_surround_lfe_alone_never_emits():
    # LFE is not a radar level; a block that only exercises it is silent.
    capture = capture_with()
    received = []
    capture.audio_data_signal.connect(lambda angle, level: received.append((angle, level)))
    capture._process_chunk(surround_block({3: 1.0}), use_surround=True)

    assert received == []
