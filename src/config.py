"""Central config. Everything comes from .env so no secret is ever in git."""
import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017/agrisync")
    DEVICE_API_KEY = os.getenv("DEVICE_API_KEY", "")
    # Optional. When empty, GET endpoints are open. Set it before you expose
    # the API through a tunnel or a port forward.
    READ_API_KEY = os.getenv("READ_API_KEY", "")

    # 0.0.0.0 accepts connections from anywhere on the network, which the
    # ESP32 needs over WiFi. Once cloudflared fronts the app, change this to
    # 127.0.0.1 so the tunnel is the ONLY way in and nothing on your LAN can
    # reach the API directly.
    BIND_HOST = os.getenv("BIND_HOST", "0.0.0.0")

    # A reading is about 300 bytes. Anything near this is abuse or a bug.
    MAX_CONTENT_LENGTH = 16 * 1024

    SITE_NAME = os.getenv("SITE_NAME", "Hapur")
    SITE_LAT = float(os.getenv("SITE_LAT", "28.7170"))
    SITE_LON = float(os.getenv("SITE_LON", "77.7740"))
    SITE_TZ = os.getenv("SITE_TZ", "Asia/Kolkata")

    FLASK_PORT = int(os.getenv("FLASK_PORT", "5000"))
