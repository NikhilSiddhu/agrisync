# Firmware 1.6.0

Builds on 1.5.0 (`docs/FIRMWARE_1.5.0.md`: the queue, the clock, `boot` and `up_s`,
the legacy buffer). Version string `FIRMWARE_VERSION "1.6.0"` in `platformio.ini`.

The point of 1.6.0: after the season-end flash, **Phytoplex should never need a cable
again.** Settings and new firmware reach it over 4G, signed by the Pi.

**Status: written and tested on a computer, not yet on the board.** Host tests pass
(below), and `main.cpp` compiles cleanly for all three transports against stand-in
headers. It has **not** yet been built with the real ESP32 toolchain: do that first,
on the Mac, with the key empty (a compile check, nothing is flashed):

```bash
cd ~/Project/AgriSync/firmware/synthetic_wifi
DEVICE_HMAC_SECRET= CELL_APN=airtelgprs.com WIFI_SSID=x WIFI_PASSWORD=x PI_HOST=x DEVICE_API_KEY=x pio run
```

Then bench-test it on a spare ESP32 and modem before it goes on the pole. Do not flash
the box until its buffer is dumped (README, "Season end").

## What changed

| Part | 1.5.0 | 1.6.0 |
|---|---|---|
| Interval | fixed 5 minutes | set by the Pi; faster on wet nights, slower on a low battery |
| Queue drain | one reading a request | up to 12 a request (`/readings/batch`) |
| Updates | by cable only | over 4G, checked, on trial, rolls back by itself |
| Device key | compiled into the image | kept in flash (NVS) |
| Rain counter | counted every flicker (53 million) | debounced; `rain_wet_pct` added |
| Diagnostics | none | `free_heap`, `min_heap`, `flash_free_kb`, `interval_s`, `cfg_v`, `ota_state`, `ota_pct` |

## Settings from the Pi

Every reply to a reading (and to a batch) can carry three extra fields:

```
"cfg":     "v=1;i=150;b=12"
"cfg_ts":  1793170000
"cfg_sig": "<hex HMAC-SHA256(key, '1793170000.v=1;i=150;b=12')>"
```

The box applies them only if all of these hold (`src/linkcore.h`):

- the HMAC matches, with the same key the box signs its readings with;
- `cfg_ts` is within 15 minutes of the box's own clock;
- `cfg_ts` is newer than the last config it applied (no replays).

| Key | Range | Meaning |
|---|---|---|
| `v` | | config version, for the logs |
| `i` | 60 to 1800 | seconds between readings |
| `b` | 1 to 12 | readings per queued batch; 1 = one at a time, as 1.5.0 |
| `ota`, `osz`, `osha` | | an update on offer: version, bytes, SHA-256 |

`i` and `b` are kept in flash and survive a power cut. Whatever `i` says, the box
reads every 10 minutes at most below 11.9 V and every 15 below 11.6 V (only on a build
with `-DUSE_BATTERY_ADC=1`).

On the Pi, `src/device_link.py` decides `i`:

| Situation | Interval |
|---|---|
| usual | `BOX_INTERVAL_S` (300) |
| 6 pm to 10 am India time, humidity 90% or more or the leaf counted wet | `BOX_WET_INTERVAL_S` (150) |
| battery under 11.9 V / 11.6 V | 600 / 900, whatever else |

Wet nights are when the disease models need the finest record of when leaves wet and
dry. Set `BOX_WET_INTERVAL_S` equal to `BOX_INTERVAL_S` to switch that off.

## Batches

After an outage the queue drains up to 12 readings per request, as one JSON array to
`POST /api/v1/sensors/readings/batch`, signed like a single reading. Each reading is
stored exactly as if it came alone, duplicates included. The cursor moves past a batch
only after a 2xx. If the Pi refuses a batch outright (400), the box sends those lines
one at a time, so one bad line is skipped alone. Batches stay under 3.6 KB: this modem
duplicates bytes when fed much more than 4 KB at once, and bodies over 1 KB are now
written in paced 256-byte pieces, as photos already were.

## Updates over 4G

1. Build the image **with the key empty** and publish it on the Pi:
   ```bash
   cd firmware/synthetic_wifi
   DEVICE_HMAC_SECRET= pio run
   # copy .pio/build/esp32dev/firmware.bin to the Pi, then on the Pi:
   python3 scripts/publish_firmware.py firmware.bin --version 1.6.1
   ```
   `publish_firmware.py` refuses an image that is not an ESP32 app, does not fit the
   1.25 MB update slot, or contains `DEVICE_HMAC_SECRET` (or the Wi-Fi password or API
   key) from `.env`. The download crosses the network in plain text; the key must never
   be in it.
