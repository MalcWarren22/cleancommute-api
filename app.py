# app.py
from __future__ import annotations

import os
import math
import logging
from datetime import datetime
from typing import List, Optional

import requests
from flask import Flask, jsonify, request, abort
from flask_cors import CORS
from pymongo import MongoClient
from werkzeug.middleware.proxy_fix import ProxyFix

import sentry_sdk
from sentry_sdk.integrations.flask import FlaskIntegration

# Local factors/estimator
from emissions import estimate_emissions, _FACTORS  # noqa: F401  (factors are imported for completeness)

# ──────────────────────────────────────────────────────────────────────────────
# Load .env for local development (no effect in production)
# ──────────────────────────────────────────────────────────────────────────────
try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv()
except Exception:
    pass

# ──────────────────────────────────────────────────────────────────────────────
# Sentry setup
# ──────────────────────────────────────────────────────────────────────────────
SENTRY_DSN = os.getenv(
    "SENTRY_DSN",
    "https://91ab82ceac9b6f932faa7c8b77369dfb@o4510193691852800.ingest.us.sentry.io/4510193702076416",
)
if SENTRY_DSN:
    sentry_sdk.init(
        dsn=SENTRY_DSN,
        integrations=[FlaskIntegration()],
        traces_sample_rate=1.0,
        send_default_pii=True,
    )

# ──────────────────────────────────────────────────────────────────────────────
# Config & logging
# ──────────────────────────────────────────────────────────────────────────────
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

# Google Maps Platform server-side key (Directions/Geocoding/Places)
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
if not GOOGLE_API_KEY:
    log.error("GOOGLE_API_KEY not set in environment. Google routing will be disabled.")

# ──────────────────────────────────────────────────────────────────────────────
# Flask app setup
# ──────────────────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_port=1, x_prefix=1)

cors_origins: List[str] = [FRONTEND_ORIGIN] if FRONTEND_ORIGIN else ["*"]
CORS(app, resources={r"/api/*": {"origins": cors_origins}}, supports_credentials=False)

# Optional rate limiter
try:
    from flask_limiter import Limiter  # type: ignore
    from flask_limiter.util import get_remote_address  # type: ignore

    limiter = Limiter(
        key_func=get_remote_address,
        app=app,
        default_limits=[DEFAULT_LIMITS] if DEFAULT_LIMITS else None,
        storage_uri=LIMITER_STORAGE_URI,
    )
    log.info("Rate limiting enabled with %s via %s", DEFAULT_LIMITS, LIMITER_STORAGE_URI)
except Exception as e:
    limiter = None  # type: ignore
    log.info("Rate limiting not enabled (%s). Continuing without limiter.", e)

# ──────────────────────────────────────────────────────────────────────────────
# Mongo client
# ──────────────────────────────────────────────────────────────────────────────
mongo_client = None
db = None
try:
    mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    db = mongo_client[MONGO_DB_NAME]
    mongo_client.admin.command("ping")
except Exception as e:
    log.error("Failed creating Mongo client: %s", e)

# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────
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

# ----------------------------- Google helpers ------------------------------
def _haversine_km(a: tuple[float, float] | None, b: tuple[float, float] | None) -> float:
    if not a or not b:
        return 0.0
    lat1, lon1 = map(math.radians, a)
    lat2, lon2 = map(math.radians, b)
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    R = 6371.0088
    h = (math.sin(dlat/2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon/2) ** 2)
    return 2 * R * math.asin(math.sqrt(h))

def _directions(origin_str: str, dest_str: str, mode: str, *, transit_mode: Optional[str] = None) -> dict:
    """
    Call Google Directions. Returns:
      { ok, distance_km, duration_min, polyline }
    """
    if not GOOGLE_API_KEY:
        return {"ok": False, "distance_km": None, "duration_min": None, "polyline": None}

    url = "https://maps.googleapis.com/maps/api/directions/json"
    params = {
        "origin": origin_str,
        "destination": dest_str,
        "mode": mode,  # driving | transit | bicycling | walking
        "alternatives": "false",
        "key": GOOGLE_API_KEY,
    }
    if mode in ("driving", "transit"):
        params["departure_time"] = "now"
    if mode == "driving":
        params["traffic_model"] = "best_guess"
    if mode == "transit" and transit_mode:
        params["transit_mode"] = transit_mode  # bus | rail | subway

    try:
        r = requests.get(url, params=params, timeout=10)
        data = r.json()
        if data.get("status") != "OK":
            return {"ok": False, "distance_km": None, "duration_min": None, "polyline": None}

        route = data["routes"][0]
        leg = route["legs"][0]
        dist_m = leg["distance"]["value"]
        sec = (leg.get("duration_in_traffic") or leg["duration"])["value"]
        poly = route.get("overview_polyline", {}).get("points")

        return {
            "ok": True,
            "distance_km": round(dist_m / 1000.0, 3),
            "duration_min": round(sec / 60.0, 1),
            "polyline": poly,
        }
    except Exception as e:
        log.warning("Directions error (%s): %s", mode, e)
        return {"ok": False, "distance_km": None, "duration_min": None, "polyline": None}

