# Reminders Plugin

Recurring nags that stay on the board until someone acknowledges them — medication, plant watering, pet feeding, bin day, anything that has to happen on a schedule.

**→ [Setup Guide](./docs/SETUP.md)** - Configuration instructions

## Overview

You describe up to six reminders in the plugin settings: a name, when it repeats, and what time of day it comes due. At that time the reminder turns **due** and stays due — on the board, counted in `{{reminders.due_count}}` — until either someone marks it done or the local day rolls over.

Acknowledgements come in over HTTP:

```bash
curl -X POST http://fiestaboard.local/api/plugins/reminders/receive \
  -H 'Content-Type: application/json' \
  -d '{"done": "Take vitamins"}'
```

No external API, no API key, no network access.

## Schedules

| `schedule` | Fires on | Extra fields |
|---|---|---|
| `daily` | Every day | — |
| `weekdays` | Monday–Friday | — |
| `weekly` | One day a week | `weekday` |
| `every_n_days` | Every N days from an anchor | `interval_days`, `anchor_date` |

Every reminder also takes a `time` (24-hour `HH:MM`) and a `color` (`red`, `orange`, `yellow`, `green`, `blue`, `violet`, `white`).

## Template Variables

```
{{reminders.due_count}}    # How many are due right now, e.g. 2
{{reminders.due_names}}    # "Take vitamins, Water ferns"
{{reminders.message}}      # "2 REMINDERS DUE" / "ALL DONE"
{{reminders.next_name}}    # Soonest reminder's name
{{reminders.next_time}}    # "8:00 AM"
{{reminders.next_in}}      # "2H 15M", or "NOW" when it is already due
```

And the `reminders` array, one entry per configured reminder, soonest first:

```
{{reminders.reminders.0.name}}       # "Take vitamins"
{{reminders.reminders.0.slug}}       # "take-vitamins" — the acknowledgement key
{{reminders.reminders.0.time}}       # "8:00 AM"
{{reminders.reminders.0.is_due}}     # "true" / "false"
{{reminders.reminders.0.is_done}}    # "true" / "false"
{{reminders.reminders.0.color}}      # "red"
{{reminders.reminders.0.next_date}}  # "Sep 16"
```

`due_count` ships a default colour rule: red while anything is outstanding, green at zero.

## Example Template

```
{center}REMINDERS
{center}{{reminders.message}}
{{reminders.reminders.0.name}} {{reminders.reminders.0.time}}
{{reminders.reminders.1.name}} {{reminders.reminders.1.time}}
{{reminders.reminders.2.name}} {{reminders.reminders.2.time}}
NEXT {{reminders.next_name}} {{reminders.next_in}}
```

Leave the page blank to get the built-in display: a `REMINDERS` header, one colour-tiled line per due reminder, and `ALL DONE` when nothing is outstanding.

## Acknowledgement Endpoint

`POST /api/plugins/reminders/receive`

| Body | Effect |
|---|---|
| `{"done": "Take vitamins"}` | Marks that reminder done for the rest of the local day |
| `{"done": "take-vitamins"}` | Same thing by slug — names and slugs both match |
| `{"done": "all"}` | Marks every configured reminder done for today |

Set the **HMAC Secret** setting to require a signature. Requests must then carry `X-Webhook-Signature: sha256=<hex>`, an HMAC-SHA256 of the raw request body keyed with the secret. Unsigned or mis-signed requests get a `403`; an unknown reminder name gets a `400`.

**Acknowledgements live in memory.** A FiestaBoard restart clears them, so anything still due today comes back. That is deliberate — a reminder you have already dealt with reappearing is a much smaller problem than one that silently vanishes.

## Triggers

With **Take Over the Board When Due** on (the default), the board switches to a reminder as it comes due, once per reminder per day. Trigger IDs are `<slug>-<YYYY-MM-DD>`, so each due instance fires exactly once. Turn the setting off to use the `{{reminders.*}}` variables on your own pages without ever interrupting the board.

## Configuration

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `reminders` | array | one daily vitamins reminder | Up to 6 reminders |
| `timezone` | string | `America/Los_Angeles` | IANA timezone for "what day is it" and due times |
| `secret` | string | `""` | Optional HMAC secret for the receive endpoint |
| `enable_triggers` | boolean | `true` | Take over the board when a reminder comes due |
| `refresh_seconds` | integer | `60` | How often to recompute what is due (min 30) |
| `enabled` | boolean | `false` | Enable/disable the plugin |

## API

None. Everything is computed locally from the configured schedules.

## Author

FiestaBoard Team
