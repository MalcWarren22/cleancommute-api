import os
import logging
from typing import List
from flask import Flask, jsonify, request, abort
from flask_cors import CORS
from pymongo import MongoClient
from werkzeug.middleware.proxy_fix import ProxyFix
import sentry_sdk  # NEW
from sentry_sdk.integrations.flask import FlaskIntegration  # NEW
from datetime import datetime, timezone  # NEW

# ------------------------------------------------------------
# Sentry setup (must come before app init)
# ------------------------------------------------------------
SENTRY_DSN = os.getenv(
    "SENTRY_DSN",
    "https://91ab82ceac9b6f932faa7c8b77369dfb@o4510193691852800.ingest.us.sentry.io/4510193702076416"
)
if SENTRY_DSN:
    sentry_sdk.init(
        dsn=SENTRY_DSN,
        integrations=[FlaskIntegration()],
        traces_sample_rate=1.0,
        send_default_pii=True,
    )

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

    # ---- Commutes indexes for faster demos (idempotent) ----
    try:
        db.commutes.create_index([("created_at", 1)])
        db.commutes.create_index([("kind", 1)])          # "estimate" | "plan" | "compare"
        db.commutes.create_index([("mode", 1)])
        log.info("Commutes indexes ensured.")
    except Exception as idx_e:
        log.warning("Could not ensure indexes: %s", idx_e)
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

def _now_utc():  # NEW helper
    return datetime.now(timezone.utc)

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
# CO₂ Estimate / Plan / Compare (presentation-ready)
# ------------------------------------------------------------
from emissions import estimate_emissions  # uses your existing module

@app.post("/api/v1/commutes/estimate")
def co2_estimate():
    """
    Estimate CO2e for a single trip.
    Body JSON:
    {
      "mode": "car"|"car_gas"|"car_hybrid"|"rideshare"|"bus"|"train"|"subway"|"bike"|"walk",
      "distance_km": 12.5,
      "passengers": 1            # optional (applies to car-like modes)
    }
    """
    require_key()
    if not _mongo_ok():
        return jsonify({"error": "Database unavailable"}), 503

    payload = request.get_json(silent=True) or {}
    mode = (payload.get("mode") or "car").lower()
    distance_km = payload.get("distance_km")
    passengers = payload.get("passengers", 1)

    if distance_km is None:
        abort(400, description="distance_km is required")

    try:
        est = estimate_emissions(float(distance_km), mode, passengers=int(passengers))
    except Exception as e:
        abort(400, description=str(e))

    doc = {
        "kind": "estimate",
        "mode": est["mode"],
        "distance_km": float(distance_km),
        "passengers": est["passengers"],
        "estimated": est["kgCO2e"],
        "factor_kg_per_km": est["factor_kg_per_km"],
        "created_at": _now_utc(),
    }
    db.commutes.insert_one(doc)

    return jsonify({
        "ok": True,
        "data": {
            "mode": est["mode"],
            "distance_km": float(distance_km),
            "passengers": est["passengers"],
            "estimated_co2_kg": est["kgCO2e"],
            "factor_kg_per_km": est["factor_kg_per_km"],
        }
    }), 201


@app.post("/api/v1/commutes/plan")
def co2_plan():
    """
    Simple multi-day plan (same distance each day).
    Body JSON:
    {
      "mode": "car_hybrid",
      "distance_km_per_day": 35,
      "days": 7,
      "passengers": 1
    }
    """
    require_key()
    if not _mongo_ok():
        return jsonify({"error": "Database unavailable"}), 503

    payload = request.get_json(silent=True) or {}
    mode = (payload.get("mode") or "car").lower()
    distance_per_day = payload.get("distance_km_per_day")
    days = int(payload.get("days", 1))
    passengers = int(payload.get("passengers", 1))

    if distance_per_day is None:
        abort(400, description="distance_km_per_day is required")
    if days < 1:
        days = 1

    try:
        per_day = estimate_emissions(float(distance_per_day), mode, passengers=passengers)
        total = round(per_day["kgCO2e"] * days, 4)
    except Exception as e:
        abort(400, description=str(e))

    doc = {
        "kind": "plan",
        "mode": per_day["mode"],
        "distance_km_per_day": float(distance_per_day),
        "days": days,
        "passengers": per_day["passengers"],
        "per_day_kg": per_day["kgCO2e"],
        "total_kg": total,
        "factor_kg_per_km": per_day["factor_kg_per_km"],
        "created_at": _now_utc(),
    }
    db.commutes.insert_one(doc)

    return jsonify({"ok": True, "data": {
        "mode": per_day["mode"],
        "distance_km_per_day": float(distance_per_day),
        "days": days,
        "passengers": per_day["passengers"],
        "per_day_kg": per_day["kgCO2e"],
        "total_kg": total,
        "factor_kg_per_km": per_day["factor_kg_per_km"],
    }}), 201


@app.get("/api/v1/commutes/compare")
def co2_compare():
    """
    Compare multiple modes on the same distance (ranked, lowest first).
    Query: ?distance_km=12.5&modes=car,car_hybrid,rideshare,bus,train,subway,bike,walk&passengers=1
    (unknown modes default to 'car' factor per your emissions.py rules)
    """
    require_key()
    if not _mongo_ok():
        return jsonify({"error": "Database unavailable"}), 503

    distance_km = request.args.get("distance_km", type=float)
    modes_csv = request.args.get("modes", type=str)
    passengers = request.args.get("passengers", default=1, type=int)

    if distance_km is None or not modes_csv:
        abort(400, description="distance_km and modes (csv) are required")

    modes = [m.strip().lower() for m in modes_csv.split(",") if m.strip()]
    results = []
    for m in modes:
        try:
            est = estimate_emissions(distance_km, m, passengers=passengers)
            results.append({
                "mode": est["mode"],
                "distance_km": distance_km,
                "passengers": est["passengers"],
                "estimated_co2_kg": est["kgCO2e"],
                "factor_kg_per_km": est["factor_kg_per_km"],
            })
        except Exception as e:
            results.append({"mode": m, "error": str(e)})

    # Persist snapshot for audit/demo
    db.commutes.insert_one({
        "kind": "compare",
        "distance_km": distance_km,
        "modes": modes,
        "passengers": passengers,
        "results": results,
        "created_at": _now_utc(),
    })

    ranked = sorted(
        [r for r in results if "estimated_co2_kg" in r],
        key=lambda x: x["estimated_co2_kg"]
    )

    return jsonify({"ok": True, "data": {"ranked": ranked, "raw": results}}), 200

# ------------------------------------------------------------
# Test route (Sentry verification, gated for safety)
# ------------------------------------------------------------
@app.get("/api/v1/test-error")
def test_error():
    # Only allow in staging and with admin key
    if os.getenv("SENTRY_ENV") != "staging":
        abort(404)
    if request.headers.get("x-admin-key") != os.getenv("ADMIN_KEY"):
        abort(403)
    1 / 0  # intentionally trigger an error
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

