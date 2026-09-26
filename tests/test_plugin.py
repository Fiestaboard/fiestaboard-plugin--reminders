"""Tests for the Reminders plugin."""

import json
from datetime import datetime, timezone

import pytest
import pytz

from plugins.reminders import MAX_REMINDERS, RemindersPlugin
from src.devices import BoardContext


MANIFEST = json.load(
    open(__file__.rsplit("/tests/", 1)[0] + "/manifest.json", encoding="utf-8")
)

TZ = pytz.timezone("America/Los_Angeles")


def _utc(year, month, day, hour, minute=0):
    """A local (America/Los_Angeles) wall clock time, as UTC."""
    return TZ.localize(datetime(year, month, day, hour, minute)).astimezone(timezone.utc)


def make_plugin(reminders, now=None, **extra):
    """Build a configured plugin whose clock is pinned to ``now``."""
    plugin = RemindersPlugin(MANIFEST)
    config = {
        "enabled": True,
        "timezone": "America/Los_Angeles",
        "reminders": reminders,
        "refresh_seconds": 60,
    }
    config.update(extra)
    plugin.config = config
    if now is not None:
        plugin._now = lambda: now
    return plugin


VITAMINS = {"name": "Take vitamins", "schedule": "daily", "time": "08:00", "color": "red"}
FERNS = {"name": "Water ferns", "schedule": "weekly", "weekday": "Wednesday", "time": "09:30", "color": "green"}
CAT = {
    "name": "Feed the cat",
    "schedule": "every_n_days",
    "interval_days": 3,
    "anchor_date": "2026-09-14",
    "time": "18:00",
    "color": "blue",
}
WORK = {"name": "Standup", "schedule": "weekdays", "time": "09:00", "color": "yellow"}


def test_plugin_id():
    assert RemindersPlugin(MANIFEST).plugin_id == "reminders"


# ----------------------------------------------------------------------
# Schedule types
# ----------------------------------------------------------------------


def test_daily_is_due_every_day():
    # 2026-09-16 is a Wednesday, 2026-09-17 a Thursday.
    for day in (16, 17):
        plugin = make_plugin([VITAMINS], now=_utc(2026, 9, day, 12))
        data, _ = plugin._compute()
        assert data["due_count"] == "1"


def test_weekdays_schedule_skips_the_weekend():
    friday = make_plugin([WORK], now=_utc(2026, 9, 18, 12))  # Friday
    assert friday._compute()[0]["due_count"] == "1"

    saturday = make_plugin([WORK], now=_utc(2026, 9, 19, 12))  # Saturday
    data, _ = saturday._compute()
    assert data["due_count"] == "0"
    # Next occurrence rolls forward to Monday.
    assert data["reminders"][0]["next_date"] == "Sep 21"


def test_weekly_schedule_only_fires_on_its_weekday():
    wednesday = make_plugin([FERNS], now=_utc(2026, 9, 16, 12))
    assert wednesday._compute()[0]["due_count"] == "1"

    thursday = make_plugin([FERNS], now=_utc(2026, 9, 17, 12))
    data, _ = thursday._compute()
    assert data["due_count"] == "0"
    assert data["reminders"][0]["next_date"] == "Sep 23"


def test_every_n_days_follows_the_anchor_date():
    # Anchor 2026-09-14, every 3 days -> 14th, 17th, 20th.
    on_cycle = make_plugin([CAT], now=_utc(2026, 9, 17, 19))
    assert on_cycle._compute()[0]["due_count"] == "1"

    off_cycle = make_plugin([CAT], now=_utc(2026, 9, 18, 19))
    data, _ = off_cycle._compute()
    assert data["due_count"] == "0"
    assert data["reminders"][0]["next_date"] == "Sep 20"


# ----------------------------------------------------------------------
# Due window and midnight reset
# ----------------------------------------------------------------------


def test_not_due_before_its_time_and_counts_down():
    plugin = make_plugin([VITAMINS], now=_utc(2026, 9, 16, 5, 45))
    data, _ = plugin._compute()
    assert data["due_count"] == "0"
    assert data["message"] == "ALL DONE"
    assert data["next_name"] == "Take vitamin"  # truncated to 12 tiles
    assert data["next_time"] == "8:00 AM"
    assert data["next_in"] == "2H 15M"


