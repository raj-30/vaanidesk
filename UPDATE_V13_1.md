# Bridge v13.1 — callback firing retries on network errors

## What happened

The first real scheduled callback fired on time, but the call to Sarvam
failed instantly with a DNS error (`Failed to resolve 'apps.sarvam.ai'`,
a temporary name-resolution hiccup on the laptop) and v13 gave up after
that single attempt, posting the "could NOT be placed, please call the
customer manually" note.

## The fix (bridge/main.py, `_fire_callback` only)

- If placing a scheduled callback fails with a network error
  (`requests.ConnectionError` or `requests.Timeout` — DNS failures,
  connection drops, timeouts), the bridge now retries up to **5 attempts,
  45 seconds apart** (about 3 minutes) before giving up.
- If Sarvam itself rejects the request (HTTP error, e.g. a bad number),
  it is NOT retried; the manual-call note is posted immediately, as before.
- Everything else is unchanged: on success the usual "Scheduled callback
  started" note, labels and live panel status work exactly as in v13.

## Tests

Mocked, all passing:
1. DNS fails twice, third attempt succeeds → call placed, success note.
2. Network never recovers → exactly 5 attempts, then the manual-call note.
3. HTTP 400 from Sarvam → exactly 1 attempt, manual-call note.

## Update steps (Raj)

```
cd ~/vaanidesk && git pull
cp bridge/main.py ~/bridge/main.py
```

Then restart the bridge the usual way (T4 terminal: Ctrl+C, re-export
.env, uvicorn). No .env changes, no Chatwoot changes.
