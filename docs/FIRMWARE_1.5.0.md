# Firmware 1.5.0

The code on Board A inside Phytoplex: `firmware/synthetic_wifi/`. Version string
`FIRMWARE_VERSION "1.5.0"` in `platformio.ini`.

**Do not flash it until the buffer in Phytoplex has been dumped and the recovery
has been written.** The order is in the season-end procedure in the README.

## Why there is a 1.5.0

1.4.0 kept readings that failed to send in one file, `/buf.jsonl`, and had three
faults:

- On cellular it never re-sent them. The flush only ran on Wi-Fi.
- It never set its clock, so a kept reading had no time on it.
- At about 1.4 MB it tried to copy half the file, could not fit the copy, and
  deleted the whole buffer.

By 22 Oct, 3,340 readings were stuck in flash. 1.5.0 fixes all three and keeps those
old lines safe.

## The clock

The A7670C modem gets the time over NTP. The board now copies it into its own clock
(`settimeofday`), but **only when the modem says it really synced** this power-up. A
modem clock can look right and still be hours out.

- Once set, the clock is copied again every hour.
- While it is not set, the board retries every 10 minutes.
- With the clock set, every reading carries `sampled_at` (epoch seconds).
- Without it, readings carry `boot` and `up_s` only, and the server places them.

## The queue

`src/queue.h`. Readings that fail to send wait in flash, oldest first.

| Part | What it does |
|---|---|
| Segments | Files `/q/00000001.jl`, `/q/00000002.jl` and so on. Each holds up to **64 KB**, about 220 readings or 18 hours at one every 5 minutes. |
| Cursor | `/q/cursor` holds `<head segment> <byte position>`: the next line to send. A sent line is never sent twice, and nothing is rewritten or copied. |
| `MIN_FREE` | **96 KB** of flash is kept free so LittleFS can work. When flash is short, the **oldest segment is dropped**. The whole queue never is. |
| Flush | Runs after a live reading is sent, and only when the link is up (cellular ready or Wi-Fi connected). At most **20 lines per loop** and a **90 s budget**, so the 5-minute cycle is not stalled. |
| Marker | `,"queued":1` is added to each line before it is sent, so the server knows the arrival time is not the time it was taken. |
| Cursor saves | Every 5 lines. A segment that is fully sent is deleted. |

A power cut in the middle of a flush can re-send a few lines. The server recognises
them and stores each once (it answers 200 `duplicate`, so the board moves on).

### Permanent refusals

If the server answers **400, 413 or 422**, it has read the line and will never take
it. The line is skipped, not retried forever, so one torn line cannot block
everything behind it. A live reading refused this way is not queued either.

Any other failure (no link, timeout, 401, 5xx) stops the flush. It tries again on
the next loop.

### When flash is full anyway

If even dropping old segments leaves less than the new line needs plus 8 KB, the new
reading is lost and the serial log says so. Torn or blank lines are skipped and not
counted as waiting.

A write cut short by full flash or a power cut leaves a last line with no newline.
The next reading then starts a new segment, so it is never glued onto the torn one.
The torn line is offered once; the server answers 400 and the board moves past it.

## What every reading now carries

| Field | Meaning |
|---|---|
| `boot` | Power-up counter, kept in NVS (`agrisync`/`boot`). Goes up by one at every power-up. |
| `up_s` | Seconds since this power-up. |
| `reset` | Why the chip last reset. **First reading after a power-up only.** The server stores it as `reset_reason`. |
| `legacy` | How many old 1.4.0 lines are kept aside. Only present while there are some. |
| `leaf_wet_mv` | Raw millivolts from the leaf wetness sensor. Only on builds with `-DLEAF_WET_SENSOR=1`. |
| `sampled_at` | Epoch seconds from the board clock. Only once the clock is set. |
| `buffered_count` | Lines waiting in the queue. Unchanged from 1.4.0, but it now counts the new queue. |

`boot` plus `up_s` is a reading's identity. With no usable clock, the server places a
queued reading from a reading of the same boot whose time it knows: that time plus
the difference in `up_s`.

## The old 1.4.0 buffer

On first start, `/buf.jsonl` is renamed to **`/legacy.jsonl`**. It is:

- **kept**, so nothing is lost by flashing;
- **never sent**, because those lines have no time and would be stamped with the
  hour they arrived;