def test_stays_due_all_day_once_its_time_passes():
    for hour in (8, 15, 23):
        plugin = make_plugin([VITAMINS], now=_utc(2026, 9, 16, hour))
        data, _ = plugin._compute()
        assert data["due_count"] == "1", hour
        assert data["next_in"] == "NOW"


def test_acknowledgement_resets_at_midnight():
    plugin = make_plugin([VITAMINS], now=_utc(2026, 9, 16, 12))
    plugin.receive_payload({"done": "Take vitamins"}, {})
    assert plugin._compute()[0]["due_count"] == "0"
    assert plugin._compute()[0]["reminders"][0]["is_done"] == "true"

    # Same in-memory acks, next day: the reminder is due again.
    plugin._now = lambda: _utc(2026, 9, 17, 12)
    data, _ = plugin._compute()
    assert data["due_count"] == "1"
    assert data["reminders"][0]["is_done"] == "false"


# ----------------------------------------------------------------------
# Acknowledgement payloads
# ----------------------------------------------------------------------


def test_ack_by_slug_and_by_name():
    plugin = make_plugin([VITAMINS, FERNS], now=_utc(2026, 9, 16, 12))
    assert plugin._compute()[0]["due_count"] == "2"

    plugin.receive_payload({"done": "take-vitamins"}, {})
    assert plugin._compute()[0]["due_count"] == "1"

    plugin.receive_payload({"done": "Water ferns"}, {})
    data, _ = plugin._compute()
    assert data["due_count"] == "0"
    assert data["message"] == "ALL DONE"


def test_ack_all():
    plugin = make_plugin([VITAMINS, FERNS, CAT], now=_utc(2026, 9, 16, 20))
    assert plugin._compute()[0]["due_count"] == "2"  # cat is off-cycle on the 16th
    plugin.receive_payload({"done": "all"}, {})
    assert plugin._compute()[0]["due_count"] == "0"


def test_ack_rejects_unknown_and_missing_names():
    plugin = make_plugin([VITAMINS], now=_utc(2026, 9, 16, 12))
    with pytest.raises(ValueError):
        plugin.receive_payload({"done": "walk the dog"}, {})
    with pytest.raises(ValueError):
        plugin.receive_payload({}, {})


# ----------------------------------------------------------------------
# HMAC
# ----------------------------------------------------------------------


def _signed(secret, body):
    import hashlib
    import hmac

    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_hmac_accepts_a_correct_signature():
    plugin = make_plugin([VITAMINS], now=_utc(2026, 9, 16, 12), secret="s3cret")
    body = json.dumps({"done": "all"}, separators=(",", ":")).encode()
    plugin.receive_payload(
        {"done": "all"}, {"x-webhook-signature": _signed("s3cret", body)}, body
    )
    assert plugin._compute()[0]["due_count"] == "0"


def test_hmac_rejects_a_bad_or_missing_signature():
    plugin = make_plugin([VITAMINS], now=_utc(2026, 9, 16, 12), secret="s3cret")
    with pytest.raises(PermissionError):
        plugin.receive_payload({"done": "all"}, {"x-webhook-signature": "sha256=nope"})
    with pytest.raises(PermissionError):
        plugin.receive_payload({"done": "all"}, {})
    assert plugin._compute()[0]["due_count"] == "1"


# ----------------------------------------------------------------------
# Triggers
# ----------------------------------------------------------------------


def test_trigger_fires_once_per_reminder_per_day():
    plugin = make_plugin([VITAMINS], now=_utc(2026, 9, 16, 8))
    first = plugin.check_triggers()
    assert len(first) == 1
    assert first[0].trigger_id == "take-vitamins-2026-09-16"
    assert first[0].triggered is True

    assert plugin.check_triggers() == []

    plugin._now = lambda: _utc(2026, 9, 17, 8)
    second = plugin.check_triggers()
    assert len(second) == 1
    assert second[0].trigger_id == "take-vitamins-2026-09-17"


def test_triggers_respect_the_opt_out_and_acknowledgement():
    off = make_plugin([VITAMINS], now=_utc(2026, 9, 16, 12), enable_triggers=False)
    assert off.check_triggers() == []

    acked = make_plugin([VITAMINS], now=_utc(2026, 9, 16, 12))
    acked.receive_payload({"done": "all"}, {})
    assert acked.check_triggers() == []


