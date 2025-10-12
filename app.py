import os
import logging
from typing import Optional

from flask import Flask, jsonify, request
from flask_cors import CORS
from dotenv import load_dotenv, find_dotenv
from pymongo import MongoClient
from werkzeug.middleware.proxy_fix import ProxyFix

# ---- Load .env EARLY (dev); real env wins in Heroku (override=False) ----
load_dotenv(find_dotenv(), override=False)

# ---- Logging ----
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("cleancommute")

# ---- Helpers ----
def _istrue(v: Optional[str]) -> bool:
    return (v or "").strip().lower() in {"1", "true", "yes", "y", "on"}

# ---- Env ----
FLASK_ENV            = os.getenv("FLASK_ENV", "production")
MONGO_URI            = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB             = os.getenv("MONGO_DB", "cleancommute")
API_KEY              = os.getenv("API_KEY", "")
FRONTEND_ORIGIN      = os.getenv("FRONTEND_ORIGIN", "")
DEFAULT_LIMITS       = os.getenv("DEFAULT_LIMITS", "100 per minute")
LIMITER_STORAGE_URI  = os.getenv("LIMITER_STORAGE_URI", "memory://")
ALLOW_CLEAR          = _istrue(os.getenv("ALLOW_CLEAR"))

log.info(
    "ENV CHECK → ALLOW_CLEAR=%s | MONGO_URI=%s | MONGO_DB=%s | FRONTEND_ORIGIN=%s | LIMITER_STORAGE_URI=%s",
    ALLOW_CLEAR, MONGO_URI, MONGO_DB, FRONTEND_ORIGIN, LIMITER_STORAGE_URI
)

# ---- App ----
app = Flask(__name__)
app.url_map.strict_slashes = False  # avoid /path vs /path/ 404s
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)

# ---- CORS (allow only your configured frontend if provided) ----
if FRONTEND_ORIGIN:
    CORS(app, resources={r"/api/*": {"origins": [FRONTEND_ORIGIN]}})
else:
    CORS(app, resources={r"/api/*": {"origins": "*"}})  # dev-friendly

# ---- DB ----
client = MongoClient(MONGO_URI, connectTimeoutMS=5000, serverSelectionTimeoutMS=5000)
db = client[MONGO_DB]

# ---- Routes ----
@app.route("/api/v1/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200

@app.route("/api/v1/db-ping", methods=["GET"])
def db_ping():
    try:
        db.command("ping")
        return jsonify({"db": "reachable"}), 200
    except Exception as e:
        log.exception("DB ping failed")
        return jsonify({"error": f"db unreachable: {e}"}), 500

@app.route("/api/v1/samples", methods=["GET"])
def list_samples():
    items = []
    for doc in db.samples.find({}, {"_id": 0}).limit(100):
        items.append(doc)
    return jsonify({"items": items}), 200

@app.route("/api/v1/samples", methods=["POST"])
def add_sample():
    # writes require x-api-key
    key = request.headers.get("x-api-key", "")
    if not API_KEY or key != API_KEY:
        return jsonify({"error": "unauthorized"}), 401

    payload = request.get_json(silent=True) or {}
    if not payload:
        return jsonify({"error": "missing json body"}), 400

    try:
        db.samples.insert_one(payload)
        return jsonify({"ok": True}), 201
    except Exception as e:
        log.exception("Insert failed")
        return jsonify({"error": f"insert failed: {e}"}), 500

@app.route("/api/v1/samples/clear", methods=["POST"])
def clear_samples():
    # writes require x-api-key
    key = request.headers.get("x-api-key", "")
    if not API_KEY or key != API_KEY:
        return jsonify({"error": "unauthorized"}), 401

    if not ALLOW_CLEAR:
        return jsonify({"error": "clears disabled (ALLOW_CLEAR=false)"}), 403

    try:
        res = db.samples.delete_many({})
        return jsonify({"ok": True, "deleted": res.deleted_count}), 200
    except Exception as e:
        log.exception("Delete failed")
        return jsonify({"error": f"delete failed: {e}"}), 500

# Optional: quick route list for debugging 404s (comment out in prod)
@app.route("/_routes")
def _routes():
    rules = [
        {"rule": str(r), "methods": sorted(list(r.methods - {'HEAD', 'OPTIONS'}))}
        for r in app.url_map.iter_rules()
    ]
    return jsonify(rules), 200

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=(FLASK_ENV != "production"))
