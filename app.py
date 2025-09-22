# app.py
from __future__ import annotations

import logging
import os
import ssl
from datetime import datetime, timezone
from typing import Optional

from flask import Flask, jsonify, request
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_talisman import Talisman
from pydantic import BaseModel, Field, ValidationError
from pymongo import MongoClient, DESCENDING
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

# ---------- Config / env ----------
API_PREFIX = "/api/v1"

MONGO_URI = os.environ.get("MONGO_URI", "")
API_KEY = os.environ.get("API_KEY", "")  # set on Heroku
ALLOW_CLEAR = os.environ.get("ALLOW_CLEAR", "0") == "1"

FRONTEND_ORIGIN = os.environ.get("FRONTEND_ORIGIN", "http://localhost:3000")
ALLOWED_ORIGINS = [FRONTEND_ORIGIN]

LIMITER_STORAGE_URI = os.environ.get("LIMITER_STORAGE_URI")  # e.g. rediss://...
LIMITER_INSECURE_SSL = os.environ.get("LIMITER_INSECURE_SSL", "0") == "1"  # allow weak TLS only if needed
DEFAULT_LIMITS = os.environ.get("DEFAULT_LIMITS", "200 per hour;50 per minute")

# For CI/local tests: disable HTTPS redirect/HSTS from Talisman
DISABLE_HTTPS_REDIRECT = os.environ.get("DISABLE_HTTPS_REDIRECT", "0") in {"1", "true", "True"}

# ---------- App ----------
app = Flask(__name__)
app.json.sort_keys = False

# Honor X-Forwarded-* from Heroku so Flask knows the real scheme/host
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# Security headers + HTTPS redirect (off when DISABLE_HTTPS_REDIRECT=1)
Talisman(
    app,
    force_https=not DISABLE_HTTPS_REDIRECT,
    strict_transport_security=not DISABLE_HTTPS_REDIRECT,
    content_security_policy=None,  # JSON API only
)

# CORS (echo only if Origin matches our allowlist)
CORS(app, resources={r"/api/*": {"origins": ALLOWED_ORIGINS}}, supports_credentials=False, max_age=3600)

# Rate limiting (Redis if configured, else in-memory)
storage_options = {}
if LIMITER_STORAGE_URI and LIMITER_STORAGE_URI.startswith("rediss://") and LIMITER_INSECURE_SSL:
    # Some managed Redis instances present a self-signed link in chain
    storage_options["ssl_cert_reqs"] = ssl.CERT_NONE  # noqa: S501

limiter = Limiter(
    key_func=get_remote_address,
    app=app,
    storage_uri=LIMITER_STORAGE_URI or "memory://",
    storage_options=storage_options or None,
    default_limits=DEFAULT_LIMITS.split(";") if DEFAULT_LIMITS else [],
    strategy="fixed-window",
)

# ---------- DB ----------
if not MONGO_URI:
    raise RuntimeError("MONGO_URI is required")

mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
db = mongo_client["cleancommute"]

# Helpful indexes (idempotent)
db.samples.create_index([("createdAt", DESCENDING)])
db.commutes.create_index([("createdAt", DESCENDING)])

# ---------- Auth ----------
def require_api_key() -> Optional[tuple[dict, int]]:
    """
    If API_KEY is set, require X-API-Key header to match. If API_KEY is empty,
    allow all (useful for local dev).
    """
    if not API_KEY:
        return None
    supplied = request.headers.get("x-api-key") or request.headers.get("X-API-Key")
    if supplied != API_KEY:
        return jsonify({"error": "unauthorized", "detail": "invalid or missing API key"}), 401
    return None


# ---------- Validation models ----------
class SampleIn(BaseModel):
    name: str = Field(min_length=1)
    status: str = Field(min_length=1)


class CommuteIn(BaseModel):
    distance_km: float = Field(gt=0)
    mode: str = Field(pattern="^(car|bus|train|bike|walk)$")
    origin: Optional[str] = None
    destination: Optional[str] = None
    passengers: int = Field(default=1, ge=1)


# ---------- Emissions (fallback simple factors) ----------
_FACTORS = {"car": 0.192, "bus": 0.105, "train": 0.041, "bike": 0.0, "walk": 0.0}


