# app.py
import os
import logging
from typing import List
from flask import Flask, jsonify, request, abort
from flask_cors import CORS
from pymongo import MongoClient
from werkzeug.middleware.proxy_fix import ProxyFix

# ------------------------------------------------------------
# Config & logging
# ------------------------------------------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL)
log = logging.getLogger("cleancommute")

FLASK_ENV = os.getenv("FLASK_ENV", "production")
FRONTEND_ORIGIN = os.getenv("FRONTEND_ORIGIN", "")  # e.g., https://staging.your-frontend.tld
API_KEY = os.getenv("API_KEY", "")

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB_NAME = os.getenv("MONGO_DB", "cleancommute")

# Optional feature flags / limits
ALLOW_CLEAR = str(os.getenv("ALLOW_CLEAR", "false")).lower() == "true"
DEFAULT_LIMITS = os.getenv("DEFAULT_LIMITS", "200 per minute")
LIMITER_STORAGE_URI = os.getenv("LIMITER_STORAGE_URI", "memory://")

# ------------------------------------------------------------
# Flask app
# ------------------------------------------------------------
app = Flask(__name__)
# Trust X-Forwarded-* headers on Heroku
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_port=1, x_prefix=1)

# CORS: keep tight to a single configured origin (add more if you need)
cors_origins: List[str] = [FRONTEND_ORIGIN] if FRONTEND_ORIGIN else ["*"]
CORS(app, resources={r"/api/*": {"origins": cors_origins}}, supports_credentials=False)

# (Optional) Rate limiting — only if Flask-Limiter is installed
try:
    from flask_limiter import Limiter
    from flask_limiter.util import get_remote_address

    limiter = Limiter(
        key_func=get_remote_address,
        app=app,
        default_limits=[DEFAULT_LIMITS] if DEFAULT_LIMITS else None,
        storage_uri=LIMITER_STORAGE_URI,
    )
    log.info("Rate limiting enabled with %s via %s", DEFAULT_LIMITS, LIMITER_STORAGE_URI)
except Exception as e:
    limiter = None
    log.info("Rate limiting not enabled (%s). Continuing without limiter.", e)

# ------------------------------------------------------------
# Mongo client
# ------------------------------------------------------------
mongo_client = None
db = None
try:
    # Short-ish timeout so startup doesn't hang forever if DNS/network is off
    mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    db = mongo_client[MONGO_DB_NAME]
    # Try a ping (non-fatal if it fails; routes will surface errors)
    mongo_client.admin.command("ping")
except Exception as e:
    log.error("Failed creating Mongo client: %s", e)

# Helpful startup log
log.info(
    "ENV → FLASK_ENV=%s | ALLOW_CLEAR=%s | MONGO_URI=%s | MONGO_DB=%s | FRONTEND_ORIGIN=%s",
    FLASK_ENV,
    ALLOW_CLEAR,
    # Safe to log full URI for dev; redact if you prefer
    MONGO_URI,
    MONGO_DB_NAME,
    FRONTEND_ORIGIN,
)

# ------------------------------------------------------------
# Helpers
# ------------------------------------------------------------
def require_key() -> None:
    """Simple header-based API key check for write endpoints."""
    if not API_KEY:
        abort(500, description="Server not configured with API_KEY")
    key = request.headers.get("x-api-key")
    if key != API_KEY:
        abort(401, description="Unauthorized")

def _mongo_ok() -> bool:
    try:
        if not mongo_client:
            return False
        mongo_client.admin.command("ping")
        return True
    except Exception:
        return False

# ------------------------------------------------------------
# Health / debug routes
# ------------------------------------------------------------
@app.get("/")
def root():
    return jsonify({
        "name": "CleanCommute API",
        "version": "v1",
        "health": "/health",
        "db_ping": "/db-ping",
        "docs": "/_routes"
    }), 200

@app.get("/health")
def health_root():
    return jsonify({"status": "ok"}), 200

@app.get("/db-ping")
def db_ping_root():
    try:
        if
