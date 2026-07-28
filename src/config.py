"""Central config. Everything comes from .env so no secret is ever in git."""
import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017/agrisync")
    DEVICE_API_KEY = os.getenv("DEVICE_API_KEY", "")

    SITE_NAME = os.getenv("SITE_NAME", "Hapur")
    SITE_LAT = float(os.getenv("SITE_LAT", "28.7170"))
    SITE_LON = float(os.getenv("SITE_LON", "77.7740"))
    SITE_TZ = os.getenv("SITE_TZ", "Asia/Kolkata")

    FLASK_PORT = int(os.getenv("FLASK_PORT", "5000"))
