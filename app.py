# app.py
import os
import logging
from typing import Optional, Iterable, Dict, Any
from datetime import datetime, timezone

from flask import Flask, jsonify, request
from flask_cors import CORS
from werkzeug.middleware.proxy_fix import ProxyFix
from pymongo import MongoClient
from dotenv import load_dotenv, find_dotenv

# --------------------------------------------------------------------
# Load .env EARLY for local dev; real envs (Heroku) still win because
# override=False means existing OS env vars take precedence.
# --------------------------------------------------------------------
load_dotenv(find_dotenv(), override=False)

# --------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("cleancommute")

# --------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------
def _istrue(v: Optional[str]) -> bool:
    return (v or "").strip().lower() in {"1", "true", "yes", "y", "on"}

def _ts() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

def json_ok(data: Dict[str, Any] | None = None, status: int = 200):
    return jsonify({"ok": True, "ts": _ts(), "data": data or {}}), status

def json_err(msg: str, status: int = 400, extra: Dict[str, Any] | None = None):
    body = {"ok": False, "ts": _ts(), "error": msg}
    if extra:
        body["details"] = extra
    return jsonify(body), status

# --------------------------------------------------------------------
# Environment
# --------------------------------------------------------------------
FLASK_ENV           = os.getenv("FLASK_ENV", "production")
MONGO_URI           = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB            = os.getenv("MONGO_DB", "cleancommute")
API_KEY             = os.getenv("API_KEY", "")
FRONTEND_ORIGIN     = os.getenv("FRONTEND_ORIGIN", "")
DEFAULT_LIMITS      = os.getenv("DEFAULT_LIMITS", "100 per minute")
LIMITER_STORAGE_URI = os.getenv("LIMITER_STORAGE_URI", "memory://")
ALLOW_CLEAR         = _istrue(os.getenv("ALLOW_CLEAR"))

log.info(
    "ENV → FLASK_ENV=%s | ALLOW_CLEAR=%s | MONGO_URI=%s | MONGO_DB=%s | FRONTEND_ORIGIN=%s",
    FLASK_ENV, ALLOW_CLEAR, MONGO_URI, MONGO_DB, FRONTEND_ORIGIN
)

# --------------------------------------------------------------------
# App + CORS + Proxy fix
# --------------------------------------------------------------------
app = Flask(__name__)
app.url_map.strict_slashes = False  # avoid /path vs /path/ 404s
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)

if FRONTEND_ORIGIN:
    CORS(app, resources={r"/api/*": {"origins": [FRONTEND_ORIGIN]}})
else:
    # Dev-friendly: allow all for local until you lock it down
    CORS(app, resources={r"/api/*": {"origins": "*"}})

# --------------------------------------------------------------------
# Database
# --------------------------------------------------------------------
try:
    mongo_client = MongoClient(
        MONGO_URI,
        connectTimeoutMS=5000,
        serverSelectionTimeoutMS=5000,
        uuidRepresentation="standard",
    )
    db = mongo_client[MONGO_DB]
except Exception as e:
    log.exception("Failed creating Mongo client: %s", e)
    # Defer actual failure to when endpoints try to use `db`

# --------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------
@app.route("/api/v1/health", methods=["GET"])
def health():
    return json_ok({"service": "clean-commute-api", "env": FLASK_ENV})

@app.route("/api/v1/db-ping", methods=["GET"])
def db_ping():
    try:
        db.command("ping")  # type: ignore[name-defined]
        return json_ok({"mongo": "up"})
    except Exception as e:
        log.exception("DB ping failed")
        return json_err("db unreachable", status=500, extra={"exception": str(e)})

@app.route("/api/v1/samples", methods=["GET"])
def list_samples():
    try:
        docs: Iterable[Dict[str, Any]] = db.samples.find({}, {"_id": 0}).limit(200)  # type: ignore[name-defined]
        items = list(docs)
        return json_ok({"items": items})
    except Exception as e:
        log.exception("List samples failed")
        return json_err("list failed", status=500, extra={"exception": str(e)})

@app.route("/api/v1/samples", methods=["POST"])
def add_sample():
    # writes require x-api-key
    key = request.headers.get("x-api-key", "")
    if not API_KEY or key != API_KEY:
        return json_err("unauthorized", status=401)

    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict) or not payload:
        return json_err("missing json body", status=400)

    try:
        db.samples.insert_one(payload)  # type: ignore[name-defined]
        return json_ok({"inserted": True}, status=201)
    except Exception as e:
        log.exception("Insert failed")
        return json_err("insert failed", status=500, extra={"exception": str(e)})

@app.route("/api/v1/samples/clear", methods=["POST"])
def clear_samples():
    # writes require x-api-key
    key = request.headers.get("x-api-key", "")
    if not API_KEY or key != API_KEY:
        return json_err("unauthorized", status=401)

    if not ALLOW_CLEAR:
        return json_err("clearing disabled (set ALLOW_CLEAR=true to enable)", status=403)

    try:
        res = db.samples.delete_many({})  # type: ignore[name-defined]
        return json_ok({"deleted": res.deleted_count})
    except Exception as e:
        log.exception("Delete failed")
        return json_err("delete failed", status=500, extra={"exception": str(e)})

# Debug route to quickly see what’s registered (remove in prod if you like)
@app.route("/_routes", methods=["GET"])
def _routes():
    rules = [
        {"rule": str(r), "methods": sorted(list(r.methods - {'HEAD', 'OPTIONS'}))}
        for r in app.url_map.iter_rules()
    ]
    return jsonify(rules), 200

# --------------------------------------------------------------------
# Main entry: respect PORT (Heroku) and default to 5000 locally.
# --------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))  # 5000 locally; Heroku injects PORT
    app.run(host="0.0.0.0", port=port, debug=(FLASK_ENV != "production"))
