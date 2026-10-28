# What the system is

Phytoplex, the AgriSync field unit, measures the microclimate a cabbage leaf actually experiences,
and turns it into a disease infection pressure estimate.

## The chain

```
sensors -> ESP32 -> WiFi -> Flask API -> MongoDB
                                  |
                       Open-Meteo forecast
                                  |
                        physics_baseline model
                                  |
                       /api/v1/risk/<device_id>
```

## Why leaf temperature and not just air temperature

Fungal infection needs free water on the leaf surface. Water appears when the leaf
surface drops to or below the dew point of the surrounding air. Air temperature alone
cannot tell you that, because a leaf radiating heat to a clear night sky can sit
several degrees below the air around it.

So Phytoplex measures air temperature and humidity (BME280) to get the dew point, and
leaf surface temperature (MLX90614) with an infrared sensor that does not touch the
leaf. The gap between them is the signal.

## The leaf wetness proxy, and its limits

`leaf_wetness` runs 0 to 100. It is 100 when the leaf is at or below the dew point,
and falls to 0 once the leaf is 2 C above it.

This is a modelled value, not a measurement. It will be wrong in specific ways:

- **Rain and irrigation.** Water arriving from above wets the leaf regardless of dew
  point. The raindrop module partly covers this, but it is crude.
- **Drying time.** Real leaves stay wet after conditions change. The proxy snaps
  instantly.
- **One leaf, one angle.** The MLX90614 sees a cone of canopy, not the whole plant.
  Shaded interior leaves stay wet much longer than the sunlit top the sensor sees.

The upgrade path is a METER PHYTOS-31 dielectric leaf wetness sensor on the ADS1115
A0 input. Until that is fitted, treat wet hours as an indicator, not a measurement.

## The disease models

Four models, each a product of two response curves:

- **Temperature response:** a cardinal-temperature beta curve. Zero below the minimum,
  peak at the optimum, zero above the maximum.
- **Wetness response:** a ramp. Zero below the minimum wet hours, saturating at a
  higher value.

Both must be satisfied. A warm dry day scores zero and so does a cold wet one.

| Model | Parameters sourced | Status |
|---|---|---|
| Alternaria brassicae | yes | ~4 h minimum wetness at ~18 C, severity rising to ~12 h |
| Alternaria brassicicola | yes | germination and development roughly 28-31 C |
| Downy mildew | **no** | placeholder shape, cool and wet. Needs real parameters |
| Black rot | **no** | placeholder shape, warm and wet. Needs real parameters |

The two placeholders are clearly flagged in `physics_baseline.py` and every API
response reports `parameters_sourced` per disease. Fix them before anyone acts on
the output.

## Why calibrated is always false

The model outputs pressure, not probability. Converting one to the other requires
observations: a season of `data/field_log.csv` rows recording what actually happened,
fitted against what the model predicted. Until that exists, a number like 0.75 means
"conditions were three quarters of the way to ideal for this pathogen", not "75 %
chance of infection".

Conformal calibration of the band is on the still-to-build list for exactly this
reason.

## Known gaps

- Images stay on the SD card. No upload path yet.
- WiFi only. Out of range means the parked A7670C 4G modem becomes necessary.
- No RTC on the camera board, so image filenames use a counter and uptime rather
  than wall-clock time.
- Soil sensor is secondary and can be dropped if it delays go-live.
