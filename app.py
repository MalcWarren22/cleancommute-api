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
FRONTEND_ORIGIN = os.getenv("FRONTEND_ORIGIN", "")
API_KEY = os.getenv("API_KEY", "")

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB_NAME = os.getenv("MONGO_DB", "cleancommute")

ALLOW_CLEAR = str(os.getenv("ALLOW_CLEAR", "false")).lower() == "true"
DEFAULT_LIMITS = os.getenv("DEFAULT_LIMITS", "200 per minute")
LIMITER_STORAGE_URI = os.getenv("LIMITER_STORAGE_URI", "memory://")

# ------------------------------------------------------------
# Flask app
# ------------------------------------------------------------
app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_port=1, x_prefix=1)

cors_origins: List[str] = [FRONTEND_ORIGIN] if FRONTEND_ORIGIN else ["*"]
CORS(app, resources={r"/api/*": {"origins": cors_origins}}, supports_credentials=False)

# Optional rate limiting (only if flask-limiter is installed)
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
    mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    db = mongo_client[MONGO_DB_NAME]
    mongo_client.admin.command("ping")  # warm-up ping (non-fatal if except)
except Exception as e:
    log.error("Failed creating Mongo client: %s", e)

log.info(
    "ENV → FLASK_ENV=%s | ALLOW_CLEAR=%s | MONGO_URI=%s | MONGO_DB=%s | FRONTEND_ORIGIN=%s",
    FLASK_ENV, ALLOW_CLEAR, MONGO_URI, MONGO_DB_NAME, FRONTEND_ORIGIN,
)

# ------------------------------------------------------------
# Helpers
# ------------------------------------------------------------
def require_key() -> None:
    if not API_KEY:
        abort(500, description="Server not configured with API_KEY")
    if request.headers.get("x-api-key") != API_KEY:
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
        "routes": "/_routes",
    }), 200

@app.get("/health")
def health_root():
    return jsonify({"status": "ok"}), 200

@app.get("/db-ping")
def db_ping_root():
    try:
        if not mongo_client:
            raise RuntimeError("No Mongo client")
        mongo_client.admin.command("ping")
        return jsonify({"ok": True}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# Versioned aliases
@app.get("/api/v1/health")
def health_v1():
    return health_root()

@app.get("/api/v1/db-ping")
def db_ping_v1():
    return db_ping_root()

# ------------------------------------------------------------
# Samples endpoints (smoke tests)
# ------------------------------------------------------------
@app.post("/api/v1/samples")
def add_sample():
    require_key()
    if not _mongo_ok():
        return jsonify({"error": "Database unavailable"}), 503
    payload = request.get_json(silent=True) or {}
    res = db.samples.insert_one(payload)
    return jsonify({"inserted_id": str(res.inserted_id)}), 201

@app.get("/api/v1/samples")
def list_samples():
    if not _mongo_ok():
        return jsonify({"error": "Database unavailable"}), 503
    docs = list(db.samples.find({}, {"_id": 0}))
    return jsonify(docs), 200

@app.delete("/api/v1/samples/clear")
def clear_samples():
    if not ALLOW_CLEAR:
        abort(403, description="Clearing is disabled")
    require_key()
    if not _mongo_ok():
        return jsonify({"error": "Database unavailable"}), 503
    res = db.samples.delete_many({})
    return jsonify({"deleted": res.deleted_count}), 200

# ------------------------------------------------------------
# Commutes endpoints (example resource)
# ------------------------------------------------------------
@app.post("/api/v1/commutes")
def add_commute():
    require_key()
    if not _mongo_ok():
        return jsonify({"error": "Database unavailable"}), 503
    payload = request.get_json(silent=True) or {}
    res = db.commutes.insert_one(payload)
    return jsonify({"inserted_id": str(res.inserted_id)}), 201

@app.get("/api/v1/commutes")
def list_commutes():
    if not _mongo_ok():
        return jsonify({"error": "Database unavailable"}), 503
    docs = list(db.commutes.find({}, {"_id": 0}))
    return jsonify(docs), 200

@app.delete("/api/v1/commutes/clear")
def clear_commutes():
    if not ALLOW_CLEAR:
        abort(403, description="Clearing is disabled")
    require_key()
    if not _mongo_ok():
        return jsonify({"error": "Database unavailable"}), 503
    res = db.commutes.delete_many({})
    return jsonify({"deleted": res.deleted_count}), 200

# ------------------------------------------------------------
# Introspection (optional)
# ------------------------------------------------------------
@app.get("/_routes")
def list_routes():
    rules = []
    for r in app.url_map.iter_rules():
        methods = ",".join(sorted(r.methods - {"HEAD", "OPTIONS"}))
        rules.append({"rule": str(r), "endpoint": r.endpoint, "methods": methods})
    rules.sort(key=lambda x: x["rule"])
    return jsonify(rules), 200

# ------------------------------------------------------------
# Gunicorn entrypoint
# ------------------------------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