def test_cleanup_clears_state():
    plugin = make_plugin([VITAMINS], now=_utc(2026, 9, 16, 12))
    plugin.receive_payload({"done": "all"}, {})
    plugin.check_triggers()
    plugin.cleanup()
    assert plugin._acknowledged == {}
    assert plugin._fired == {}


# ----------------------------------------------------------------------
# Config validation
# ----------------------------------------------------------------------


def test_validate_config_accepts_a_good_config():
    plugin = RemindersPlugin(MANIFEST)
    assert plugin.validate_config({
        "reminders": [VITAMINS, FERNS, CAT, WORK],
        "timezone": "America/New_York",
        "refresh_seconds": 60,
    }) == []


@pytest.mark.parametrize("bad_time", ["25:00", "8am", "08:60", "", "8"])
def test_validate_config_rejects_bad_times(bad_time):
    plugin = RemindersPlugin(MANIFEST)
    errors = plugin.validate_config({"reminders": [{**VITAMINS, "time": bad_time}]})
    assert any("24-hour time" in e for e in errors)


@pytest.mark.parametrize("bad_interval", [0, -3, 400, "3", None])
def test_validate_config_rejects_bad_intervals(bad_interval):
    plugin = RemindersPlugin(MANIFEST)
    errors = plugin.validate_config({"reminders": [{**CAT, "interval_days": bad_interval}]})
    assert any("every N days" in e for e in errors)


def test_validate_config_rejects_other_mistakes():
    plugin = RemindersPlugin(MANIFEST)
    errors = plugin.validate_config({
        "reminders": [
            {"name": "", "schedule": "hourly", "time": "08:00", "color": "puce"},
            {**FERNS, "weekday": "Caturday"},
            {**CAT, "anchor_date": "not-a-date"},
        ],
        "timezone": "Mars/Olympus",
        "refresh_seconds": 5,
    })
    joined = " | ".join(errors)
    assert "name is required" in joined
    assert "repeats must be one of" in joined
    assert "color must be one of" in joined
    assert "day of week must be one of" in joined
    assert "starting date" in joined
    assert "Invalid timezone" in joined
    assert "at least 30 seconds" in joined


def test_validate_config_rejects_empty_too_many_and_duplicate_reminders():
    plugin = RemindersPlugin(MANIFEST)
    assert "At least one reminder is required" in plugin.validate_config({"reminders": []})
    assert any(
        f"At most {MAX_REMINDERS}" in e
        for e in plugin.validate_config({"reminders": [VITAMINS] * (MAX_REMINDERS + 1)})
    )
    assert any(
        "unique" in e
        for e in plugin.validate_config({"reminders": [VITAMINS, {**VITAMINS, "time": "20:00"}]})
    )


# ----------------------------------------------------------------------
# Data shape and display
# ----------------------------------------------------------------------


def test_fetch_data_returns_every_declared_variable():
    plugin = make_plugin([VITAMINS, FERNS], now=_utc(2026, 9, 16, 12))
    result = plugin.fetch_data()
    assert result.available is True

    declared = set(MANIFEST["variables"]["simple"]) | set(MANIFEST["variables"]["arrays"])
    assert set(result.data) == declared

    item_fields = set(MANIFEST["variables"]["arrays"]["reminders"]["item_fields"])
    assert all(set(item) == item_fields for item in result.data["reminders"])
    assert result.data["due_names"] == "Take vitamins, Water ferns"
    assert result.data["message"] == "2 REMINDERS DUE"


def test_fetch_data_unavailable_without_reminders():
    result = make_plugin([], now=_utc(2026, 9, 16, 12)).fetch_data()
    assert result.available is False
    assert "No reminders configured" in result.error


def test_misconfigured_reminders_are_skipped_not_fatal():
    plugin = make_plugin(
        ["not a dict", {"name": "Broken", "schedule": "daily", "time": "nope"}, VITAMINS],
        now=_utc(2026, 9, 16, 12),
    )
    data, _ = plugin._compute()
    assert [item["name"] for item in data["reminders"]] == ["Take vitamins"]


