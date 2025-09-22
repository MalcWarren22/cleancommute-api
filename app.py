from flask import Flask, jsonify, request, Blueprint
from flask_cors import CORS
from pymongo import MongoClient, errors
from dotenv import load_dotenv
from datetime import datetime, timezone
from werkzeug.exceptions import HTTPException
from pydantic import BaseModel, Field, ValidationError
from typing import Optional
from werkzeug.middleware.proxy_fix import ProxyFix
from flask_talisman import Talisman
import logging, sys, os

# ----- Setup / DB -----
load_dotenv(".env")
MONGO_URI = os.getenv("MONGO_URI")
client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000, retryWrites=True) if MONGO_URI else None
db = client["cleancommute"] if client else None

app = Flask(__name__)

# Trust Heroku's reverse proxy (so X-Forwarded-Proto is honored)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)

# CORS: allow only origins from env var (comma-separated). Use "*" in dev if needed.
origins_env = os.getenv("ALLOWED_ORIGINS", "*")
allowed_origins = [o.strip() for o in origins_env.split(",")] if origins_env else ["*"]
CORS(app, resources={r"/api/*": {"origins": allowed_origins}}, supports_credentials=False)

# HTTPS/HSTS headers + http→https redirect (can disable with ENABLE_HTTPS_HEADERS=0)
if os.getenv("ENABLE_HTTPS_HEADERS", "1").lower() not in ("0", "false", "no"):
    Talisman(
        app,
        force_https=True,
        strict_transport_security=True,
        strict_transport_security_preload=True,
        strict_transport_security_max_age=31536000,  # 1 year
        content_security_policy=None,  # API-only; no CSP needed
    )

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

# Try to use user's emissions.py if present
try:
    from emissions import estimate_emissions as _external_estimate_emissions  # (distance_km: float, mode: str) -> dict
except Exception:
    _external_estimate_emissions = None

# ----- Helpers -----
def _serialize(doc):
    out = {}
    for k, v in doc.items():
        if isinstance(v, datetime):
            out[k] = v.astimezone(timezone.utc).isoformat()
        else:
            out[k] = v
    return out

def _estimate_emissions_local(distance_km: float, mode: str) -> dict:
    EF = {
        "car": 0.192,
        "car_gas": 0.192,
        "car_hybrid": 0.120,
        "rideshare": 0.212,
        "bus": 0.082,
        "train": 0.041,
        "subway": 0.045,
        "bike": 0.0,
        "walk": 0.0,
    }
    f = EF.get((mode or "").lower(), EF["car"])
    kg = round(distance_km * f, 4)
    return {"kgCO2e": kg, "factor_kg_per_km": f, "mode": mode, "source": "local_fallback"}

def ensure_indexes():
    if db is None:
        return
    db.samples.create_index([("createdAt", -1)])
    db.commutes.create_index([("createdAt", -1)])
ensure_indexes()

# ----- Errors -----
@app.errorhandler(HTTPException)
def handle_http_err(e):
    return {"error": e.name, "detail": e.description}, e.code

@app.errorhandler(Exception)
def handle_any_err(e):
    log.exception("unhandled_error")
    return {"error": "internal_error", "detail": str(e)}, 500

# ----- Root -----
@app.get("/")
def root():
    return {"service": "cleancommute-api", "version": "v1"}

# ----- Validation models -----
class SampleIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    status: str = Field(min_length=1, max_length=40)

class CommuteIn(BaseModel):
    distance_km: float = Field(gt=0)
    mode: str = Field(min_length=1, max_length=40)
    origin: Optional[str] = None
    destination: Optional[str] = None

# ----- Core impls (shared) -----
def health_impl():
    return {"status": "ok"}

def db_ping_impl():
    if db is None:
        return {"db": "missing_config"}, 503
    db.command("ping")
    return {"db": "ok"}

def samples_get_impl():
    if db is None:
        return {"error": "db not configured"}, 503
    limit = request.args.get("limit", "100")
    try:
        limit = max(1, min(int(limit), 1000))
    except ValueError:
        limit = 100
    cursor = db.samples.find({}, {"_id": 0}).sort("createdAt", -1).limit(limit)
    return jsonify([_serialize(d) for d in cursor])

def samples_post_impl():
    if db is None:
        return {"error": "db not configured"}, 503
    raw = request.get_json(silent=True) or {}
    try:
        payload = SampleIn.model_validate(raw)
    except ValidationError as e:
        return {"error": "validation_error", "detail": e.errors()}, 400
    doc = payload.model_dump()
    doc["createdAt"] = datetime.now(timezone.utc)
    result = db.samples.insert_one(doc)
    return {"inserted_id": str(result.inserted_id)}, 201

