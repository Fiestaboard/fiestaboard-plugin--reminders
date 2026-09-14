"""Reminders plugin for FiestaBoard.

Recurring nags — medication, plant watering, pet feeding — that go up on the
board at their scheduled time and stay there until someone acknowledges them.
Purely schedule-driven: no network access, no external API.

Acknowledgements arrive as an HTTP POST to ``/api/plugins/reminders/receive``
and are held in memory on the plugin instance, so a FiestaBoard restart clears
them and anything still due today comes back. That is deliberate: a reminder
you have already dealt with reappearing after a restart is a much smaller
problem than one that silently disappears, and it keeps the plugin free of
persistent state.
"""

import hashlib
import hmac
import json
import logging
import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import pytz

from src.plugins.base import PluginBase, PluginResult, TriggerResult

logger = logging.getLogger(__name__)

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
SCHEDULES = ("daily", "weekdays", "weekly", "every_n_days")
COLORS = ("red", "orange", "yellow", "green", "blue", "violet", "white")

DEFAULT_TIMEZONE = "America/Los_Angeles"
DEFAULT_COLOR = "red"
MAX_REMINDERS = 6
MAX_NAME_TILES = 12

_SIGNATURE_HEADER = "x-webhook-signature"
_TRIGGER_PRIORITY = 5
_TRIGGER_DURATION_SECONDS = 600


def _slug(name: str) -> str:
    """Normalise a reminder name into its acknowledgement key.

    Also used on the incoming ``done`` value, so a payload can name a reminder
    either way round: "Water the ferns" and "water-the-ferns" both match.
    """
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", name.strip().lower())).strip("-")


def _parse_date(value: Any) -> Optional[date]:
    """Parse a YYYY-MM-DD string, returning None when it is not a valid date."""
    try:
        return date.fromisoformat(str(value).strip())
    except (TypeError, ValueError):
        return None


def _parse_time(value: Any) -> Optional[time]:
    """Parse a 24-hour HH:MM string, returning None when it is not a valid time."""
    text = str(value).strip()
    if not re.fullmatch(r"\d{1,2}:\d{2}", text):
        return None
    hour, minute = (int(part) for part in text.split(":"))
    if hour > 23 or minute > 59:
        return None
    return time(hour, minute)


def _format_time(t: time) -> str:
    """'8:00 AM' style time."""
    return t.strftime("%I:%M %p").lstrip("0")


def _format_date(d: date) -> str:
    """'Sep 16' style date."""
    return d.strftime("%b ") + str(d.day)


def _format_in(seconds: float) -> str:
    """'2H 15M' style countdown; 'NOW' once the reminder is due."""
    if seconds <= 0:
        return "NOW"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes}M"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}H {minutes}M"
    days, hours = divmod(hours, 24)
    return f"{days}D {hours}H"


def _occurs_on(reminder: Dict[str, Any], day: date) -> bool:
    """Whether a (already normalised) reminder happens on ``day``."""
    schedule = reminder["schedule"]
    if schedule == "daily":
        return True
    if schedule == "weekdays":
        return day.weekday() < 5
    if schedule == "weekly":
        return day.weekday() == WEEKDAYS.index(reminder["weekday"])
    # every_n_days: Python's % is never negative, so anchors in the future work too.
    return (day - reminder["anchor_date"]).days % reminder["interval_days"] == 0


def _next_occurrence(reminder: Dict[str, Any], start: date) -> date:
    """First day on or after ``start`` that the reminder happens."""
    for offset in range(366):
        day = start + timedelta(days=offset)
        if _occurs_on(reminder, day):
            return day
    # Unreachable: interval_days is capped at 365 and every other schedule
    # repeats within a week.
    raise ValueError(f"No occurrence found for reminder '{reminder['name']}'")


def _build_message(due_names: List[str]) -> str:
    """One-line summary that always fits in 22 tiles."""
    if not due_names:
        return "ALL DONE"
    if len(due_names) == 1:
        single = f"{due_names[0].upper()} DUE"
        return single if len(single) <= 22 else "1 REMINDER DUE"
    return f"{len(due_names)} REMINDERS DUE"