def test_formatted_display_fits_the_board():
    plugin = make_plugin([VITAMINS, FERNS, CAT], now=_utc(2026, 9, 16, 12))
    lines = plugin.get_formatted_display()
    assert lines is not None
    assert len(lines) == 6
    assert lines[0].strip() == "REMINDERS"
    # Colour markers are one tile each, so measure the rendered width.
    import re

    for line in lines:
        assert len(re.sub(r"\{[a-z]+\}", ".", line)) <= 22
    assert lines[1] == "{red} TAKE VITAMIN 8:00 AM"


def test_formatted_display_says_all_done():
    plugin = make_plugin([VITAMINS], now=_utc(2026, 9, 16, 12))
    plugin.receive_payload({"done": "all"}, {})
    lines = plugin.get_formatted_display()
    assert lines[2].strip() == "ALL DONE"


def test_formatted_display_returns_none_when_unavailable():
    assert make_plugin([], now=_utc(2026, 9, 16, 12)).get_formatted_display() is None


# ----------------------------------------------------------------------
# Board geometry
# ----------------------------------------------------------------------


def test_reminder_count_is_not_capped_at_six_on_a_tall_board():
    """A cap independent of the board leaves a tall panel mostly blank.

    MAX_REMINDERS used to be a hardcoded 6, so no matter how tall the board,
    at most 6 items would ever show. It is now derived from the largest
    note-array (24 rows), so a tall board can show far more than 6.
    """
    reminders = [
        {"name": f"R{i}", "schedule": "daily", "time": "00:00", "color": "red"}
        for i in range(1, MAX_REMINDERS + 1)
    ]
    plugin = make_plugin(reminders, now=_utc(2026, 9, 16, 12))
    board = BoardContext(device_type="note_array", rows=24, cols=15)  # 1 wide x 8 tall
    lines = plugin.get_data(board).formatted_lines
    assert len(lines) == 24
    shown = sum(1 for line in lines if line.strip())
    # Header + every configured reminder: strictly more than the old cap of
    # 6 (header + 6 = 7) would ever have allowed.
    assert shown == 1 + MAX_REMINDERS
    assert shown > 7


def test_reminder_line_widens_with_the_board():
    """The name field must scale with cols, not stay fixed at 12 tiles."""
    long_name = {
        "name": "Empty the dishwasher completely",
        "schedule": "daily",
        "time": "08:00",
        "color": "red",
    }
    plugin = make_plugin([long_name], now=_utc(2026, 9, 16, 12))
    board = BoardContext(device_type="note_array", rows=12, cols=30)  # 2 wide x 4 tall
    lines = plugin.get_data(board).formatted_lines
    assert lines[1] == "{red} EMPTY THE DISHWASHER 8:00 AM"


def test_reminder_line_drops_the_time_field_on_a_narrow_board():
    """A Note is too narrow for both a useful name and a time field."""
    plugin = make_plugin([VITAMINS], now=_utc(2026, 9, 16, 12))
    board = BoardContext(device_type="note", rows=3, cols=15)
    lines = plugin.get_data(board).formatted_lines
    assert lines[1] == "{red} TAKE VITAMINS"


def test_message_variable_fits_a_note_not_just_a_flagship():
    """`message` must respect the board it's rendered for, not a fixed 22."""
    plugin = make_plugin([VITAMINS], now=_utc(2026, 9, 16, 12))
    note = BoardContext(device_type="note", rows=3, cols=15)
    result = plugin.get_data(note)
    # "TAKE VITAMINS DUE" is 17 chars -- fits a 22-tile Flagship but not a
    # 15-tile Note, so the short fallback form must be used instead.
    assert result.data["message"] == "1 REMINDER DUE"
    assert len(result.data["message"]) <= 15


def test_get_formatted_display_honours_the_currently_bound_board():
    """get_formatted_display() must render for self.board, not a default.

    It must not go through get_data() (which defaults its own board
    argument to None and would silently discard whatever board the caller
    already bound), or every call would render a Flagship-sized frame.
    """
    plugin = make_plugin([VITAMINS], now=_utc(2026, 9, 16, 12))
    note = BoardContext(device_type="note", rows=3, cols=15)
    with plugin._bound_board(note):
        lines = plugin.get_formatted_display()
    assert len(lines) == 3
    for line in lines:
        assert len(line.replace("{red}", ".")) <= 15
