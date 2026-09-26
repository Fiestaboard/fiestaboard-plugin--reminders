"""Board-geometry conformance for the Reminders plugin.

Runs the shared FiestaBoard conformance suite (the same definition of
"supports every board" core holds its own plugins to) against this plugin.
See ``src/plugins/geometry_conformance.py`` in the FiestaBoard core repo.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from src.plugins.geometry_conformance import assert_board_conformance

from plugins.reminders import MAX_REMINDERS, RemindersPlugin

MANIFEST = json.loads(
    (Path(__file__).resolve().parent.parent / "manifest.json").read_text(encoding="utf-8")
)

# Well after midnight local time, so every "daily" reminder below is due
# regardless of which timezone or DST offset is in effect when this runs.
_NOW = datetime(2026, 9, 16, 23, 0, tzinfo=timezone.utc)


def _make_plugin() -> RemindersPlugin:
    """Fresh, configured, ready-to-render plugin.

    No network access exists to stub -- the plugin is purely schedule-driven.
    Configured up to the new cap (MAX_REMINDERS) so the growth check has
    enough due reminders to actually fill the tallest geometry in the ladder
    (a 24-row note array), rather than running out of content early.
    """
    plugin = RemindersPlugin(MANIFEST)
    plugin.config = {
        "enabled": True,
        "timezone": "America/Los_Angeles",
        "refresh_seconds": 60,
        "reminders": [
            {"name": f"R{i}", "schedule": "daily", "time": "00:00", "color": "red"}
            for i in range(1, MAX_REMINDERS + 1)
        ],
    }
    plugin._now = lambda: _NOW
    return plugin


def test_renders_on_every_board_shape():
    assert_board_conformance(
        _make_plugin,
        manifest=MANIFEST,
        strict_growth=True,
        require_note_array_preview=True,
    )