def _offline_time_min(distance_km: float, mode: str) -> float:
    SPEED_KMH = {
        "car": 35.0, "car_hybrid": 35.0,
        "bus": 18.0, "train": 45.0, "subway": 30.0,
        "bike": 15.5, "walk": 5.0,
    }
    OVERHEAD = {
        "car": 5.0, "car_hybrid": 5.0,
        "bus": 6.0, "train": 6.0, "subway": 6.0,
        "bike": 2.0, "walk": 0.0,
    }
    sp = max(SPEED_KMH.get(mode, 30.0), 1e-6)
    oh = OVERHEAD.get(mode, 0.0)
    return round(oh + (distance_km / sp) * 60.0, 1)

# ──────────────────────────────────────────────────────────────────────────────
# Health / diagnostics
# ──────────────────────────────────────────────────────────────────────────────
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

# Versioned
@app.get("/api/v1/health")
def health_v1():
    return health_root()

@app.get("/api/v1/db-ping")
def db_ping_v1():
    return db_ping_root()

# ──────────────────────────────────────────────────────────────────────────────
# Sample routes
# ──────────────────────────────────────────────────────────────────────────────
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

# ──────────────────────────────────────────────────────────────────────────────
# Commute routes
# ──────────────────────────────────────────────────────────────────────────────
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

# ──────────────────────────────────────────────────────────────────────────────
# Auto-compare route 🚀 (Google-powered distance/time per mode)
# ──────────────────────────────────────────────────────────────────────────────
@app.post("/api/v1/commutes/auto-compare")
def auto_compare():
    """
    Compare all modes with Google Directions distance/time for accuracy.
    Driving uses live traffic (duration_in_traffic).
    Transit returns separate rows for Bus/Rail/Subway when routable.
    """
    require_key()
    data = request.get_json(silent=True) or {}
    origin = data.get("origin")
    destination = data.get("destination")
    passengers = int(data.get("passengers", 1))

    if not origin or not destination:
        abort(400, description="origin and destination required")

    plan = [
        {"mode": "car",         "gmode": "driving",  "tmode": None},
        {"mode": "car_hybrid",  "gmode": "driving",  "tmode": None},
        {"mode": "bus",         "gmode": "transit",  "tmode": "bus"},
        {"mode": "train",       "gmode": "transit",  "tmode": "rail"},
        {"mode": "subway",      "gmode": "transit",  "tmode": "subway"},
        {"mode": "bike",        "gmode": "bicycling","tmode": None},
        {"mode": "walk",        "gmode": "walking",  "tmode": None},
    ]

    results: list[dict] = []
    base_distance_for_log: Optional[float] = None

    for item in plan:
        m = item["mode"]
        gmode = item["gmode"]
        tmode = item["tmode"]

        resp = _directions(origin, destination, gmode, transit_mode=tmode)

        if resp["ok"] and resp["distance_km"]:
            distance_km = float(resp["distance_km"])
            duration_min = float(resp["duration_min"])
        else:
            # Minimal fallback if Directions fails completely
            distance_km = 1.0
            duration_min = _offline_time_min(distance_km, m)

        if base_distance_for_log is None and distance_km:
            base_distance_for_log = distance_km

        est = estimate_emissions(distance_km, m, passengers=passengers)

        results.append({
            "mode": est["mode"],
            "factor_kg_per_km": est["factor_kg_per_km"],
            "kgCO2e": est["kgCO2e"],
            "passengers": est["passengers"],
            "distance_km": distance_km,
            "time_min": duration_min,
            "source": est["source"],
        })

    # Optional realism filter for human-powered modes
    REALISM_LIMITS = {"walk": 3.2, "bike": 4.8}
    filtered, removed = [], []
    total_dist = base_distance_for_log or 0.0
    for r in results:
        lim = REALISM_LIMITS.get(r["mode"])
        if lim and total_dist > lim:
            removed.append(f"Excluded {r['mode'].title()} (> {lim} km)")
            continue
        filtered.append(r)

    # Persist summary (privacy-safe)
    if _mongo_ok():
        try:
            db.commutes.insert_one({
                "kind": "auto_compare",
                "origin": origin,
                "destination": destination,
                "passengers": passengers,
                "distance_km": total_dist,
                "results": filtered,
                "removed_notes": " ; ".join(removed),
                "ts": datetime.utcnow().isoformat() + "Z",
            })
        except Exception as e:
            log.warning("Failed to insert commute doc: %s", e)

    return jsonify({
        "ok": True,
        "origin": origin,
        "destination": destination,
        "distance_km": total_dist,
        "results": filtered,
        "removed_notes": " ; ".join(removed),
    }), 200

# ──────────────────────────────────────────────────────────────────────────────
# Test route for Sentry
# ──────────────────────────────────────────────────────────────────────────────
@app.get("/api/v1/test-error")
def test_error():
    if os.getenv("SENTRY_ENV") != "staging":
        abort(404)
    if request.headers.get("x-admin-key") != os.getenv("ADMIN_KEY"):
        abort(403)
    1 / 0  # intentional error

# ──────────────────────────────────────────────────────────────────────────────
# Routes list
# ──────────────────────────────────────────────────────────────────────────────
@app.get("/_routes")
def list_routes():
    rules = []
    for r in app.url_map.iter_rules():
        methods = ",".join(sorted(r.methods - {"HEAD", "OPTIONS"}))
        rules.append({"rule": str(r), "endpoint": r.endpoint, "methods": methods})
    rules.sort(key=lambda x: x["rule"])
    return jsonify(rules), 200

# ──────────────────────────────────────────────────────────────────────────────
# Entrypoint
# ──────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
