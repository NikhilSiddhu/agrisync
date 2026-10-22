"""Outside data the station is checked against and forecast from.

Every source here is free and needs no account: Open-Meteo (forecast,
ensemble, ERA5-Land reanalysis), NASA POWER and Sentinel-2 via the Earth
Search STAC catalogue on AWS open data. Nothing in this package writes to
the database; scripts/update_feeds.py does that.
"""
