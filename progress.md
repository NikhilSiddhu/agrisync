# AgriSync build progress

Tick these off as the manual's stages pass. Do not skip ahead.

## Stage A - Board A on the bench
- [x] Pi 5 on NVMe, booted and reachable over SSH
- [x] MongoDB running in Docker, `ping: ok 1`
- [x] Repo cloned on the Pi, venv created, requirements installed
- [x] `.env` filled in with MONGO_URI and DEVICE_API_KEY
- [x] `curl localhost:5000/health` returns ok
- [x] `curl localhost:5000/health/db` returns ok
- [x] `curl http://<pi-ip>:5000/health` works FROM THE LAPTOP (proves 0.0.0.0 binding)
- [x] BME280 wired to Board A (3V3 / GND / GPIO21 / GPIO22)
- [x] Board A flashed, serial shows `BME280 OK`
- [x] Serial shows repeating `HTTP 200`
- [x] `GET /api/v1/sensors/readings` returns a real row

## Hardening before the field (do NOT skip)
- [ ] systemd service installed, API survives a reboot
- [ ] Pi clock synced, RTC battery fitted (`timedatectl`, `hwclock -r`)
- [ ] Watchdog timer running, test alert received on your phone
- [ ] Nightly backup in crontab, first backup file confirmed on disk
- [ ] Firmware buffering tested: unplug the router, confirm readings backfill
- [ ] LED fault codes verified (pull a sensor, count the blinks)

## Stage B - full I2C bus
- [ ] MLX90614 added, serial shows `MLX90614 OK @ 0x5A`
- [ ] ADS1115 added, serial shows `ADS1115 OK @ 0x48`
- [ ] `leaf_wetness` appears in the JSON payload
- [ ] Raindrop module on GPIO35 / GPIO32
- [ ] (optional) SGP40 on 0x59

## Stage C - Board B, the camera
- [ ] microSD formatted FAT32, 32 GB or smaller
- [ ] FTDI wired, jumper set to 5 V, IO0 to GND
- [ ] Flashed, serial shows `Camera OK` and `SD OK`
- [ ] Timestamped JPEG lands in /canopy/
- [ ] Grey/white card zip-tied into frame

## Stage D - power chain
- [ ] Battery to controller BAT through the 3 A fuse
- [ ] Panel to PV
- [ ] Buck input to LOAD
- [ ] Buck output metered to exactly 5.0 V
- [ ] 1000 uF capacitor soldered across the 5 V output
- [ ] Only now: 5 V to Board A and Board B

## Stage E - assembly
- [ ] Dry-fit before drilling
- [ ] Circuit moved from breadboard to perfboard and soldered
- [ ] Conformal coating sprayed and fully cured
- [ ] Glands drilled on the BOTTOM face only
- [ ] Camera lens through a sealed port
- [ ] BME280 in a radiation shield OUTSIDE the box
- [ ] Battery strapped down, silica gel in, lid closed

## Stage F - field install
- [ ] Pole set, enclosure in shade
- [ ] Panel facing south, tilted 25-30 degrees
- [ ] Sensor arm at canopy height
- [ ] MLX90614 pointed down, 15-30 cm above canopy
- [ ] Camera aimed across the canopy, grey card in frame
- [ ] Status LED steady
- [ ] Readings confirmed arriving before leaving the field

## Stage G - software
- [ ] gunicorn running as a systemd service
- [ ] `python -m src.ml.physics_baseline` prints a readout
- [ ] `/api/v1/risk/<device_id>` returns a curve
- [ ] Nightly mongodump backup scheduled
- [ ] field_log.csv routine started

## Still to build (from the manual)
- [ ] ESP32-CAM on-device image upload
- [ ] Builder dashboard (risk plot)
- [ ] Conformal calibration of the band
- [ ] Anomaly detector
- [ ] Sentinel-1/2 satellite ingestion