class RemindersPlugin(PluginBase):
    """Reminders plugin."""

    def __init__(self, manifest: Dict[str, Any]):
        super().__init__(manifest)
        # slug -> local date the reminder was acknowledged on.
        self._acknowledged: Dict[str, date] = {}
        # slug -> local date a board takeover last fired on.
        self._fired: Dict[str, date] = {}

    @property
    def plugin_id(self) -> str:
        return "reminders"

    # ------------------------------------------------------------------
    # Time
    # ------------------------------------------------------------------

    def _now(self) -> datetime:
        """Current UTC time. Overridden in tests."""
        return datetime.now(timezone.utc)

    def _timezone(self):
        return pytz.timezone(self.config.get("timezone", DEFAULT_TIMEZONE))

    def _local_now(self) -> datetime:
        return self._now().astimezone(self._timezone())

    # ------------------------------------------------------------------
    # Config
    # ------------------------------------------------------------------

    def validate_config(self, config: Dict[str, Any]) -> List[str]:
        errors: List[str] = []

        tz_name = config.get("timezone", DEFAULT_TIMEZONE)
        try:
            pytz.timezone(tz_name)
        except pytz.exceptions.UnknownTimeZoneError:
            errors.append(f"Invalid timezone: {tz_name}")

        reminders = config.get("reminders", [])
        if not isinstance(reminders, list) or not reminders:
            errors.append("At least one reminder is required")
        elif len(reminders) > MAX_REMINDERS:
            errors.append(f"At most {MAX_REMINDERS} reminders are supported")
        else:
            for i, reminder in enumerate(reminders, start=1):
                errors.extend(self._validate_reminder(i, reminder))
            names = [
                _slug(str(r.get("name", ""))) for r in reminders if isinstance(r, dict)
            ]
            if len(set(names)) != len(names):
                errors.append("Reminder names must be unique — they are how an acknowledgement picks one")

        errors.extend(self._validate_refresh_seconds(config))
        return errors

    @staticmethod
    def _validate_reminder(index: int, reminder: Any) -> List[str]:
        errors: List[str] = []
        if not isinstance(reminder, dict):
            return [f"Reminder {index}: must be an object"]

        if not str(reminder.get("name", "")).strip():
            errors.append(f"Reminder {index}: name is required")

        schedule = reminder.get("schedule", "daily")
        if schedule not in SCHEDULES:
            errors.append(f"Reminder {index}: repeats must be one of {', '.join(SCHEDULES)}")
        elif schedule == "weekly":
            if reminder.get("weekday") not in WEEKDAYS:
                errors.append(f"Reminder {index}: day of week must be one of {', '.join(WEEKDAYS)}")
        elif schedule == "every_n_days":
            interval = reminder.get("interval_days")
            if not isinstance(interval, int) or isinstance(interval, bool) or not 1 <= interval <= 365:
                errors.append(f"Reminder {index}: every N days must be a whole number between 1 and 365")
            if _parse_date(reminder.get("anchor_date")) is None:
                errors.append(
                    f"Reminder {index}: a starting date (YYYY-MM-DD) is required for an every-N-days schedule"
                )

        if _parse_time(reminder.get("time")) is None:
            errors.append(f"Reminder {index}: due at must be a 24-hour time like 08:00")

        if reminder.get("color", DEFAULT_COLOR) not in COLORS:
            errors.append(f"Reminder {index}: color must be one of {', '.join(COLORS)}")

        return errors

    def _reminders(self) -> List[Dict[str, Any]]:
        """Configured reminders, normalised; misconfigured entries are dropped."""
        normalised: List[Dict[str, Any]] = []
        for raw in self.config.get("reminders") or []:
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("name", "")).strip()
            schedule = raw.get("schedule", "daily")
            at = _parse_time(raw.get("time"))
            anchor = _parse_date(raw.get("anchor_date"))
            interval = raw.get("interval_days")
            if (
                not name
                or schedule not in SCHEDULES
                or at is None
                or (schedule == "weekly" and raw.get("weekday") not in WEEKDAYS)
                or (
                    schedule == "every_n_days"
                    and (anchor is None or not isinstance(interval, int) or interval < 1)
                )
            ):
                logger.warning("Skipping misconfigured reminder: %r", raw)
                continue
            color = raw.get("color", DEFAULT_COLOR)
            normalised.append({
                "name": name,
                "slug": _slug(name),
                "schedule": schedule,
                "weekday": raw.get("weekday"),
                "interval_days": interval,
                "anchor_date": anchor,
                "time": at,
                "color": color if color in COLORS else DEFAULT_COLOR,
            })
        return normalised[:MAX_REMINDERS]

    # ------------------------------------------------------------------
    # Schedule
    # ------------------------------------------------------------------

    def _compute(self) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        """Build the template data dict, plus the item list with sort keys."""
        tz = self._timezone()
        now = self._now().astimezone(tz)
        today = now.date()

        items: List[Dict[str, Any]] = []
        for reminder in self._reminders():
            slug = reminder["slug"]
            occurs_today = _occurs_on(reminder, today)
            acknowledged = self._acknowledged.get(slug) == today
            is_due = occurs_today and now.time() >= reminder["time"] and not acknowledged

            if is_due or (occurs_today and now.time() < reminder["time"]):
                next_day = today
            else:
                next_day = _next_occurrence(reminder, today + timedelta(days=1))

            next_dt = tz.localize(datetime.combine(next_day, reminder["time"]))
            items.append({
                "name": reminder["name"],
                "slug": slug,
                "time": _format_time(reminder["time"]),
                "is_due": "true" if is_due else "false",
                "is_done": "true" if (occurs_today and acknowledged) else "false",
                "color": reminder["color"],
                "next_date": _format_date(next_day),
                "_dt": next_dt,
            })

        if not items:
            raise ValueError("No reminders configured")

        items.sort(key=lambda item: item["_dt"])
        due_names = [item["name"] for item in items if item["is_due"] == "true"]
        soonest = items[0]

        data = {
            "due_count": str(len(due_names)),
            "due_names": ", ".join(due_names)[:44],
            "message": _build_message(due_names),
            "next_name": soonest["name"][:MAX_NAME_TILES],
            "next_time": soonest["time"],
            "next_in": _format_in((soonest["_dt"] - now).total_seconds()),
            "reminders": [
                {k: v for k, v in item.items() if k != "_dt"} for item in items
            ],
        }
        return data, items

    # ------------------------------------------------------------------
    # Acknowledgement
    # ------------------------------------------------------------------

    def receive_payload(
        self,
        payload: Dict[str, Any],
        headers: Dict[str, str],
        raw_body: bytes = b"",
    ) -> None:
        """Mark a reminder (or everything) done for the rest of the local day."""
        secret = self.config.get("secret", "")
        if secret:
            sig_header = headers.get(_SIGNATURE_HEADER, "")
            if not sig_header:
                raise PermissionError("Missing X-Webhook-Signature header")
            body_bytes = raw_body if raw_body else json.dumps(payload, separators=(",", ":")).encode()
            expected = "sha256=" + hmac.new(secret.encode(), body_bytes, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(sig_header, expected):
                raise PermissionError("Invalid webhook signature")

        done = str(payload.get("done", "")).strip()
        if not done:
            raise ValueError("Payload must include a 'done' field naming a reminder, or 'all'")

        today = self._local_now().date()
        reminders = self._reminders()

        if done.lower() == "all":
            for reminder in reminders:
                self._acknowledged[reminder["slug"]] = today
            return

        wanted = _slug(done)
        for reminder in reminders:
            if reminder["slug"] == wanted:
                self._acknowledged[wanted] = today
                return
        raise ValueError(f"No reminder named '{done}'")

    # ------------------------------------------------------------------
    # PluginBase API
    # ------------------------------------------------------------------

    def fetch_data(self) -> PluginResult:
        try:
            data, _ = self._compute()
            return PluginResult(
                available=True,
                data=data,
                formatted_lines=self._format_display(data),
            )
        except Exception as e:
            logger.exception("Error computing reminders")
            return PluginResult(available=False, error=str(e))

    def get_formatted_display(self) -> Optional[List[str]]:
        result = self.get_data()
        if not result.available:
            return None
        return result.formatted_lines

    def check_triggers(self) -> List[TriggerResult]:
        """Fire one board takeover per reminder per day, as it comes due."""
        if not self.config.get("enable_triggers", True):
            return []

        try:
            data, items = self._compute()
        except Exception:
            logger.warning("Could not compute reminders for trigger check", exc_info=True)
            return []

        today = self._local_now().date()
        results: List[TriggerResult] = []
        for item in items:
            slug = item["slug"]
            if item["is_due"] != "true" or self._fired.get(slug) == today:
                continue
            self._fired[slug] = today
            results.append(TriggerResult(
                triggered=True,
                trigger_id=f"{slug}-{today.isoformat()}",
                formatted_lines=self._format_trigger(item),
                priority=_TRIGGER_PRIORITY,
                duration_seconds=_TRIGGER_DURATION_SECONDS,
                data=data,
            ))
        return results

    def cleanup(self) -> None:
        self._acknowledged.clear()
        self._fired.clear()

    # ------------------------------------------------------------------
    # Display
    # ------------------------------------------------------------------

    def _cols(self) -> int:
        return self.board.cols if self.board else 22

    def _rows(self) -> int:
        return self.board.rows if self.board else 6

    def _reminder_line(self, item: Dict[str, Any], cols: int) -> str:
        tile = f"{{{item['color']}}}"
        name = item["name"].upper()
        if cols >= 22:
            # tile + space + 12-char name + right-aligned 8-char time = 22 tiles
            return f"{tile} {name[:MAX_NAME_TILES]:<{MAX_NAME_TILES}}{item['time']:>8}"
        return f"{tile} {name[:cols - 2]}"

    def _format_display(self, data: Dict[str, Any]) -> List[str]:
        rows = self._rows()
        cols = self._cols()

        lines = ["REMINDERS".center(cols).rstrip()]
        due = [item for item in data["reminders"] if item["is_due"] == "true"]
        if due:
            for item in due[: rows - 1]:
                lines.append(self._reminder_line(item, cols))
        else:
            lines.append("")
            lines.append("ALL DONE".center(cols).rstrip())

        while len(lines) < rows:
            lines.append("")
        return lines[:rows]

    def _format_trigger(self, item: Dict[str, Any]) -> List[str]:
        rows = self._rows()
        cols = self._cols()
        lines = [
            "REMINDER".center(cols).rstrip(),
            item["name"].upper()[:cols].center(cols).rstrip(),
            item["time"].center(cols).rstrip(),
        ]
        while len(lines) < rows:
            lines.append("")
        return lines[:rows]


# Export the plugin class
Plugin = RemindersPlugin
