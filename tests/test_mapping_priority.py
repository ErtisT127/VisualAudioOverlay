"""Unit tests for the remembered stereo/surround mapping priority (main.py).

The priority lives in settings.json under "mapping_mode": surround for anyone
who never toggled the dashboard mapping switch, the user's explicit choice
once they have. It is consulted exactly where a wire offers both mappings
(>=6 channels) - never to claim a narrower wire can do surround.
"""

from main import _mapping_for_wire, _normalize_mapping_priority

# ── _normalize_mapping_priority ────────────────────────────────────────────


def test_missing_priority_defaults_to_surround():
    assert _normalize_mapping_priority(None) == "surround"


def test_valid_saved_priority_is_accepted():
    assert _normalize_mapping_priority("surround") == "surround"
    assert _normalize_mapping_priority("stereo") == "stereo"


def test_corrupt_saved_priority_falls_back_to_surround():
    # settings.json is hand-editable; anything unusable keeps the default.
    assert _normalize_mapping_priority("mono") == "surround"
    assert _normalize_mapping_priority("") == "surround"
    assert _normalize_mapping_priority(0) == "surround"
    assert _normalize_mapping_priority(["stereo"]) == "surround"


# ── _mapping_for_wire ──────────────────────────────────────────────────────


def test_wire_with_both_mappings_opens_with_the_priority():
    assert _mapping_for_wire(6, "surround") == "surround"
    assert _mapping_for_wire(8, "stereo") == "stereo"


def test_narrow_wire_is_stereo_even_with_a_surround_priority():
    # A stored surround preference must never claim a 2-channel wire can do
    # full-360 mapping; below 6 channels the choice does not exist.
    assert _mapping_for_wire(2, "surround") == "stereo"
    assert _mapping_for_wire(2, "stereo") == "stereo"