def samples_clear_impl():
    if db is None:
        return {"error": "db not configured"}, 503
    if not str(os.getenv("ALLOW_CLEAR", "")).lower() in ("1", "true", "yes"):
        return {"error": "forbidden", "detail": "clear endpoints disabled"}, 403
    res = db.samples.delete_many({})
    return {"deleted": res.deleted_count}

def commutes_post_impl():
    if db is None:
        return {"error": "db not configured"}, 503
    raw = request.get_json(silent=True) or {}
    try:
        trip = CommuteIn.model_validate(raw)
    except ValidationError as e:
        return {"error": "validation_error", "detail": e.errors()}, 400

    if _external_estimate_emissions:
        try:
            er = _external_estimate_emissions(trip.distance_km, trip.mode, **{k: v for k, v in raw.items() if k not in {"distance_km", "mode"}})
        except Exception as ex:
            er = _estimate_emissions_local(trip.distance_km, trip.mode)
            er["source"] = f"emissions.py failed: {ex}; used local_fallback"
    else:
        er = _estimate_emissions_local(trip.distance_km, trip.mode)

    kg = er.get("kgCO2e")
    if kg is None:
        kg = er.get("kg_co2e") or er.get("kg") or er.get("co2e_kg") or 0.0

    doc = {
        "origin": trip.origin,
        "destination": trip.destination,
        "mode": trip.mode,
        "distance_km": float(trip.distance_km),
        "emissions_kgCO2e": float(kg),
        "meta": er,
        "createdAt": datetime.now(timezone.utc),
    }
    res = db.commutes.insert_one(doc)
    doc_out = {k: v for k, v in doc.items() if k != "_id"}
    doc_out["id"] = str(res.inserted_id)
    return {"ok": True, "data": doc_out}, 201

def commutes_get_impl():
    if db is None:
        return {"error": "db not configured"}, 503
    limit = request.args.get("limit", "50")
    try:
        limit = max(1, min(int(limit), 1000))
    except ValueError:
        limit = 50
    cursor = db.commutes.find({}, {"_id": 0}).sort("createdAt", -1).limit(limit)
    return jsonify([_serialize(d) for d in cursor])

def commutes_clear_impl():
    if db is None:
        return {"error": "db not configured"}, 503
    if not str(os.getenv("ALLOW_CLEAR", "")).lower() in ("1", "true", "yes"):
        return {"error": "forbidden", "detail": "clear endpoints disabled"}, 403
    res = db.commutes.delete_many({})
    return {"deleted": res.deleted_count}

# ----- v1 Blueprint -----
api = Blueprint("api", __name__, url_prefix="/api/v1")

@api.get("/health")
def v1_health():
    return health_impl()

@api.get("/db-ping")
def v1_db_ping():
    return db_ping_impl()

@api.get("/samples")
def v1_samples_get():
    try:
        return samples_get_impl()
    except errors.PyMongoError as e:
        return {"error": "db read failed", "detail": str(e)}, 500

@api.post("/samples")
def v1_samples_post():
    try:
        return samples_post_impl()
    except errors.PyMongoError as e:
        return {"error": "db write failed", "detail": str(e)}, 500

@api.post("/samples/clear")
def v1_samples_clear():
    try:
        return samples_clear_impl()
    except errors.PyMongoError as e:
        return {"error": "db clear failed", "detail": str(e)}, 500

@api.post("/commutes")
def v1_commutes_post():
    try:
        return commutes_post_impl()
    except errors.PyMongoError as e:
        return {"error": "db write failed", "detail": str(e)}, 500

@api.get("/commutes")
def v1_commutes_get():
    try:
        return commutes_get_impl()
    except errors.PyMongoError as e:
        return {"error": "db read failed", "detail": str(e)}, 500

@api.post("/commutes/clear")
def v1_commutes_clear():
    try:
        return commutes_clear_impl()
    except errors.PyMongoError as e:
        return {"error": "db clear failed", "detail": str(e)}, 500

app.register_blueprint(api)

# ----- Legacy (temporary) -----
@app.get("/health")
def legacy_health():
    return v1_health()

@app.get("/db-ping")
def legacy_db_ping():
    return v1_db_ping()

@app.get("/samples")
def legacy_samples_get():
    return v1_samples_get()

@app.post("/samples")
def legacy_samples_post():
    return v1_samples_post()

if __name__ == "__main__":
    app.run(debug=True)