- **printed by `D`** as `[legacy] ...` lines.

`scripts/recover_buffer.py` puts those lines back at their right times from the
server's own record. The serial log reads
`[queue] N readings from 1.4.0 kept aside` at start-up.

## Serial commands

115200 baud. The board reads commands between cycles, so an answer can take up to
about five minutes.

| Key | What it does |
|---|---|
| `D` | Dump. Prints every queued line as `[payload] ...` and every legacy line as `[legacy] ...`, then `[dump] end, N readings`. **Deletes nothing.** |
| `C`, then `Y` within 10 s | Clear. Deletes the queue and `/legacy.jsonl`. **Never send `C` or `c` to Phytoplex in the field.** Only in the last step of the season-end procedure. |
| `?` | Status: firmware, boot count, uptime, whether the clock is set, queued, legacy, flash free. |
| `T` | Upload size sweep (cellular builds only). Posts a test body at growing sizes to find the largest one the modem sends without altering it. `400` means it arrived intact, `401` means it was altered. Touches no readings. |

Upper or lower case both work. `scripts/dump_buffer.py` sends one `D` and never `C`.

## Leaf wetness sensor (optional)

Off by default. In `platformio.ini` uncomment:

```
-DLEAF_WET_SENSOR=1
-DLEAF_WET_POWER_PIN=25
```

The sensor goes on ADS1115 channel A0. The board powers it from the pin only while it
reads (50 ms to settle, then the average of 4 samples), because a grid left under
constant voltage plates and corrodes.

It sends raw millivolts. What dry and wet look like depends on the sensor, so the
server does the conversion: set `LW_DRY_MV` and `LW_WET_MV` in `.env` (see the README).

## Building it

Everything secret comes in as an environment variable at build time. Nothing is
committed.

```bash
export WIFI_SSID='...' WIFI_PASSWORD='...' PI_HOST='...'
export DEVICE_API_KEY='...' CELL_APN='...'
export DEVICE_HMAC_SECRET='...' CF_ACCESS_ID='...' CF_ACCESS_SECRET='...'
cd firmware/synthetic_wifi && pio run -t upload
```

`TRANSPORT=2` is the cellular field build. A copy built without your real values
(for example in a sandbox) is a compile check only. **Do not flash it.**

Flash with plain `pio run -t upload`. Do not erase the flash or upload a filesystem
image over a box that still holds readings.

### Testing the queue on a computer

`test/queue_host_test.cpp` runs `src/queue.h` against an in-memory LittleFS
(`test/arduino_shim.h`, a 0x160000 partition in 4 KB blocks). No board, no serial
port.

```bash
cd firmware/synthetic_wifi
g++ -std=c++17 -Wall -I src -I test test/queue_host_test.cpp -o /tmp/qtest && /tmp/qtest
```

It covers push and flush order, segment rollover, the cursor across a reboot and a
power cut mid-flush, the 20-line and 90 s limits, 400/413/422 skipped and every
other failure retried, the oldest segment dropped at `MIN_FREE`, short writes and
torn last lines, the legacy rename, and the `D` dump format. It exits 1 on any
failure. `QTEST_CANARY=1` adds one check that must fail, to prove failures are
not swallowed.

## What the server does with these fields

Details are in `src/api/sensors.py`.

| `ts_source` | Meaning |
|---|---|
| `device` | The board's own clock, set by NTP. Trusted if it is no more than 5 minutes ahead and no more than 30 days behind (`MAX_BACKFILL_DAYS`). |
| `server` | A live reading, stamped on arrival. |
| `device_uptime` | Queued, placed by `up_s` from a reference reading of the same boot. It must land inside the 30-day window. |
| `server_queued` | Queued with nothing to place it by. Time unknown: flagged `ts:unknown` and kept out of the models. |
| `recovered` | Put back by `scripts/recover_buffer.py`. |

- A board clock outside that range is not trusted. The reading is stamped on
  arrival, with `ts_source` `server_device_clock_ahead` or `server_device_clock_behind`.
- Readings older than 5 minutes get `backfilled: true` and `delay_seconds`.
- A repeat is matched on device and time (for `device` and `recovered`), or on
  device, `boot` and `up_s` (for the rest).
