# Reminders Setup

Put your recurring tasks on the board and keep them there until someone says they are done.

## Overview

**What it does:**
- Shows up to 6 recurring reminders — medication, plant watering, pet feeding, anything
- A reminder turns due at its scheduled time and **stays** due until acknowledged or until midnight
- Optionally takes over the board the moment something comes due
- Acknowledge from your phone, a smart button, or anything that can make an HTTP request

**Prerequisites:**
- ✅ No API key needed — nothing leaves your network
- ✅ No internet connection required

## Quick Setup

### 1. Enable the Plugin

**Option A: Web UI**
1. Go to **Integrations** and find "Reminders"
2. Toggle **Enable Reminders** to on
3. Click **Save Changes**

**Option B: Environment Variable**

Add to your `.env` file:
```bash
REMINDERS_ENABLED=true
```

### 2. Add Your Reminders

Under **Reminders**, click **Add** for each one and fill in:

| Field | What it means |
|---|---|
| **Name** | Short label, up to 12 characters so it fits the board — "Take vitamins", "Water ferns" |
| **Repeats** | Every day / Weekdays only / Once a week / Every N days |
| **Day of Week** | Only used by *Once a week* |
| **Every N Days** + **Starting From** | Only used by *Every N days*. Enter any date the task happens (`YYYY-MM-DD`) so the cycle lines up |
| **Due At** | 24-hour time, e.g. `08:00` or `18:30` |
| **Color** | The tile colour shown next to the reminder on the board |

Set **Timezone** to your own IANA timezone so "today" and "8:00 AM" mean what you expect.

### 3. Use in Templates

Leave the page blank for the built-in display, or build your own:

```
{center}REMINDERS
{center}{{reminders.message}}
{{reminders.reminders.0.name}} {{reminders.reminders.0.time}}
{{reminders.reminders.1.name}} {{reminders.reminders.1.time}}
{{reminders.reminders.2.name}} {{reminders.reminders.2.time}}
NEXT {{reminders.next_name}} {{reminders.next_in}}
```

## Marking Something Done

POST to the plugin's receive endpoint. Replace `fiestaboard.local` with your board's address.

```bash
# One reminder, by name
curl -X POST http://fiestaboard.local/api/plugins/reminders/receive \
  -H 'Content-Type: application/json' \
  -d '{"done": "Take vitamins"}'

# ...or by slug (lowercase, dashes for spaces)
curl -X POST http://fiestaboard.local/api/plugins/reminders/receive \
  -H 'Content-Type: application/json' \
  -d '{"done": "take-vitamins"}'

# Everything, at once
curl -X POST http://fiestaboard.local/api/plugins/reminders/receive \
  -H 'Content-Type: application/json' \
  -d '{"done": "all"}'
```

The reminder stays acknowledged until midnight in your configured timezone, then comes back on its next scheduled day.

### With an HMAC secret

If you fill in **HMAC Secret**, every request must be signed. Sign the exact raw body with HMAC-SHA256:

```bash
BODY='{"done":"all"}'
SIG=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac 'your-secret' -hex | awk '{print $2}')

curl -X POST http://fiestaboard.local/api/plugins/reminders/receive \
  -H 'Content-Type: application/json' \
  -H "X-Webhook-Signature: sha256=$SIG" \
  -d "$BODY"
```

### Example: iOS Shortcut

A one-tap "I took my vitamins" button on your home screen:

1. Open **Shortcuts** → **+** → **Add Action**
2. Search for **Get Contents of URL** and add it
3. **URL:** `http://fiestaboard.local/api/plugins/reminders/receive`
4. Tap the arrow to expand **Show More**:
   - **Method:** `POST`
   - **Headers:** add `Content-Type` = `application/json`
   - **Request Body:** `JSON`
   - Add field **`done`** (Text) = `Take vitamins`
5. Name it "Vitamins done", then **Add to Home Screen**

Add a personal automation (Shortcuts → **Automation** → **Time of Day**) if you also want the tap prompted at 8am.

### Example: Home Assistant

Add a REST command and a button to `configuration.yaml`:

```yaml
rest_command:
  fiestaboard_reminder_done:
    url: "http://fiestaboard.local/api/plugins/reminders/receive"
    method: POST
    content_type: "application/json"
    payload: '{"done": "{{ reminder }}"}'

script:
  vitamins_done:
    alias: "Vitamins done"
    sequence:
      - service: rest_command.fiestaboard_reminder_done
        data:
          reminder: "Take vitamins"
```

Then wire `script.vitamins_done` to a dashboard button, a Zigbee button press, or a motion sensor in the bathroom:

```yaml
automation:
  - alias: "Clear vitamins reminder when the cabinet opens"
    trigger:
      - platform: state
        entity_id: binary_sensor.medicine_cabinet
        to: "on"
    action:
      - service: script.vitamins_done
```

## Board Takeover

**Take Over the Board When Due** is on by default: when a reminder comes due, the board switches to it for 10 minutes, once per reminder per day. Turn it off if you would rather drive the display yourself with `{{reminders.due_count}}` and a conditional page.

## Configuration Reference

| Setting | Name | Description | Default |
|---|---|---|---|
| `reminders` | Reminders | Up to 6 recurring tasks | one daily vitamins reminder |
| `timezone` | Timezone | IANA timezone used to decide what day it is | `America/Los_Angeles` |
| `secret` | HMAC Secret | Optional shared secret for the receive endpoint | `` |
| `enable_triggers` | Take Over the Board When Due | Switch the board to a reminder as it comes due | `true` |
| `refresh_seconds` | Refresh Interval (seconds) | How often to recompute what is due | `60` |
| `enabled` | Enable Reminders | Turn the plugin on | `false` |

### Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `REMINDERS_ENABLED` | No | `false` | Enable the Reminders plugin |

## Troubleshooting

- **Everything came back after a restart** — expected. Acknowledgements are held in memory, so a restart clears them and anything still due today reappears.
- **403 Forbidden** — the HMAC secret does not match, or the `X-Webhook-Signature` header is missing. Sign the *raw* body bytes, not a re-serialised copy.
- **400 Bad Request** — the `done` value does not match any configured reminder. Check `{{reminders.reminders.0.slug}}` for the exact key.
- **Reminder fires at the wrong time** — check **Timezone**; times are interpreted in that zone, not the server's.
- **Nothing shows up** — a reminder with an invalid time, weekday or starting date is skipped. Re-save the settings and the validation errors will point at it.
