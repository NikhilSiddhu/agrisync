# AgriSync

Solar-powered field box logging cabbage disease conditions in Hapur, U.P.
Two ESP32 boards, a Raspberry Pi 5, and a physics-based infection-pressure model.

Site: 28.7170, 77.7740 · Crop: cabbage (rabi) · Go-live: transplant, Aug 2026

---

## What is real and what is not

Read this before you trust any number this system produces.

**Real:** air temperature, humidity, pressure (BME280), rain wet/dry, canopy images.

**Derived, not measured:** `leaf_wetness`. There is no leaf wetness sensor in this
build, and the infrared leaf thermometer (MLX90614) never read reliably and was
removed. Leaf temperature is estimated as air temperature minus 1 °C, and a reading
counts as wet when that is at or below the dew point. It is a proxy and it is the
honest weak point of the project. A METER PHYTOS-31 on the ADS1115 A0 input is the
drop-in upgrade. `src/ml/leaf_temp.py` (the sky-temperature correction for the
infrared sensor) stays in the repo for when a working one goes back in.

**Modelled, not measured:** everything from outside the box. Rain comes from the
ERA5 reanalysis, sunlight from NASA POWER, the next three days from a weather
forecast. The dashboard labels each one with its source.

**Not calibrated:** every risk number. The API always returns `calibrated: false`.
The disease models are a physics floor, not probabilities. Two of the four still use
placeholder parameters that need literature values. Do not make spray decisions from
this output.

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
src/feeds/sources.py         Open-Meteo, ERA5-Land, ERA5, NASA POWER, Sentinel-2, Sentinel-1
scripts/update_feeds.py      refreshes every outside feed, run hourly by a timer
src/api/fieldlog.py          /admin/field-log, what the plants actually did
src/weather/forecast.py      Open-Meteo forecast fetch (no API key needed)
firmware/synthetic_wifi/     Board A - ESP32 sensor node
firmware/esp32cam_capture/   Board B - ESP32-CAM
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

About 200 tests, no hardware, no database and no internet needed. They check the
dew point math, the disease model shape, every API route including auth rejection,
and every outside feed with its source replaced by a fake.

## Bench mode: USB instead of WiFi

The board is powered from the Pi's USB anyway. On the bench that cable is a far
more reliable path than WiFi, and WiFi never ships — the field box runs on 4G
through the tunnel.

Flash with `-DUSE_WIFI_POST=0` in `platformio.ini`, then run the bridge:

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

Set `USE_WIFI_POST` back to 1, or switch to the A7670C, before the box leaves
your desk.

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

## Outside feeds

Free data about the same field, fetched by the Pi every hour and shown on the
dashboard. No accounts, no API keys.

| Feed | Source | What it gives |
|---|---|---|
| Forecast | Open-Meteo, plus the 51-run ECMWF ensemble | infection pressure 12-72 h ahead, and how the weather runs split |
| Cross-check | ERA5-Land reanalysis, ~5 days behind | station vs model: bias, correlation, and the days the station missed |
| Rain | ERA5 | season rain; the box only has a wet/dry sensor |
| Sunlight | NASA POWER | daily solar energy |
| Greenness | Sentinel-2 L2A via Earth Search | NDVI over the field on clear passes |
| Radar | Sentinel-1 GRD via Microsoft Planetary Computer | radar vegetation index on every pass, cloud or not |
| Unusual conditions | the station itself | the last 6 hours against the same hours over 15 days |
| Disease or stress | all of the above | points at disease only when the canopy falls while the weather favoured infection |

Install once on the Pi, with the middle of the field in decimal degrees (it goes
into `.env`, never into git):

```bash
bash deploy/install_feeds.sh LAT LON
python -m scripts.update_feeds all        # everything now; "auto" is what the timer runs
journalctl -u agrisync-feeds -n 40        # what the last run did
```

One feed failing (a slow server, no internet) is recorded and the others still run.

---

## API

| Method | Route | Auth | Purpose |
|---|---|---|---|
| GET | `/health` | none | is Flask alive |
| GET | `/health/db` | none | can Flask reach Mongo |
| POST | `/api/v1/sensors/readings` | `X-API-Key` | the ESP32 posts here |
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

- Firmware: re-send buffered readings over cellular (today only Wi-Fi flushes the buffer)
- Conformal calibration of the risk band, once the field log holds a season of outcomes
- A learned healthy-field model across sensor and satellite signals (today's
  unusual-conditions check is a per-hour median and spread, not a trained model)
- Direct leaf-wetness measurement
- Real literature parameters for downy mildew and black rot
- Alerts, after calibration


---

## Running it unattended

`python main/app.py` dies when you close the terminal. For anything longer than a
bench test, install the systemd units:

```bash
cat deploy/README.md
```

That covers the API service, the health watchdog with phone alerts, the nightly
backup, and setting the Pi's clock. Do not put the box in the field before those
are running and you have tested a reboot.

## What the board does when the network drops

Readings that fail to POST are written to the ESP32's flash. On Wi-Fi they are
retried when the link returns. On cellular (firmware 1.4.0, the field box) they are
kept but **not yet re-sent**: the flush only runs on Wi-Fi, and the box never sets
its clock, so the server stamps each reading on arrival. Kept readings can be dumped
over USB with the `D` serial command; never send `C`, which clears them. Check the
queue depth in any payload: `buffered_count`. The flash holds roughly 5,700 lines
before the oldest half is dropped.

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
and setting up a Cloudflare Tunnel with Access service tokens for when the box
moves out of WiFi range and onto 4G.

Short version of the threat that matters: someone injecting fake readings would
poison your dataset silently and permanently. Protecting writes is worth real
effort. Protecting reads is not, until the API leaves your LAN.