2. Set `OTA_ENABLED=1` in `.env` and restart `agrisync-api`.
3. The next reply to a reading carries `ota`, `osz`, `osha`, signed. When the clock is
   set, the battery is at 12 V or more (if measured) and fewer than 100 readings are
   queued, the box starts downloading: `GET /api/v1/firmware/chunk?v=&off=&len=3072`,
   base64, signed over the path. It fetches for up to 60 s after each reading, so
   readings keep going out. A 1 MB image comes down in an hour or two of slices.
4. Every byte is hashed. The image is installed only if the SHA-256 matches `osha`.
   The old image stays the boot image until then; a reboot mid-download starts again.
5. The box reboots into the new image **on trial**. The first reading that gets
   through settles it (`ota_state: ok:1.6.1`). Three boots without one, and the box
   switches back to the previous image by itself (`ota_state: rolled_back:1.6.1`) and
   never tries that version again.
6. A bad hash or a refused install reports `fail:sha:1.6.1` (or `begin`, `write`,
   `end`) and that version is not tried again here. A failed download (`fetch`,
   `chunk`) is retried on the next offer. The Pi stops offering a version after three
   failures from the box. `python3 scripts/publish_firmware.py --status` and
   `/api/v1/devices` show the last word.

Withdraw an offer at any time: `python3 scripts/publish_firmware.py --withdraw`.

## The key in flash

A build made with `DEVICE_HMAC_SECRET` set writes it to NVS (namespace `agrisync`, key
`hmac`) at boot, replacing any older one. That is also how the key is rotated: one
cable flash with the new key. A build made with it empty uses what is stored. A board
that has never had a keyed build cannot sign, and says so on serial (`[key] NO DEVICE
KEY`).

**The season-end flash of 1.6.0 must be made with the new key set.** It is the last
cable flash; every later image goes over the air, built without the key.

## Rain

1.4.0 counted every edge of the rain plate's comparator, which chatters when the plate
is half wet: 53,575,447 by October. Edges less than 200 ms apart now count once.
`rain_wet_pct` is the share of 10-second samples in the interval when the plate read
wet. The plate on the pole has read wet since 27 Sep and needs cleaning or replacing;
the Pi's health check now says so.

## Testing on a computer

```bash
cd firmware/synthetic_wifi
g++ -std=c++17 -Wall -I src -I test test/queue_host_test.cpp -o qtest && ./qtest     # 1.5.0 queue: 238 checks
g++ -std=c++17 -Wall -I src -I test test/batch_host_test.cpp -o btest && ./btest     # batches: 26 checks
g++ -std=c++17 -Wall -I src -I test test/link_host_test.cpp -lcrypto -o ltest && ./ltest   # settings and updates: 94 checks
```

All three are also clean under `-fsanitize=address,undefined`. `QTEST_CANARY=1`,
`BTEST_CANARY=1` and `LTEST_CANARY=1` each add a check that must fail.

The link test checks the box's SHA-256 and base64 against OpenSSL (empty input to 1 MB,
whole and in odd-sized pieces), the config parser and signature check (tampered,
stale, replayed), and the download: clean, spread over loops, an altered chunk caught
by the hash, short chunks re-fetched, a dropped link resumed, write and install
failures, and the offer rules.

What the host tests cannot cover: the A7670C's `AT+HTTPREAD` framing on the real
modem, the Update library writing flash, and the trial boot and rollback. Those need
the bench.

## Bench checklist before the pole

1. Cable-flash a spare ESP32 with 1.6.0 and a test key. `?` shows `key stored`.
2. Point it at the Pi. A reading's reply should apply a config: `[cfg] interval now ...`.
3. Pull the antenna for an hour, put it back: `[batch] ... HTTP 200` lines drain it.
4. Publish a 1.6.1 built with the key empty, `OTA_ENABLED=1`. Watch `ota_pct` climb in
   `/api/v1/sensors/readings`, the reboot, then `ota_state: ok:1.6.1`.
5. Publish a deliberately broken 1.6.2 (one that never posts). After three power
   cycles the board must be back on 1.6.1 with `rolled_back:1.6.2`.
