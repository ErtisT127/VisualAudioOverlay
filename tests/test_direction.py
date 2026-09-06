"""Unit tests for direction.py - the pure math behind the radar.

These run without any audio hardware, Qt, or Windows APIs (plain numpy), so
they stay fast and deterministic in CI.
"""

import math

import numpy as np
import pytest

from direction import (
    SURROUND_CONTENT_FLOOR,
    angle_diff,
    band_rms,
    detect_surround_mode,
    fold_surround,
    stereo_angle,
    surround_angle,
)

SR = 48000
N = 960  # one 20ms capture chunk, same as audio_capture.py


def sine(freq, amp=1.0, n=N, sr=SR):
    t = np.arange(n) / sr
    return amp * np.sin(2 * np.pi * freq * t)


# ── band_rms ───────────────────────────────────────────────────────────


def test_full_range_equals_plain_rms():
    rng = np.random.default_rng(0)
    data = rng.standard_normal((N, 2))
    expected = np.sqrt(np.mean(data**2, axis=0))
    np.testing.assert_allclose(band_rms(data, SR, 20, 20000), expected)


def test_in_band_sine_keeps_its_rms():
    # 440 Hz sine, amplitude 0.5 -> RMS = 0.5/sqrt(2)
    data = sine(440, amp=0.5)[:, None]
    rms = band_rms(data, SR, 100, 900)
    assert rms[0] == pytest.approx(0.5 / math.sqrt(2), rel=0.05)


def test_out_of_band_sine_is_rejected():
    data = sine(4000, amp=0.5)[:, None]
    rms = band_rms(data, SR, 100, 900)
    assert rms[0] < 0.01


def test_band_separates_mixed_signal():
    # Footstep-band tone plus loud, well-separated high-frequency sound: the
    # band RMS should reflect the tone rather than the out-of-band component.
    # A 20ms analysis window has 50Hz FFT bins, so testing a 40Hz signal just
    # below a 100Hz cutoff would assert away normal spectral leakage.
    in_band = sine(300, amp=0.2)
    high_frequency = sine(4000, amp=1.0)
    data = (in_band + high_frequency)[:, None]
    rms = band_rms(data, SR, 100, 900)
    assert rms[0] == pytest.approx(0.2 / math.sqrt(2), rel=0.1)


def test_per_channel_independence():
    data = np.stack([sine(300, amp=0.4), sine(4000, amp=0.4)], axis=1)
    rms = band_rms(data, SR, 100, 900)
    assert rms[0] > 10 * rms[1]


def test_empty_and_silent_chunks_are_silent():
    # Device reconnects can produce an empty chunk; silence must not invent a
    # level or make the FFT path raise before the capture thread can continue.
    assert np.array_equal(band_rms(np.empty((0, 2)), SR, 100, 900), [0.0, 0.0])
    assert np.array_equal(band_rms(np.zeros(N), SR, 100, 900), [0.0])


# ── stereo_angle ───────────────────────────────────────────────────────


def test_stereo_centre_is_zero():
    assert stereo_angle(0.5, 0.5) == pytest.approx(0.0, abs=0.1)


def test_stereo_hard_right_and_left():
    assert stereo_angle(0.0, 0.5) == pytest.approx(90.0, rel=0.01)
    assert stereo_angle(0.5, 0.0) == pytest.approx(-90.0, rel=0.01)


def test_stereo_slight_pan_is_expanded():
    # The 0.3 exponent should push a mild 60/40 imbalance well off centre.
    angle = stereo_angle(0.4, 0.6)
    assert 30.0 < angle < 90.0


def test_stereo_direction_is_independent_of_volume():
    # Capture gain may change, but a source's left/right balance must remain
    # at the same radar angle.
    assert stereo_angle(0.2, 0.5) == pytest.approx(stereo_angle(2.0, 5.0))


def test_silent_stereo_stays_centered():
    assert stereo_angle(0.0, 0.0) == pytest.approx(0.0)


# ── surround_angle ─────────────────────────────────────────────────────


def test_surround_front_is_zero():
    assert surround_angle(1.0, 1.0, 1.0, 0.0, 0.0) == pytest.approx(0.0)


def test_surround_right_is_90():
    assert surround_angle(0.0, 1.0, 0.0, 0.0, 1.0) == pytest.approx(90.0)


def test_surround_left_is_minus_90():
    assert surround_angle(1.0, 0.0, 0.0, 1.0, 0.0) == pytest.approx(-90.0)


