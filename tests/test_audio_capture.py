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


def capture_with(*, sensitivity=0.005, gain=1.0, max_amplitude=1.0, mode="surround"):
    # Slot 5 is the mapping choice for >=6ch wires, mirroring the thread's own
    # default (1.0 = surround).
    params = [sensitivity, gain, 100, 900, max_amplitude, 1.0 if mode == "surround" else 0.0]
    return AudioCaptureThread(_manager=False, shared_params=params)


def emitted_audio(capture, data):
    received = []
    capture.audio_data_signal.connect(lambda angle, level: received.append((angle, level)))
    capture._process_chunk(data, data.shape[1])
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


# ── multichannel layouts: 5.1 / 7.1 speaker azimuths ────────────────────


def surround_block(channel_amps, channels=8):
    time = np.arange(N) / SR
    tone = np.sin(2 * np.pi * 300 * time)
    data = np.zeros((N, channels))
    for ch, amp in channel_amps.items():
        data[:, ch] = amp * tone
    return data


def test_surround_side_right_keeps_its_own_side_azimuth():
    # 7.1 side-R (index 7) alone stays at its own +90 side azimuth - the
    # per-layout geometry fix (it used to fold into the rear pair and draw
    # rear-right, on top of BR).
    capture = capture_with()
    received = []
    capture.audio_data_signal.connect(lambda angle, level: received.append((angle, level)))
    capture._process_chunk(surround_block({7: 0.4}), 8)

    assert len(received) == 1
    angle, level = received[0]
    assert level == pytest.approx(0.4 / math.sqrt(2), rel=0.05)
    assert angle == pytest.approx(90.0, abs=1e-6)


def test_surround_lfe_alone_never_emits():
    # LFE is not a radar level; a block that only exercises it is silent.
    capture = capture_with()
    received = []
    capture.audio_data_signal.connect(lambda angle, level: received.append((angle, level)))
    capture._process_chunk(surround_block({3: 1.0}), 8)

    assert received == []


def test_surround_default_places_front_only_in_front_quadrant():
    # 8ch, only FL/FR audible, surround mode (the session default): the full
    # 360 geometry keeps the blip in the front quadrants, not hard +-90.
    fl_strong = emitted_audio(capture_with(), surround_block({0: 0.4, 1: 0.1}))
    fr_strong = emitted_audio(capture_with(), surround_block({0: 0.1, 1: 0.4}))

    assert len(fl_strong) == 1 and len(fr_strong) == 1
    assert -90.0 < fl_strong[0][0] < 0.0
    assert 0.0 < fr_strong[0][0] < 90.0


def test_stereo_mode_on_8ch_wire_matches_2ch():
    # In stereo mode the FL/FR balance drives the angle exactly as on a 2ch
    # wire: same pan, same blip.
    eight_ch = emitted_audio(capture_with(mode="stereo"), surround_block({0: 0.1, 1: 0.4}))
    stereo = emitted_audio(capture_with(mode="stereo"), stereo_tone(0.1, 0.4))

    assert len(eight_ch) == 1 and len(stereo) == 1
    assert eight_ch[0][0] == pytest.approx(stereo[0][0], rel=1e-6)
    assert eight_ch[0][1] == pytest.approx(stereo[0][1], rel=1e-6)


def test_stereo_mode_folds_centre_to_front():
    # Stereo mode downmixes the whole wire: C alone still emits, split evenly
    # into L/R at 0.707, so it draws front-centre - the old FL/FR-only reading
    # dropped centre dialog on multichannel mixes.
    received = emitted_audio(capture_with(mode="stereo"), surround_block({2: 0.7}))

    assert len(received) == 1
    angle, level = received[0]
    assert angle == pytest.approx(0.0, abs=1e-6)
    # 0.7 tone -> 0.7/sqrt(2) RMS -> 0.707 into each side = amp * 0.5.
    assert level == pytest.approx(0.7 * 0.5, rel=0.05)


def test_stereo_mode_folds_rear_to_its_own_side():
    # Rear energy stays audible in stereo mode, folded onto the L/R axis: BL
    # alone draws hard left, BR alone hard right (each 0.707 into its side).
    left = emitted_audio(capture_with(mode="stereo"), surround_block({4: 0.7}))
    right = emitted_audio(capture_with(mode="stereo"), surround_block({5: 0.7}))

    assert len(left) == 1 and len(right) == 1
    assert left[0][0] == pytest.approx(-90.0, rel=1e-4)
    assert right[0][0] == pytest.approx(90.0, rel=1e-4)
    assert left[0][1] == pytest.approx(0.7 * 0.5, rel=0.05)
    assert right[0][1] == pytest.approx(0.7 * 0.5, rel=0.05)


def test_stereo_mode_lfe_alone_stays_silent():
    # LFE never participates in a level, regardless of the mapping.
    assert emitted_audio(capture_with(mode="stereo"), surround_block({3: 1.0})) == []


def test_surround_back_right_keeps_its_own_back_azimuth():
    # 7.1 back-R (index 5) alone: +135, not collapsed onto the side pair.
    received = emitted_audio(capture_with(), surround_block({5: 0.4}))
    assert len(received) == 1
    assert received[0][0] == pytest.approx(135.0, abs=1e-6)


def test_surround_side_left_azimuth():
    received = emitted_audio(capture_with(), surround_block({6: 0.4}))
    assert len(received) == 1
    assert received[0][0] == pytest.approx(-90.0, abs=1e-6)


def test_surround_5_1_rear_azimuth():
    # A 6-column wire picks the 5.1 layout: surround-R sits at +110, not the
    # 7.1 back angle.
    received = emitted_audio(capture_with(), surround_block({5: 0.4}, channels=6))
    assert len(received) == 1
    assert received[0][0] == pytest.approx(110.0, abs=1e-6)


def test_surround_6ch_front_only_in_front_quadrant():
    received = emitted_audio(capture_with(), surround_block({0: 0.4, 1: 0.1}, channels=6))
    assert len(received) == 1
    assert -90.0 < received[0][0] < 0.0


def test_surround_equal_back_pair_is_directly_behind():
    # Equal 7.1 backs cancel left/right and point straight back (+180 and
    # -180 draw identically, hence the wrap-safe form).
    received = emitted_audio(capture_with(), surround_block({4: 0.4, 5: 0.4}))
    assert len(received) == 1
    assert abs(abs(received[0][0]) - 180.0) < 1e-3
