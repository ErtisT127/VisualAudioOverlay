"""Unit tests for direction.py - the pure math behind the radar.

These run without any audio hardware, Qt, or Windows APIs (plain numpy), so
they stay fast and deterministic in CI.
"""

import math

import numpy as np
import pytest

from direction import (
    angle_diff,
    band_rms,
    stereo_angle,
    stereo_downmix,
    surround_angle,
    surround_intensity,
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


# ── surround_angle / surround_intensity (per-layout speaker azimuths) ───


def single_speaker(index, value, width):
    levels = np.zeros(width)
    levels[index] = value
    return levels


def test_5_1_each_speaker_at_its_azimuth():
    # 5.1: fronts at +-30, surrounds at +-110 (ITU-R BS.775). Single-tone
    # placement is exact - no cancellation with only one active speaker.
    for index, azimuth in ((0, -30.0), (1, 30.0), (2, 0.0), (4, -110.0), (5, 110.0)):
        assert surround_angle(single_speaker(index, 1.0, 6), 6) == pytest.approx(azimuth, abs=1e-9)


def test_7_1_each_speaker_at_its_azimuth():
    for index, azimuth in (
        (0, -30.0),
        (1, 30.0),
        (2, 0.0),
        (4, -135.0),
        (5, 135.0),
        (6, -90.0),
        (7, 90.0),
    ):
        assert surround_angle(single_speaker(index, 1.0, 8), 8) == pytest.approx(azimuth, abs=1e-9)


def test_side_and_back_pairs_are_distinct_on_8ch():
    # The core 7.1 fix: a side speaker keeps its own +-90 azimuth instead of
    # folding into the back pair at +-135, so SL/BL and SR/BR no longer share
    # one radar position.
    assert surround_angle(single_speaker(7, 1.0, 8), 8) == pytest.approx(90.0, abs=1e-9)
    assert surround_angle(single_speaker(5, 1.0, 8), 8) == pytest.approx(135.0, abs=1e-9)


def test_equal_front_pair_is_front():
    levels = np.array([0.5, 0.5, 0.0, 0.0, 0.0, 0.0])
    assert surround_angle(levels, 6) == pytest.approx(0.0, abs=1e-6)


def test_equal_rear_pair_is_directly_behind():
    # 5.1 surrounds and 7.1 backs sit symmetric behind the player, so an equal
    # pair reads straight back. Wrap-safe: +180 and -180 draw identically.
    for levels, count in (
        (np.array([0.0, 0.0, 0.0, 0.0, 1.0, 1.0]), 6),
        (np.array([0.0, 0.0, 0.0, 0.0, 0.5, 0.5, 0.0, 0.0]), 8),
    ):
        angle = surround_angle(levels, count)
        assert abs(abs(angle) - 180.0) < 1e-6


def test_lfe_is_excluded_from_angle_and_intensity():
    for count in (6, 8):
        levels = single_speaker(3, 1.0, count)
        assert surround_angle(levels, count) == pytest.approx(0.0)
        assert surround_intensity(levels, count) == pytest.approx(0.0)


def test_all_silent_is_front():
    levels = np.zeros(8)
    assert surround_angle(levels, 8) == pytest.approx(0.0)
    assert surround_intensity(levels, 8) == pytest.approx(0.0)


def test_intensity_is_max_of_participating():
    # A loud LFE column must not leak into the radar level.
    levels = np.array([0.1, 0.2, 0.3, 9.9, 0.7, 0.9])
    assert surround_intensity(levels, 6) == pytest.approx(0.9)
    assert surround_intensity(single_speaker(6, 0.8, 8), 8) == pytest.approx(0.8)


def test_layout_is_chosen_by_channel_count_not_array_length():
    # An 8-column frame read as a 5.1 wire: its side column (index 6) is not
    # part of that layout, so it must not steer the angle or the level.
    side = single_speaker(6, 1.0, 8)
    assert surround_angle(side, 8) == pytest.approx(-90.0, abs=1e-9)
    assert surround_angle(side, 6) == pytest.approx(0.0)
    assert surround_intensity(side, 6) == pytest.approx(0.0)
    # A >8ch wire (7.1 + height) only reads its base ring.
    tall = single_speaker(12, 1.0, 16)
    assert surround_angle(tall, 16) == pytest.approx(0.0)
    assert surround_intensity(tall, 16) == pytest.approx(0.0)


def test_mixed_channels_blend_continuously_between_speakers():
    # A source panned across real speakers sweeps the ring instead of hopping:
    # equal FL+surround-L on 5.1 points halfway down the left wall...
    left_wall = np.array([1.0, 0.0, 0.0, 0.0, 1.0, 0.0])
    assert surround_angle(left_wall, 6) == pytest.approx(-70.0, abs=1e-9)
    # ...FL biased toward C stays in the front-left quadrant...
    front_biased = np.array([0.8, 0.0, 1.0, 0.0, 0.0, 0.0])
    assert surround_angle(front_biased, 6) == pytest.approx(-13.294686193990028, abs=1e-9)
    # ...and 7.1 content split between back-L and side-L lands between them.
    side_dominant = np.array([0.0, 0.0, 0.0, 0.0, 0.3, 0.0, 0.6, 0.0])
    assert surround_angle(side_dominant, 8) == pytest.approx(-104.63880659517828, abs=1e-9)


# ── stereo_downmix (multichannel -> L/R fold) ──────────────────────────


def test_downmix_passes_front_through():
    # FL/FR leave untouched; the L/R fold equals the original stereo pair.
    assert stereo_downmix(np.array([0.2, 0.3, 0.0, 0.0, 0.0, 0.0]), 6) == pytest.approx((0.2, 0.3))


def test_downmix_splits_centre_equally():
    # Centre-only content reaches both sides at 1/sqrt(2), like the ITU fold.
    q = 1.0 / math.sqrt(2.0)
    assert stereo_downmix(np.array([0.0, 0.0, 1.0, 0.0, 0.0, 0.0]), 6) == pytest.approx((q, q))


def test_downmix_folds_each_rear_speaker_to_its_own_side():
    # 5.1 surrounds and 7.1 backs/sides land on the L/R axis: BL/SL to the
    # left, BR/SR to the right, each at 1/sqrt(2). Equal rears stay centred.
    q = 1.0 / math.sqrt(2.0)
    assert stereo_downmix(np.array([0.0, 0.0, 0.0, 0.0, 1.0, 1.0]), 6) == pytest.approx((q, q))
    assert stereo_downmix(np.array([0.0, 0.0, 0.0, 0.0, 1.0, 0.0]), 6) == pytest.approx((q, 0.0))
    assert stereo_downmix(np.array([0.0, 0.0, 0.0, 0.0, 0.0, 1.0]), 6) == pytest.approx((0.0, q))
    assert stereo_downmix(np.array([0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0]), 8) == pytest.approx((q, 0.0))
    assert stereo_downmix(np.array([0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0]), 8) == pytest.approx((0.0, q))


def test_downmix_excludes_lfe_and_columns_beyond_the_layout():
    # LFE never participates; a 6-column frame read as 7.1 stops at its own
    # columns; a >8ch wire reads its base ring only (mirrors surround_angle).
    assert stereo_downmix(np.array([0.0, 0.0, 0.0, 9.0, 0.0, 0.0]), 6) == pytest.approx((0.0, 0.0))
    assert stereo_downmix(np.array([0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 1.0]), 6) == pytest.approx((0.0, 0.0))
    tall = np.zeros(16)
    tall[12] = 1.0
    assert stereo_downmix(tall, 16) == pytest.approx((0.0, 0.0))


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
