# AgriSync build progress

Only tick what you have actually verified. A checklist that lies to you in
December is worse than no checklist.

**Status:** node running clean, server side complete, Stage B blocked on a faulty
MLX90614 (replacement ordered). Stage C is next and needs nothing from it.

---

## Stage A — Board A on the bench  ✅ COMPLETE

- [x] Pi 5 on NVMe, booted and reachable over SSH
- [x] MongoDB running in Docker, `ping: ok 1`
- [x] Repo cloned on the Pi, venv created, requirements installed
- [x] `.env` filled in with MONGO_URI and DEVICE_API_KEY
- [x] `curl localhost:5000/health` returns ok
- [x] `curl localhost:5000/health/db` returns ok
- [x] `curl http://192.168.1.39:5000/health` works FROM THE LAPTOP
- [x] BME280 wired to Board A (3V3 / GND / GPIO21 / GPIO22)
- [x] Board A flashed, serial shows `BME280 OK @ 0x76`
- [x] Serial shows repeating `HTTP 201`
- [x] `GET /api/v1/sensors/readings` returns a real row
- [x] `ts_source: device` — board's own NTP clock stamping readings
- [x] `dew_point_c` computed server side, below air temperature

## Infrastructure — not in the manual, but required

- [x] systemd service, API survives reboot and SSH disconnect
- [x] chrony on the Pi, serving time to the board (`local stratum 10` for offline)
- [x] Firmware NTP points at the Pi, not public servers
- [x] Static IP 192.168.1.39 set on the Pi, survives reboot
- [x] DNS moved to 1.1.1.1 (NextDNS was blocking package repos)
- [x] Nightly `mongodump` in crontab, backup file confirmed on disk
- [x] Watchdog timer running every 15 minutes
- [x] Data export script: raw CSV, hourly modelling table, quality report
- [x] Store-and-forward buffering proven in anger (269 readings recovered)
- [ ] **Pi 5 RTC battery fitted** — chrony syncs, but no clock through a power cut
- [ ] Test alert confirmed received on phone (ntfy topic `agrisynctrial`)
- [ ] LED fault codes verified by pulling a sensor and counting blinks
- [ ] Deliberate router-unplug test of the buffer

## Security  ✅ COMPLETE

- [x] SSH key login working
- [x] Password authentication disabled
- [x] ufw active (22, 123 from LAN; 5000 on wlan0)
- [x] fail2ban running
- [x] unattended-upgrades installed
- [x] `DEVICE_API_KEY` rotated after exposure, board reflashed
- [x] `READ_API_KEY` set, GET endpoints require it
- [x] Cloudflare Tunnel live on `api.agrisync.tech`, no ports forwarded
- [x] Cloudflare Access: Allow policy (email) + Service Auth policy (token)
- [x] Service token created and stored
- [x] Verified: no credentials → 302 to login; token → data
- [x] Tunnel and API both survive a reboot
- [ ] `BIND_HOST=127.0.0.1` and drop port 5000 — **only after firmware moves to HTTPS**

---

## Stage B — full I²C bus  🔶 IN PROGRESS

- [x] ADS1115 added, serial shows `ADS1115 OK @ 0x48` (ADDR tied to GND)
- [ ] **MLX90614 — module faulty, no response at 0x5A, replacement ordered**
- [ ] `leaf_wetness` appears in the JSON payload
- [ ] Palm test: `leaf_temp_raw_c` jumps to ~33 °C when a hand is held in front
- [ ] `/api/v1/devices` reports `status: ok` with no problems
- [ ] Raindrop module on GPIO35 / GPIO32
- [ ] Battery divider on GPIO39 (**47 kΩ / 10 kΩ** — the firmware assumes these)
- [ ] (optional) SGP40 on 0x59

**When the replacement arrives:** four wires, press EN, done. No reflash, no code
changes. The Pi starts computing leaf wetness from the next reading.

## Soak test

- [ ] BME280 moved outdoors, out of air conditioning
- [ ] 24 hours undisturbed
- [ ] Coverage above 95%
- [ ] Longest gap under 5 minutes
- [ ] 0% suspect rows
- [ ] Temperature curve rises through the afternoon and falls overnight

