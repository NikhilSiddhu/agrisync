# AgriSync

AgriSync is the whole system. Phytoplex is the solar-powered unit in the field in
Hapur, U.P., logging cabbage disease conditions. Inside it: two ESP32 boards. Back
home: a Raspberry Pi 5 and a physics-based disease-odds engine.

Site: 28.7170, 77.7740 · Crop: cabbage (rabi) · Go-live: transplant, Aug 2026

---

## What is real and what is not

Read this before you trust any number this system produces.

**Real:** air temperature, humidity, pressure (BME280), rain wet/dry, canopy images.

**Derived, not measured:** `leaf_wetness`. There is no leaf wetness sensor in this
build, and the infrared leaf thermometer (MLX90614) never read reliably and was
removed. Leaf temperature is estimated as air temperature minus 1 °C, and a reading
counts as wet when that is at or below the dew point. It is a proxy and it is the
honest weak point of the project. A leaf wetness sensor on the ADS1115 A0 input is
the drop-in upgrade, and firmware 1.5.0 and the server are ready for it (see
[Leaf wetness](#leaf-wetness-proxy-or-sensor)). `src/ml/leaf_temp.py` (the
sky-temperature correction for the infrared sensor) stays in the repo for when a
working one goes back in.

**Modelled, not measured:** everything from outside Phytoplex. Rain comes from the
ERA5 reanalysis, sunlight from NASA POWER, the next three days from a weather
forecast. The dashboard labels each one with its source.

**Not yet checked against this field:** the disease odds. Their scale starts as one
stated assumption and only moves once the field log holds at least 20 judged visits
and 3 with new spots. Until then the dashboard says so. The older infection-pressure
curve (`/api/v1/risk`) is a physics floor, not a probability, and always returns
`calibrated: false`. Do not make spray decisions from either. Walk the crop.

---

## Repo layout

```
main/app.py                  Flask entry point and app factory
src/config.py                environment config
src/db.py                    Mongo connection
src/auth.py                  X-API-Key check for device POSTs
src/api/health.py            /health, /health/db
src/api/sensors.py           POST + GET /api/v1/sensors/readings
src/api/risk.py              GET /api/v1/risk/<device_id>
src/ml/wetness.py            dew point + leaf wetness proxy
src/ml/leaf_temp.py          emissivity + sky temperature correction
src/validation.py            plausibility checks, quality flags
src/health_check.py          device health assessment
src/api/devices.py           GET /api/v1/devices
scripts/check_devices.py     cron watchdog with alerting
scripts/serial_bridge.py     USB serial -> API, for bench use
deploy/                      systemd units, see deploy/README.md
src/ml/physics_baseline.py   disease infection pressure models
src/ml/forecast_risk.py      the same models run 12-72 h ahead on a forecast
src/ml/anomaly.py            unusual-conditions check against the last 15 days
src/ml/attribution.py        disease, or just stress? (canopy fall vs weather)
src/ml/odds.py               the odds engine: one number, with a range
src/ml/leafwet.py            leaf wetness from an energy balance, not the dew-point rule
src/ml/pathogens.py          the five diseases: temperatures, wet hours needed
src/ml/mos.py                corrects the forecast to this field
src/ml/calibration.py        checks the odds against the field log
src/ml/baseline.py           a model of this field when it is healthy; flags departures
src/alerts.py                phone alerts when the odds turn high
src/feeds/sources.py         Open-Meteo, ERA5-Land, ERA5, NASA POWER, Sentinel-2, Sentinel-1
scripts/update_feeds.py      refreshes every outside feed and the odds, run hourly by a timer
scripts/dump_buffer.py       copies the readings stuck in Phytoplex's flash to a file
scripts/recover_buffer.py    puts those readings back at their right times
scripts/publish_firmware.py  offers a firmware image to the box over 4G (1.6.0)
src/device_link.py           the signed settings the Pi sends back to the box (1.6.0)
deploy/update.sh             updates the Pi from GitHub, checks it, goes back if not
src/api/fieldlog.py          /admin/field-log, what the plants actually did
src/weather/forecast.py      Open-Meteo forecast fetch (no API key needed)
firmware/synthetic_wifi/     Board A - ESP32 sensor node (firmware 1.6.0)
firmware/esp32cam_capture/   Board B - ESP32-CAM
docs/FIRMWARE_1.5.0.md       what firmware 1.5.0 does: queue, payload, serial commands
docs/FIRMWARE_1.6.0.md       what 1.6.0 adds: settings from the Pi, batches, updates over 4G
scripts/backup_mongo.sh      nightly mongodump
tests/                       runs without any hardware
data/field_log.csv           your field observations - the most valuable file here
progress.md                  stage-by-stage build checklist
```

---

## Quick start on the Pi

```bash
git clone <your-repo-url> AgriSync && cd AgriSync
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env && nano .env      # set MONGO_URI and DEVICE_API_KEY
python main/app.py
```

Check it:

```bash
curl localhost:5000/health
curl localhost:5000/health/db
```

Then, **from your laptop, not the Pi**:

```bash
curl http://<pi-ip>:5000/health
```

That last one is the test people skip. Passing on localhost does not prove the ESP32
can reach the API.

## Production

```bash
.venv/bin/gunicorn -w 1 -b 0.0.0.0:5000 "main.app:create_app()"
```

One worker. Multiple workers each holding a Mongo pool buys nothing on a Pi serving
a few requests a minute.

## Tests

```bash
pip install -r requirements-dev.txt
pytest -q
```

About 240 tests, no hardware, no database and no internet needed. They check the
dew point math, the odds engine, the disease model shape, the queued-reading ingest,
the buffer recovery, every API route including auth rejection, and every outside
feed with its source replaced by a fake. A few skip when an optional package is
missing.

## Bench mode: USB instead of WiFi

The board is powered from the Pi's USB anyway. On the bench that cable is a far
more reliable path than WiFi, and WiFi never ships — Phytoplex runs on 4G
through the tunnel.

Flash with `-DTRANSPORT=0` in `platformio.ini`, then run the bridge:

```bash
sudo systemctl start agrisync-serial-bridge
journalctl -u agrisync-serial-bridge -f
```

The board prints each reading to serial, the bridge forwards it to the API. No
radio, no NTP, no buffering, no failed POSTs. The Pi stamps the timestamps, which
is accurate to within a second on a live serial link.

The journal shows every line the board prints, so it replaces `pio device
monitor`. Only one process can hold a serial port — stop the service before
flashing:

```bash
sudo systemctl stop agrisync-serial-bridge
```

Set `TRANSPORT` back to 2 (the A7670C cellular modem) before Phytoplex leaves your
desk. The bridge must also be stopped whenever you dump the buffer; see
[Season end](#season-end-dump-recover-flash-clear).

## Looking at your data

```bash
python -m scripts.export_readings --summary-only     # quality report, writes nothing
python -m scripts.export_readings                    # two CSVs
python -m scripts.export_readings --days 7 --xlsx    # last week, plus a spreadsheet
```

Two files come out, because inspecting data and modelling data want different
shapes:

| File | What it is | Use it for |
|---|---|---|
| `readings_raw_*.csv` | one row per reading, exactly as recorded | the archive, never edit it |
| `readings_hourly_*.csv` | one row per hour, aggregated | **modelling and plotting** |

The hourly table is the one that matters. Disease models count **wet hours**, not
wet readings, so the per-minute data has to be collapsed to hours before any of it
means anything. It carries `wet_minutes`, `is_wet_hour`, hourly temperature mean,
min and max, and `local_hour` in IST so you can reason about "last night" without
doing UTC arithmetic in your head.

`--xlsx` adds a workbook with three sheets: the quality report, the hourly table,
and the raw readings, with filters and frozen headers.

### The quality report is the point

It tells you coverage percentage, the longest gap, how many rows failed
plausibility checks, and how many hours have enough readings to be trustworthy.

Check it before you fit anything. A model trained on data with an unnoticed
three-hour hole produces confident nonsense, and gaps are invisible until you go
looking for them.

## Risk readout

```bash
python -m src.ml.physics_baseline
```

Prints the infection pressure table across temperature and wet hours.

## The odds engine

The big number on the dashboard. It lives in `src/ml/odds.py`.

**What the number is.** The chance that the next 24 hours start an infection you
would later see as new spots. All five diseases together (two Alternaria spots,
downy mildew, black rot, white mould), as **one number with a range**. The 48 and
72 hour versions sit next to it.

**How it gets there.** A disease needs three things at once: the right weather, a
crop at a susceptible stage, and spores. The engine plays the next days out
1,020 times:

- 51 weather runs (the ECMWF ensemble), each corrected to this field using
  Phytoplex's own record;
- times 20 draws of the disease and leaf settings, each within what the
  literature leaves uncertain.

Each of the 1,020 is one "world". In each world the diseases add up to one
infection hazard H, and the odds in that world are 1 − exp(−H). The headline is the
average over the worlds. The **range is the 10th to 90th percentile**, so
four worlds in five land inside it.

**Levels.**

| Level | Odds |
|---|---|
| low | under 10% |
| moderate | 10% to under 25% |
| high | 25% or more |

**Wet leaves** are not read off the dew point. The engine works out, hour by hour,
whether a top leaf collects dew or rain and how fast it dries, from air temperature,
humidity, wind, cloud and sunshine. The satellite's view of how closed the canopy is
shades the leaf. Whether Phytoplex's own wetness reading is used is in
[Leaf wetness](#leaf-wetness-proxy-or-sensor).

**The crop and the spores** change how fast the odds rise:

- crop stage comes from days since transplanting (`TRANSPLANT_DATE`). With no date
  the engine weighs every stage evenly, and the page says so;
- spores follow the calendar month, are eight times higher for 28 days after the
  field log records symptoms of that disease, and are doubled for Alternaria and
  black rot when the satellite check sees the canopy falling in disease weather.

**Not yet checked against this field.** The scale is one assumption: with no disease
on the farm, one ideal wet spell for one disease gives a 1 in 10 chance. Lab
numbers say *when* the odds rise. Only your field log says *how far* they should
go here. Every visit, healthy ones included, nudges the scale towards what really
happened. Until the log holds **at least 20 judged visits and at least 3 positives**,
the page says **"not yet checked against this field"**.

- A visit is *judged* for a disease when the walk either found new spots of it or
  ruled it out. A walk that checks several diseases gives several judgments. The
  weather that counts is the weather before the visit, over the days that disease
  takes to show.
- A *positive* is a judgment that found new spots.
- Log the healthy days too. A scale fitted only on sick days cannot tell you when
  not to worry.

**Also on the page:** the wet-hour count with its range, how many of the 51 weather
runs put the odds high, "spots could show until ...", how this year's weather ranks
against the last 20 seasons (`CLIMATE_YEARS`), and last week against cabbage's best
conditions (days 15 to 24 °C, nights 4 to 15 °C).

**What it cannot do.** It cannot see spores. It cannot see a sprayed crop. It is
not a spray order.

### Alerts

When the 48 hour odds are **high** and at least half of the 51 weather runs agree,
`src/alerts.py` sends one message to your phone. After that, at most one reminder a
day while the odds stay high. The message says when the leaves are expected wet,
which disease is most likely, and, until the odds are checked against the field,
"not yet checked against this field". Set a channel in `.env`:
`ALERT_WEBHOOK_URL` (ntfy.sh, Slack or Discord), or `TELEGRAM_BOT_TOKEN` and
`TELEGRAM_CHAT_ID`. With none set, nothing is sent.

## Leaf wetness: proxy or sensor

Two readings sit side by side. They are never mixed in the stored history.

**Proxy (always).** With no leaf thermometer, leaf temperature is air temperature
minus `LEAF_TEMP_OFFSET_C` (1.0 °C by default). The reading is 100 when the leaf is
at or below the dew point and falls to 0 once it is 2 °C above. It is stored as
`leaf_wetness`, marked `leaf_wetness_source: air_proxy` and `leaf_wetness_estimated`.
Phytoplex has no working MLX90614 today, so all live wetness is this proxy.

**Calibrated sensor (once fitted).** Firmware 1.5.0, built with
`-DLEAF_WET_SENSOR=1`, sends the raw millivolts as `leaf_wet_mv`. The server cannot
read millivolts until it knows what *your* sensor says:

1. Read the sensor clean and dry, in air. Put it in `.env` as `LW_DRY_MV`.
2. Mist it until it drips and read again. Put that in `LW_WET_MV`.
3. Leave `LW_WET_THRESHOLD_PCT` at 20, or change it. It is how far along the
   dry-to-wet range counts as wet. It is low on purpose: dew covers only part of the
   surface. Check it on a dew morning against your own eyes.

It works whichever way round the sensor reads. With both numbers set, each reading
gets `leaf_wet_pct` (0 to 100) and `leaf_wet_sensor` (true or false). With either one
missing, nothing downstream uses the sensor.

**What the odds do with it.** When the sensor is calibrated, its wet hours replace
the simulated ones for the hours Phytoplex has already seen. An hour counts as wet
when at least half of its sensor readings are wet. **The forecast stays simulated.**
The proxy keeps being stored, so you can compare the two night by night.

## Outside feeds

Free data about the same field, fetched by the Pi every hour and shown on the
dashboard. No accounts, no API keys.

| Feed | Source | What it gives |
|---|---|---|
| Forecast | Open-Meteo, plus the 51-run ECMWF ensemble | infection pressure 12-72 h ahead, and how the weather runs split |
| Cross-check | ERA5-Land reanalysis, ~5 days behind | Phytoplex vs model: bias, correlation, and the days Phytoplex missed |
| Rain | ERA5 | season rain; Phytoplex only has a wet/dry sensor |
| Sunlight | NASA POWER | daily solar energy |
| Greenness | Sentinel-2 L2A via Earth Search | NDVI over the field on clear passes |
| Radar | Sentinel-1 GRD via Microsoft Planetary Computer | radar vegetation index on every pass, cloud or not |
| Unusual conditions | Phytoplex itself | the last 6 hours against the same hours over 15 days |
| Disease or stress | all of the above | points at disease only when the canopy falls while the weather favoured infection |
| Disease odds | all of the above, plus the field log | the odds engine's numbers: see [The odds engine](#the-odds-engine) |

## The jobs

Two things keep the feeds and the odds fresh. Both run on the Pi.

**`scripts/update_feeds.py`** refreshes every feed, then recomputes everything built
from them. Each job is independent. One failing (a slow server, no internet) is
recorded in the feeds status document and the others still run. It only ever reads
the readings collection.

```bash
.venv/bin/python -m scripts.update_feeds auto      # what the timer runs: only what is due
.venv/bin/python -m scripts.update_feeds all       # everything, now
.venv/bin/python -m scripts.update_feeds odds      # or any one job
```

The jobs are `forecast`, `reanalysis`, `satellite`, `radar`, `anomaly`, `check`,
`calibration`, `baseline`, `mos`, `climate`, `odds` and `alerts`. In `auto` mode each
runs when it is older than:

| Job | Runs again after |
|---|---|
| `forecast` | 3 hours |
| `reanalysis`, `satellite`, `radar` | 20 hours |
| `check`, `calibration` | 6 hours |
| `baseline` | 12 hours |
| `mos` (corrects the forecast to this field) | 24 hours |
| `climate` (20 past seasons for the rank) | 30 days |
| `anomaly`, `odds`, `alerts` | every run |

**`deploy/install_feeds.sh`** sets the above up. Safe to run again. It:

1. Asks for the field position the first time, as decimal degrees, and writes it to
   `.env`. It never goes into git, because this repo is public.
2. Installs the Python packages from `requirements.txt` (numpy is among them; the
   odds engine needs it).
3. Installs `agrisync-feeds.service` and `agrisync-feeds.timer`. The timer starts 4
   minutes after boot and then fires every hour.
4. Runs one first fill (5 to 10 minutes, about 45 satellite scenes), then restarts
   the dashboard and the API.

```bash
bash deploy/install_feeds.sh LAT LON      # first time: LAT LON is the middle of the field
bash deploy/install_feeds.sh              # later runs need no arguments
journalctl -u agrisync-feeds -n 40        # what the last run did
```

The first `climate` run makes about 20 requests to ERA5, so the "how this season
ranks" line shows up a little after the rest.

## Settings in `.env`

`cp .env.example .env` and fill in the blanks. The ones the odds engine adds:

| Key | Default | What it does |
|---|---|---|
| `TRANSPLANT_DATE` | empty | Day the crop went in, `YYYY-MM-DD`. Sets the crop stage. Empty: the first field-log visit marked transplanted is used, else every stage is weighed evenly. |
| `CLIMATE_YEARS` | `20` | How many past seasons the odds are ranked against. |
| `LEAF_TEMP_OFFSET_C` | `1.0` | Degrees subtracted from air temperature to stand in for leaf temperature. |
| `LW_DRY_MV` | empty | Leaf wetness sensor reading, dry, in millivolts. |
| `LW_WET_MV` | empty | Same sensor, misted until dripping. Both must be set or the sensor is ignored. |
| `LW_WET_THRESHOLD_PCT` | `20` | How far along dry-to-wet counts as wet. |
| `ALERT_WEBHOOK_URL`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | empty | Where odds alerts go. Any one channel is enough. |
| `ALERT_ESTIMATED` | `0` | `1` lets the old per-disease alert rule include downy mildew and black rot. |
| `DEVICE_HMAC_SECRET` | empty | Signing key shared with Phytoplex. Rotated at season end. |
| `BOX_INTERVAL_S` | `300` | Seconds between readings the Pi asks of a 1.6.0 box. |
| `BOX_WET_INTERVAL_S` | `150` | The same on a wet night (6 pm to 10 am, humidity 90%+ or leaf wet). Equal to the above switches it off. |
| `BOX_BATCH` | `12` | Queued readings per request, 1 to 12. |
| `OTA_ENABLED` | `0` | `1` offers the image published with `scripts/publish_firmware.py` to the box. |
| `FIRMWARE_DIR` | `data/firmware` | Where published images and their manifest live. Never in git. |

`ALERT_STALE_MINUTES` (the watchdog) is unchanged. The `LW_*` keys are only needed
once a leaf wetness sensor is fitted. `install_feeds.sh` also writes `FIELD_LAT`,
`FIELD_LON`, `FIELD_RADIUS_M` and `SEASON_START` into `.env`.

## Packages

`requirements.txt` includes **numpy**: the odds engine and `scripts/update_feeds.py`
import it. Satellite reading needs `rasterio`. After a `git pull`, run `pip install -r
requirements.txt` (`install_feeds.sh` does it for you).

---

## API

| Method | Route | Auth | Purpose |
|---|---|---|---|
| GET | `/health` | none | is Flask alive |
| GET | `/health/db` | none | can Flask reach Mongo |
| POST | `/api/v1/sensors/readings` | `X-Sig` or `X-API-Key` | the ESP32 posts here; 1.6.0 gets its signed settings in the reply |
| POST | `/api/v1/sensors/readings/batch` | `X-Sig` or `X-API-Key` | up to 32 queued readings as one JSON array (firmware 1.6.0) |
| GET | `/api/v1/firmware/chunk?v=&off=&len=` | `X-Sig` over the path | a piece of a published image, base64 (firmware 1.6.0) |
| GET | `/api/v1/firmware/manifest` | `X-Sig` over the path | what is on offer |
| GET | `/api/v1/sensors/readings` | `X-Read-Key`* | recent rows, `?device_id=` `?limit=` |
| GET | `/api/v1/risk/<device_id>` | `X-Read-Key`* | infection pressure curve |
| GET | `/api/v1/devices` | `X-Read-Key`* | health of every device |
| GET | `/api/v1/devices/<id>` | `X-Read-Key`* | one device in detail |

\* Read endpoints are open until you set `READ_API_KEY` in `.env`. `/health`
and `/health/db` are always open so monitoring works without credentials.
See `docs/SECURITY.md`.

Test a POST by hand before you flash anything:

```bash
curl -X POST http://localhost:5000/api/v1/sensors/readings \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: your-key-from-.env' \
  -d '{"device_id":"esp32-smoke-01","air_temp_c":25.0,"humidity_pct":80.0,"leaf_temp_c":21.0}'
```

If that returns 201, the API is fine and any later failure is on the board side.
This one command saves hours of blaming the wrong thing.

---

## Firmware

Board A flashes over its own USB. Board B has no USB and needs the FTDI programmer
with TX/RX crossed and IO0 jumpered to GND.

```bash
export WIFI_SSID='your-wifi' WIFI_PASSWORD='your-pass'
export PI_HOST='192.168.1.35' DEVICE_API_KEY='the-same-key-as-your-.env'
cd firmware/synthetic_wifi && pio run -t upload && pio device monitor
```

Secrets are passed in as environment variables and injected as build flags, so no
credential is ever committed.

The Phytoplex field build is firmware 1.6.0 over cellular (`-DTRANSPORT=2`). It also
needs `CELL_APN` and `DEVICE_HMAC_SECRET` in the environment. What 1.5.0 changed (the
queue, the clock, the serial commands) is in
[docs/FIRMWARE_1.5.0.md](docs/FIRMWARE_1.5.0.md). What 1.6.0 adds (settings from the
Pi, batches, updates over 4G, the key kept in flash, the rain fix) is in
[docs/FIRMWARE_1.6.0.md](docs/FIRMWARE_1.6.0.md). 1.6.0 is tested on a computer and
waits for a bench test before the pole.

**If Phytoplex has readings stuck in its flash, do not flash it yet.** Follow
[Season end](#season-end-dump-recover-flash-clear) first.

---

## Data routine

The model is worthless without ground truth. Every field visit, add a row at
`dashboard.agrisync.tech/admin/field-log` (admin only): date, stage, what you saw,
severity 0-5, plants checked and plants affected. **Log the healthy days too.** A
model fitted only on sick days cannot tell you when not to spray. The rows go to the
`field_log` collection; `data/field_log.csv` is the older paper-style template.

Collect the microSD images periodically and keep them dated. Note irrigation,
spraying, and weather damage, because they explain the anomalies you will find later.

---

## Still to build

- Checking the odds against this field, once the field log holds 20 judged visits and
  3 positives. The code is there; the field log has to catch up
- Direct leaf-wetness measurement: the firmware and server are ready, the sensor is
  not fitted yet
- Real literature parameters for downy mildew and black rot
- Fix Phytoplex's faults found on 22 Oct: the leaf thermometer is missing, the rain
  plate has read wet since 27 Sep, and the BME280 saturates at 100% humidity. The
  health check now names the plate and the counter; 1.6.0 debounces the counter
- Re-send of readings stuck in Phytoplex, in the field. Firmware 1.5.0 fixes this and
  1.6.0 batches it, but one of them has to be flashed first (see Season end)
- A bench test of 1.6.0 (docs/FIRMWARE_1.6.0.md, "Bench checklist") before the pole


---

## Running it unattended

`python main/app.py` dies when you close the terminal. For anything longer than a
bench test, install the systemd units:

```bash
cat deploy/README.md
```

That covers the API service, the health watchdog with phone alerts, the nightly
backup, and setting the Pi's clock. Do not put Phytoplex in the field before those
are running and you have tested a reboot.

## What the board does when the network drops

**Firmware 1.5.0.** Readings that fail to POST are written to the ESP32's flash in
64 KB segments and sent, oldest first, as soon as the link is back (Wi-Fi or
cellular). A cursor remembers where it got to, so a line is never sent twice. When
flash runs short, the oldest segment is dropped, never the whole queue. The board
copies the modem's NTP time into its own clock, so every reading says when it was
taken. Check the queue depth in any payload: `buffered_count`. Details:
[docs/FIRMWARE_1.5.0.md](docs/FIRMWARE_1.5.0.md).

**Firmware 1.4.0 (what is in the field today).** Readings that fail to POST are kept
but **not re-sent** on cellular, because the flush only ran on Wi-Fi. The board never
sets its clock, so a kept reading has no time on it. At about 1.4 MB (roughly 5,700
lines) the buffer deletes itself. Kept readings can be dumped over USB with the `D`
serial command. **Never send `C`, which clears them.**

## Season end: dump, recover, flash, clear

Phytoplex holds readings in its flash that the server never received: 3,340 on
22 Oct, including the whole week of 3 to 10 Oct. They can only be got out over USB.
Do these steps **in this order**, on the Pi, with Phytoplex plugged into it.

> **Never send `C` or `c` to Phytoplex over serial. It clears the buffer.** The one
> exception is the last step, after everything else has been checked.
>
> **Don't reflash before the buffer is dumped.**
>
> **Don't plug Phytoplex into a Pi while `agrisync-serial-bridge` is running.**

1. **Stop the serial bridge.**
   ```bash
   sudo systemctl stop agrisync-serial-bridge
   ```
   The bridge forwards every line Phytoplex prints as a live reading. Three weeks of
   old readings would all be stamped with the minute of the dump.

2. **Dump the buffer.**
   ```bash
   .venv/bin/python -m scripts.dump_buffer --port /dev/ttyUSB0     # or set SERIAL_PORT
   ```
   - **It refuses to run while `agrisync-serial-bridge` is active.** `--force`
     overrides that. Don't.
   - It sends one `D` and waits for the answer. Phytoplex only reads commands between
     cycles, so allow up to 7 minutes of quiet (`--quiet-s 420`, the default).
   - It writes `data/dumps/<device>-<UTC time>.jsonl` and a `.log` next to it. The
     first line of the `.jsonl` says how many readings the board expected and how many
     arrived.
   - **It never sends `C`, and deletes nothing.**
   - If it warns that the dump never ended, or that the counts differ, run it again.
     Keep a copy of the dump file somewhere other than the Pi.

3. **Dry-run the recovery.**
   ```bash
   .venv/bin/python -m scripts.recover_buffer data/dumps/<file>.jsonl
   ```
   It writes nothing. It prints how it would place every line. Lines are spaced
   between the readings the server did receive (each carries `buffered_count`), and a
   drop in `rain_tips` marks a reboot. Read the summary:
   - Most lines should read *placed, trusted*: their spacing is within 25% of the
     usual five-minute cycle.
   - *Placed, approximate* lines were counted from one side only, more than 36 cycles
     (about three hours) from a reading the server has. They are stored with
     `ts:approximate` and kept out of the models.
   - *Already on server* lines reached the server but the reply was lost. They are
     not stored twice.
   - *Could not place* lines need a look before you go on.
   - It checks the placed air temperatures against the outside temperatures hour by
     hour. They must follow each other (r of 0.8 or better), or `--write` refuses.
     The check is skipped when the dump covers under 24 hours.

4. **Write it.**
   ```bash
   .venv/bin/python -m scripts.recover_buffer data/dumps/<file>.jsonl --write
   ```
   It is safe to run twice: each stored reading records which dump and slot it came
   from, so nothing is stored twice. Check the dashboard: the gaps should be filled.

5. **Only then, flash 1.6.0** (or 1.5.0 if 1.6.0 has not passed its bench test).
   This should be the last cable flash: from 1.6.0 on, updates go over 4G. Build it
   on your own machine with the real environment variables (see [Firmware](#firmware)),
   including a **new** `DEVICE_HMAC_SECRET`, which 1.6.0 writes into its own flash:
   ```bash
   openssl rand -hex 32          # make a new one; keep it out of git and out of chat
   cd firmware/synthetic_wifi && pio run -t upload
   ```
   - **Don't flash a build made without your real values** (for example one built in
     a sandbox). It is a compile check only.
   - Use plain `upload`. Don't erase the flash or upload a filesystem image.
   - On its first start, 1.5.0 and later move the old buffer to `/legacy.jsonl`. It
     is kept, never sent, and `D` still prints it as `[legacy]` lines.

6. **Rotate the HMAC secret on the server.** The old one is in public git history, so
   treat it as burned. Put the same new value in `DEVICE_HMAC_SECRET` in the Pi's
   `.env`, then:
   ```bash
   sudo systemctl restart agrisync-api
   ```
   Until the two match, every POST comes back 401 and Phytoplex queues. Nothing is
   lost, and the queue empties afterwards. The camera board (`firmware/esp32cam_capture`)
   uses the same secret and needs rebuilding with the new value too.

7. **Check, then clear.** Send `?` over serial (`pio device monitor`). It should show
   `fw 1.6.0` (or `1.5.0`), the clock `set`, `key stored`, and a queue that is draining. A new reading should
   appear on the dashboard. Only when the recovery is written and checked, clear the
   old buffer: send `C`, then `Y` within 10 seconds. This deletes the queue and the
   legacy readings from the box.

## Updating the Pi

One command, on the Pi:

```bash
cd ~/agrisync && bash deploy/update.sh
```

It stops if a tracked file was edited by hand, shows the commits it is about to take,
backs up the database, fast-forwards to `origin/main`, installs the packages, checks
both apps load, refreshes the feeds timer (`deploy/install_feeds.sh`), restarts the API
and the dashboard and asks each one if it is healthy. If either is not, it goes back to
the commit it started from and restarts again. `--no-backup` and `--no-feeds` skip
those steps. Don't pull power on the Pi while it runs; Phytoplex keeps posting
throughout and its readings wait in its queue for the minute the API restarts.

Add any new keys to `.env` first (see [Settings in `.env`](#settings-in-env)).

## Reading the status LED from the ground

| Pattern | Meaning |
|---|---|
| Solid on | healthy |
| 1 blink | WiFi down |
| 2 blinks | a core sensor is missing from the I2C bus |
| 3 blinks | cannot reach the API, readings are buffering |
| 4 blinks | clock never synced, timestamps unreliable |


## Security

`docs/SECURITY.md` covers SSH keys, firewall, automatic updates, key rotation,
and setting up a Cloudflare Tunnel with Access service tokens for when Phytoplex
moves out of WiFi range and onto 4G. For rotating the device HMAC secret, see step 6
of [Season end](#season-end-dump-recover-flash-clear).

Short version of the threat that matters: someone injecting fake readings would
poison your dataset silently and permanently. Protecting writes is worth real
effort. Protecting reads is not, until the API leaves your LAN.
