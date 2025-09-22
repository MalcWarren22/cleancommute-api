from flask import Flask, jsonify, request, Blueprint
from flask_cors import CORS
from pymongo import MongoClient, errors
from dotenv import load_dotenv
from datetime import datetime, timezone
from werkzeug.exceptions import HTTPException
from pydantic import BaseModel, Field, ValidationError
import logging, sys, os

load_dotenv(".env")
MONGO_URI = os.getenv("MONGO_URI")
client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000, retryWrites=True) if MONGO_URI else None
db = client["cleancommute"] if client else None

app = Flask(__name__)
CORS(app)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

def _serialize(doc):
    out = {}
    for k, v in doc.items():
        if isinstance(v, datetime):
            out[k] = v.astimezone(timezone.utc).isoformat()
        else:
            out[k] = v
    return out

def ensure_indexes():
    if db is None:
        return
    db.samples.create_index([("createdAt", -1)])
ensure_indexes()

@app.errorhandler(HTTPException)
def handle_http_err(e):
    return {"error": e.name, "detail": e.description}, e.code

@app.errorhandler(Exception)
def handle_any_err(e):
    log.exception("unhandled_error")
    return {"error": "internal_error", "detail": str(e)}, 500

@app.get("/")
def root():
    return {"service": "cleancommute-api", "version": "v1"}

# -------- Validation models --------
class SampleIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    status: str = Field(min_length=1, max_length=40)

# -------- Core impls shared by v1 + legacy --------
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

# -------- v1 blueprint --------
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

app.register_blueprint(api)

# -------- Legacy routes (temporary compatibility) --------
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
