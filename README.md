# AgriSync

Solar-powered field box logging cabbage disease conditions in Hapur, U.P.
Two ESP32 boards, a Raspberry Pi 5, and a physics-based infection-pressure model.

Site: 28.7170, 77.7740 · Crop: cabbage (rabi) · Go-live: transplant, Aug 2026

---

## What is real and what is not

Read this before you trust any number this system produces.

**Real:** air temperature, humidity, pressure (BME280), leaf surface temperature
(MLX90614), rain wet/dry, canopy images.

**Derived, not measured:** `leaf_wetness`. There is no leaf wetness sensor in this
build. The value is computed by comparing leaf temperature against the dew point.
It is a proxy and it is the honest weak point of the project. A METER PHYTOS-31 on
the ADS1115 A0 input is the drop-in upgrade.

**Corrected, with assumptions:** `leaf_temp_c`. The MLX90614 assumes the leaf is a
perfect blackbody. It is not, and what it reflects is the sky, which on a clear
night radiates far colder than the air. The server corrects for this using a
modelled sky temperature. `leaf_temp_raw_c` is always stored alongside, so the
correction can be improved and re-applied to the entire history without touching
the hardware. See `src/ml/leaf_temp.py`.

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
deploy/                      systemd units, see deploy/README.md
src/ml/physics_baseline.py   disease infection pressure models
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

38 tests, no hardware and no database needed. They check the dew point math, the
disease model shape, and every API route including auth rejection.

## Risk readout

```bash
python -m src.ml.physics_baseline
```

Prints the infection pressure table across temperature and wet hours.

---

## API

| Method | Route | Auth | Purpose |
|---|---|---|---|
| GET | `/health` | none | is Flask alive |
| GET | `/health/db` | none | can Flask reach Mongo |
| POST | `/api/v1/sensors/readings` | `X-API-Key` | the ESP32 posts here |
| GET | `/api/v1/sensors/readings` | none | recent rows, `?device_id=` `?limit=` |
| GET | `/api/v1/risk/<device_id>` | none | infection pressure curve |
| GET | `/api/v1/devices` | none | health of every device |
| GET | `/api/v1/devices/<id>` | none | one device in detail |

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

The model is worthless without ground truth. Every field visit, add a row to
`data/field_log.csv`: date, stage, healthy or first-lesion or spread. **Log the healthy
days too.** A model fitted only on sick days cannot tell you when not to spray.

Collect the microSD images periodically and keep them dated. Note irrigation,
spraying, and weather damage, because they explain the anomalies you will find later.

---

## Still to build

- ESP32-CAM on-device image upload
- Builder dashboard (risk plot)
- Conformal calibration of the risk band
- Anomaly detector that learns during the season
- Sentinel-1/2 satellite ingestion
- Real literature parameters for downy mildew and black rot


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

Readings that fail to POST are written to the ESP32's flash and retried when the
link returns, with their original timestamps preserved via NTP. A router outage
during a wet night costs you nothing. Check the queue depth in any payload:
`buffered_count`.

## Reading the status LED from the ground

| Pattern | Meaning |
|---|---|
| Solid on | healthy |
| 1 blink | WiFi down |
| 2 blinks | a core sensor is missing from the I2C bus |
| 3 blinks | cannot reach the API, readings are buffering |
| 4 blinks | clock never synced, timestamps unreliable |