def calc_emissions(distance_km: float, mode: str, passengers: int = 1) -> tuple[float, dict]:
    factor = _FACTORS.get(mode, 0.0)
    per_passenger = factor / max(passengers, 1)
    kg = round(distance_km * per_passenger, 3)
    meta = {
        "mode": mode,
        "factor_kg_per_km": round(per_passenger, 6),
        "kgCO2e": kg,
        "passengers": passengers,
        "source": "emissions.py",
    }
    return kg, meta


# ---------- Error handlers ----------
@app.errorhandler(ValidationError)
def on_validation(err: ValidationError):
    return jsonify({"error": "validation_error", "detail": err.errors()}), 400


@app.errorhandler(HTTPException)
def on_http_exc(err: HTTPException):
    return jsonify({"error": err.name.replace(" ", "_").lower(), "detail": err.description}), err.code


@app.errorhandler(Exception)
def on_unhandled(err: Exception):
    app.logger.exception("unhandled_error")
    return jsonify({"error": "internal_error", "detail": str(err)}), 500


# ---------- Helpers ----------
def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def get_limit_param(default: int = 20, maximum: int = 100) -> int:
    try:
        n = int(request.args.get("limit", default))
    except Exception:
        n = default
    return max(1, min(n, maximum))


# ---------- Routes ----------
@app.route(f"{API_PREFIX}/health", methods=["GET", "HEAD"])
@app.route("/health", methods=["GET", "HEAD"])
def health():
    return jsonify({"status": "ok"})


@app.route(f"{API_PREFIX}/db-ping", methods=["GET"])
@app.route("/db-ping", methods=["GET"])
def db_ping():
    db.command("ping")
    return jsonify({"db": "ok"})


@app.route(f"{API_PREFIX}/samples", methods=["GET"])
@app.route("/samples", methods=["GET"])
def samples_list():
    limit = get_limit_param()
    docs = list(db.samples.find({}, {"_id": 0}).sort("createdAt", DESCENDING).limit(limit))
    return jsonify(docs)


@limiter.limit("20 per minute")
@app.route(f"{API_PREFIX}/samples", methods=["POST"])
@app.route("/samples", methods=["POST"])
def samples_create():
    auth = require_api_key()
    if auth:
        return auth
    data = SampleIn.model_validate_json(request.data or b"{}")
    doc = data.model_dump()
    doc["createdAt"] = now_utc()
    ins = db.samples.insert_one(doc)
    return jsonify({"inserted_id": str(ins.inserted_id)}), 201


@app.route(f"{API_PREFIX}/samples/clear", methods=["POST"])
def samples_clear():
    if not ALLOW_CLEAR:
        return jsonify({"error": "forbidden", "detail": "clearing disabled"}), 403
    auth = require_api_key()
    if auth:
        return auth
    res = db.samples.delete_many({})
    return jsonify({"deleted": res.deleted_count})


@limiter.limit("20 per minute")
@app.route(f"{API_PREFIX}/commutes", methods=["POST"])
def commute_create():
    auth = require_api_key()
    if auth:
        return auth
    payload = CommuteIn.model_validate_json(request.data or b"{}")
    kg, meta = calc_emissions(payload.distance_km, payload.mode, payload.passengers)
    doc = {
        "createdAt": now_utc(),
        "distance_km": float(payload.distance_km),
        "mode": payload.mode,
        "origin": payload.origin,
        "destination": payload.destination,
        "emissions_kgCO2e": kg,
        "meta": meta,
    }
    db.commutes.insert_one(doc)
    out = dict(doc)
    out["createdAt"] = out["createdAt"].isoformat()
    return jsonify({"data": out}), 201


@app.route(f"{API_PREFIX}/commutes", methods=["GET"])
def commute_list():
    limit = get_limit_param()
    docs = list(
        db.commutes.find({}, {"_id": 0}).sort("createdAt", DESCENDING).limit(limit)
    )
    return jsonify(docs)


@app.route(f"{API_PREFIX}/commutes/clear", methods=["POST"])
def commute_clear():
    if not ALLOW_CLEAR:
        return jsonify({"error": "forbidden", "detail": "clearing disabled"}), 403
    auth = require_api_key()
    if auth:
        return auth
    res = db.commutes.delete_many({})
    return jsonify({"deleted": res.deleted_count})


# ---------- Entrypoint ----------
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "3000")), debug=False)