---

## Stage C — Board B, the camera  ⬜ NEXT

- [ ] microSD formatted FAT32, **32 GB or smaller**
- [ ] FTDI wired: 5V→5V, GND→GND, TX→U0R, RX→U0T, jumper set to 5 V
- [ ] IO0 jumpered to GND for flashing only
- [ ] Flashed, serial shows `Camera OK` and `SD OK`
- [ ] Timestamped JPEG lands in /canopy/
- [ ] IO0 jumper removed, RST pressed, runs standalone
- [ ] Grey/white card zip-tied into frame

## Stage D — power chain  ⬜

- [ ] **W88B load mode set to 24-hour, NOT light-control** (default is dusk-to-dawn)
- [ ] Battery to controller BAT through the 3 A fuse
- [ ] Panel to PV
- [ ] Buck input to LOAD
- [ ] Buck output metered to exactly 5.0 V
- [ ] ≥1000 µF capacitor across the buck's 5 V output
- [ ] Only then: 5 V to Board A and Board B
- [ ] Battery divider wired and `battery_v` appearing in the data

## Stage E — assembly  ⬜

- [ ] Dry-fit before drilling
- [ ] Circuit moved from breadboard to perfboard and soldered
- [ ] Conformal coating sprayed and fully cured
- [ ] Glands drilled on the BOTTOM face only
- [ ] Camera lens through a sealed port
- [ ] **BME280 in a radiation shield OUTSIDE the box**
- [ ] MLX90614 outside, aimed at the canopy
- [ ] Battery strapped down, silica gel in, lid closed

## Stage F — field install  ⬜

- [ ] Pole set, enclosure in shade
- [ ] Panel facing south, tilted 25–30°
- [ ] Sensor arm at canopy height
- [ ] MLX90614 pointed down, 15–30 cm above canopy
- [ ] Camera aimed across the canopy, grey card in frame
- [ ] Status LED steady
- [ ] Readings confirmed arriving before leaving the field

## Stage F+ — cellular  ⬜

WiFi will not reach 1 km through 6–7 walls. The A7670C is the plan.

- [ ] Bench-test the modem alone with the FTDI (AT, CPIN?, CSQ, CREG?, CGATT?)
- [ ] **Antenna attached before every power-on** — transmitting into an open
      connector can destroy the amplifier
- [ ] ≥470 µF bulk capacitance at the module (board has 100 µF, SIMCOM wants 300 µF+)
- [ ] APN set (`jionet` or `airtelgprs.com`)
- [ ] Data connection confirmed, IP returned
- [ ] Modem wired to ESP32 UART2 (GPIO26 / 27)
- [ ] Firmware switched to HTTPS via `api.agrisync.tech`
- [ ] `CF-Access-Client-Id` / `CF-Access-Client-Secret` headers added
- [ ] Then, and only then: `BIND_HOST=127.0.0.1`, drop port 5000 from ufw

## Stage G — software and the data routine  ⬜

- [x] gunicorn running as a systemd service
- [x] `python -m src.ml.physics_baseline` prints a readout
- [x] `/api/v1/risk/<device_id>` returns a curve
- [x] Nightly backup scheduled
- [ ] field_log.csv routine started — **log healthy days too**
- [ ] Collect microSD images periodically and keep them dated

---

## Known limitations, stated honestly

- `leaf_wetness` is a dew-point **proxy**, not a measurement. The METER PHYTOS-31
  on ADS1115 input A0 is the real fix.
- **Downy mildew and black rot use placeholder parameters.** Only the two
  Alternaria models have sourced values. Every API response reports
  `parameters_sourced` per disease.
- Every risk response carries `calibrated: false` and will until a season of
  `field_log.csv` exists to fit against.
- The sky-temperature model assumes clear sky. No cloud sensor.
- Images stay on the SD card. No upload path yet.

## Still to build

- [ ] ESP32-CAM on-device image upload
- [ ] Builder dashboard on `dashboard.agrisync.tech`
- [ ] Conformal calibration of the risk band
- [ ] Anomaly detector that learns during the season
- [ ] Sentinel-1/2 satellite ingestion
- [ ] Real literature parameters for downy mildew and black rot