def test_surround_rear_is_180():
    assert abs(surround_angle(0.0, 0.0, 0.0, 1.0, 1.0)) == pytest.approx(180.0)


# ── detect_surround_mode ───────────────────────────────────────────────


def test_detect_stereo_device_is_never_surround():
    assert detect_surround_mode([0.0, 0.0], 2) is False
    assert detect_surround_mode([0.5, 0.5], 4) is False  # <6ch ignores content


def test_detect_silent_blocks_keep_waiting():
    assert detect_surround_mode([0.0] * 6, 6) is None
    # Exactly at the floor is still silence, not a decision.
    assert detect_surround_mode([SURROUND_CONTENT_FLOOR] * 6, 6) is None


def test_detect_front_only_content_is_stereo():
    levels = [0.5, 0.4, 0.0, 0.0, 0.0, 0.0]
    assert detect_surround_mode(levels, 6) is False


def test_detect_any_rear_speaker_flips_surround():
    assert detect_surround_mode([0.0, 0.0, 0.5, 0.0, 0.0, 0.0], 6) is True  # centre
    assert detect_surround_mode([0.0, 0.0, 0.0, 0.0, 0.5, 0.0], 6) is True  # rear L
    assert detect_surround_mode([0.0, 0.0, 0.0, 0.5, 0.0, 0.0], 6) is True  # LFE
    assert detect_surround_mode([0.0, 0.0, 0.0, 0.0, 0.0, 0.5], 6) is True  # rear R


def test_detect_eight_channel_side_speakers_flip_surround():
    # 7.1: side channels at indices 6/7 are surround evidence too.
    assert detect_surround_mode([0.0] * 6 + [0.5, 0.0], 8) is True
    assert detect_surround_mode([0.0] * 6 + [0.0, 0.5], 8) is True


def test_detect_ignores_levels_beyond_channel_count():
    # Levels may outlive the declared channel count (caller truncates); a
    # probe that read 8 channels while the device has 6 must not see index 6.
    levels = [0.5, 0.5, 0.0, 0.0, 0.0, 0.0, 0.9, 0.9]
    assert detect_surround_mode(levels, 6) is False


def test_detect_floor_is_configurable():
    assert detect_surround_mode([0.0005] * 6, 6, floor=0.0001) is True


# ── fold_surround ──────────────────────────────────────────────────────


def test_fold_six_channels_maps_directly():
    fl, fr, c, rl, rr = fold_surround(np.array([0.5, 0.4, 0.3, 9.9, 0.2, 0.1]))
    assert (fl, fr, c, rl, rr) == (0.5, 0.4, 0.3, 0.2, 0.1)


def test_fold_drops_lfe():
    levels = np.array([0.0, 0.0, 0.0, 1.0, 0.0, 0.0])
    assert tuple(fold_surround(levels)) == (0.0, 0.0, 0.0, 0.0, 0.0)


def test_fold_eight_channels_folds_sides_into_rear():
    # 7.1: indices 6/7 (side L/R) add into rl/rr; LFE stays out.
    levels = np.array([0.1, 0.2, 0.3, 9.9, 0.4, 0.5, 0.6, 0.7])
    fl, fr, c, rl, rr = fold_surround(levels)
    assert (fl, fr, c) == (0.1, 0.2, 0.3)
    assert rl == 0.4 + 0.6
    assert rr == 0.5 + 0.7


def test_fold_side_only_content_reaches_the_rear_pair():
    levels = np.array([0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.5, 0.5])
    fl, fr, c, rl, rr = fold_surround(levels)
    assert (fl, fr, c) == (0.0, 0.0, 0.0)
    assert (rl, rr) == (0.5, 0.5)


# ── angle_diff ─────────────────────────────────────────────────────────


def test_angle_diff_simple():
    assert angle_diff(10.0, 30.0) == pytest.approx(20.0)
    assert angle_diff(30.0, 10.0) == pytest.approx(20.0)


def test_angle_diff_wraps_at_180():
    # A sound directly behind the player: -179 and +179 are 2 degrees apart,
    # not 358 (the bug that split rear blips in two).
    assert angle_diff(179.0, -179.0) == pytest.approx(2.0)
    assert angle_diff(-170.0, 170.0) == pytest.approx(20.0)


def test_angle_diff_opposites():
    assert angle_diff(90.0, -90.0) == pytest.approx(180.0)
