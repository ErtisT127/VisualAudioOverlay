"""Pure direction/level math shared by the capture thread and the overlay.

No Qt, no audio-device imports - everything here is plain numpy/math so it can
be unit-tested on any platform (see tests/test_direction.py) and reused without
dragging in soundcard/COM.
"""

import math
from functools import lru_cache

import numpy as np


@lru_cache(maxsize=32)
def _band_plan(n, samplerate, freq_low, freq_high):
    """Cache frequency-domain metadata; these arrays never change per call."""
    window = np.hanning(n)
    freqs = np.fft.rfftfreq(n, d=1.0 / samplerate)
    in_band = (freqs >= freq_low) & (freqs <= freq_high)

    weights = np.full(len(freqs), 2.0)
    weights[0] = 1.0
    if n % 2 == 0:
        weights[-1] = 1.0

    band_weights = weights * in_band
    window_power = np.mean(window**2)
    for array in (window, band_weights):
        array.setflags(write=False)
    return window, band_weights, window_power


def band_rms(data, samplerate, freq_low=20, freq_high=20000):
    """Per-channel RMS of `data` restricted to [freq_low, freq_high] Hz.

    `data` is (frames, channels) float. Returns a 1-D array of one RMS value
    per channel.

    The band restriction is computed in the frequency domain via Parseval's
    theorem on a Hann-windowed FFT, rather than zeroing bins and inverting
    (the old approach): the window kills the spectral leakage that a raw
    rectangular chunk smears across the band edge, and skipping the inverse
    FFT makes it cheaper. The Hann window's power loss is corrected by
    mean(w^2) so results are comparable to a plain time-domain RMS.
    """
    data = np.asarray(data, dtype=np.float64)
    if data.ndim == 1:
        data = data[:, None]
    n = data.shape[0]
    if n == 0:
        return np.zeros(data.shape[1])

    if freq_low <= 20 and freq_high >= 20000:
        # Full audible range: no filtering needed, plain RMS is exact.
        return np.sqrt(np.mean(data**2, axis=0))

    window, band_weights, window_power = _band_plan(n, samplerate, freq_low, freq_high)
    spec = np.fft.rfft(data * window[:, None], axis=0)

    # Parseval for rfft: sum(x^2) = (|X_0|^2 + 2*sum(|X_k|^2) + |X_nyq|^2) / n.
    band_energy = ((np.abs(spec) ** 2) * band_weights[:, None]).sum(axis=0) / n
    mean_square = band_energy / n / window_power
    return np.sqrt(mean_square)


def stereo_angle(left_rms, right_rms):
    """L/R balance -> angle in degrees, -90 (hard left) .. +90 (hard right).

    The 0.3 exponent expands small balance differences so slightly-panned
    sounds still visibly leave the centre of the radar.
    """
    balance = (right_rms - left_rms) / (right_rms + left_rms + 1e-6)
    sign = 1.0 if balance >= 0 else -1.0
    return sign * (abs(balance) ** 0.3) * 90.0


# Azimuths in degrees, 0 = front, + = right (compass-clockwise), as
# (channel_index, azimuth) pairs in WAVE order (FL FR FC LFE BL BR [SL SR]).
# LFE (index 3) is excluded structurally - it has no entry.
_LAYOUT_5_1 = ((0, -30.0), (1, 30.0), (2, 0.0), (4, -110.0), (5, 110.0))
_LAYOUT_7_1 = ((0, -30.0), (1, 30.0), (2, 0.0), (4, -135.0), (5, 135.0), (6, -90.0), (7, 90.0))


def _layout_for(channel_count):
    """6 -> 5.1 table; >=8 -> 7.1 table applied to the first 8 columns (a
    16ch 7.1+height wire keeps its base ring; a 5.1.2 wire's 8th column is a
    height speaker, not a side - accepted, never exercised). 7ch (never
    observed) falls to the 5.1 table; <6ch is the caller's stereo path."""
    return _LAYOUT_7_1 if channel_count >= 8 else _LAYOUT_5_1


def surround_angle(levels, channel_count):
    """Weighted speaker azimuths -> angle in degrees, 0 = front, + = right.
    atan2(sum(L_i * sin a_i), sum(L_i * cos a_i)) over the layout's non-LFE
    columns. Single-speaker tones land exactly on that speaker's azimuth;
    equal L/R pairs land front; an equal rear pair lands +-180. An exact zero
    vector returns 0.0 - the caller's gate suppresses silent blocks first.
    (A perfectly balanced side pair on 7.1 is a directionless phantom and
    reads front here; libm's sin antisymmetry makes it deterministic.)"""
    sx = sy = 0.0
    for i, az in _layout_for(channel_count):
        if i >= len(levels):
            break
        w = float(levels[i])
        if w == 0.0:
            continue
        r = math.radians(az)
        sx += w * math.sin(r)
        sy += w * math.cos(r)
    return math.degrees(math.atan2(sx, sy))


def surround_intensity(levels, channel_count):
    """Loudest participating (non-LFE) level of the wire's layout."""
    return max(
        (float(levels[i]) for i, _ in _layout_for(channel_count) if i < len(levels)),
        default=0.0,
    )


# ITU BS.775-style coefficient for folding a multichannel mix into stereo:
# every non-front speaker enters at 1/sqrt(2), preserving total energy.
DOWNMIX_Q = 1.0 / math.sqrt(2.0)


def stereo_downmix(levels, channel_count):
    """Fold a >=6-channel wire down to the L/R pair a stereo mix of the same
    programme would carry, so nothing that is audible in surround mode
    disappears in stereo mode. FL/FR pass through unchanged; centre and each
    rear/side speaker contribute at DOWNMIX_Q - centre split across both
    sides, left-azimuth speakers (BL/SL) into the left and right-azimuth
    (BR/SR) into the right. LFE never participates and columns beyond the
    wire's layout are ignored, mirroring surround_angle."""
    levels = np.asarray(levels, dtype=np.float64).ravel()
    left = float(levels[0]) if levels.size else 0.0
    right = float(levels[1]) if levels.size > 1 else 0.0
    for i, azimuth in _layout_for(channel_count):
        if i < 2 or i >= levels.size:  # FL/FR already counted as pass-through
            continue
        w = DOWNMIX_Q * float(levels[i])
        if azimuth == 0.0:  # centre -> both sides
            left += w
            right += w
        elif azimuth < 0.0:  # rear/side-left -> left
            left += w
        else:  # rear/side-right -> right
            right += w
    return left, right


def angle_diff(a, b):
    """Smallest absolute difference between two angles in degrees.

    Wraps at +-180 so e.g. angle_diff(179, -179) == 2 - without this, blips
    for a sound directly behind the player (surround mode) split into two.
    """
    return abs((a - b + 180.0) % 360.0 - 180.0)
