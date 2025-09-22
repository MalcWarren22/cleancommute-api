import os
import json
from functools import wraps
from datetime import datetime
from typing import Any, Dict

from flask import Flask, request, current_app
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from pymongo import MongoClient
from bson import ObjectId
from werkzeug.middleware.proxy_fix import ProxyFix

# ---------------------------------------
# App Factory
# ---------------------------------------
def create_app() -> Flask:
    app = Flask(__name__)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)  # type: ignore

    # ---------- Config ----------
    app.config["API_KEY"] = os.getenv("API_KEY", "")
    app.config["MONGO_URI"] = os.getenv("MONGO_URI", "")
    app.config["MONGO_DB"] = os.getenv("MONGO_DB", "cleancommute")
    app.config["DEFAULT_LIMITS"] = os.getenv("DEFAULT_LIMITS", "100 per 15 minutes")
    # Accept several truthy values so CI can pass "true" or "1"
    app.config["ALLOW_CLEAR"] = os.getenv("ALLOW_CLEAR", "false").strip().lower() in (
        "true",
        "1",
        "yes",
        "y",
        "on",
    )
    app.config["FRONTEND_ORIGIN"] = os.getenv("FRONTEND_ORIGIN", "")

    if not app.config["MONGO_URI"]:
        raise RuntimeError("MONGO_URI is required")

    # ---------- CORS ----------
    # If FRONTEND_ORIGIN is unset, allow all in dev/test to avoid CORS headaches.
    cors_origin = app.config["FRONTEND_ORIGIN"] or "*"
    CORS(app, resources={r"/api/*": {"origins": cors_origin}})

    # ---------- Rate Limiter ----------
    limiter_storage = os.getenv("LIMITER_STORAGE_URI")  # e.g., redis://...
    limiter = Limiter(
        key_func=get_remote_address,
        default_limits=[app.config["DEFAULT_LIMITS"]],
        storage_uri=limiter_storage if limiter_storage else None,
    )
    limiter.init_app(app)

    # ---------- Mongo ----------
    mongo_client = MongoClient(app.config["MONGO_URI"])
    db = mongo_client[app.config["MONGO_DB"]]
    samples = db["samples"]
    commutes = db["commutes"]

    # ---------- Helpers ----------
    def sanitize_mongo(obj: Any) -> Any:
        if isinstance(obj, ObjectId):
            return str(obj)
        if isinstance(obj, list):
            return [sanitize_mongo(x) for x in obj]
        if isinstance(obj, dict):
            return {k: sanitize_mongo(v) for k, v in obj.items()}
        return obj

    def json_ok(payload: Dict[str, Any], status: int = 200):
        body = json.dumps(payload, default=str)
        return current_app.response_class(body, mimetype="application/json", status=status)

    def parse_limit(default: int = 100, maximum: int = 500) -> int:
        """Read ?limit= from querystring, clamp between 1..maximum."""
        try:
            n = int(request.args.get("limit", default))
        except (TypeError, ValueError):
            n = default
        return max(1, min(maximum, n))

    # --- 401 for missing/invalid API key ---
    def require_api_key(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            expected = current_app.config.get("API_KEY", "")
            if not expected:  # allow in dev if no key configured
                return f(*args, **kwargs)
            provided = request.headers.get("x-api-key")
            if not provided or provided != expected:
                resp = json_ok(
                    {"error": "unauthorized", "detail": "Missing or invalid API key"},
                    401,
                )
                resp.headers["WWW-Authenticate"] = 'API-Key realm="clean-commute-api"'
                return resp
            return f(*args, **kwargs)
        return wrapper

    # ---------- Error Handlers ----------
    @app.errorhandler(429)
    def rate_limit_handler(e):
        return json_ok({"error": "rate_limited", "detail": str(e)}, 429)

    @app.errorhandler(Exception)
    def unhandled_error(e):
        # Keep simple, consistent JSON for CI and clients
        return json_ok({"error": "internal_error", "detail": str(e)}, 500)

    # ---------- Routes ----------
    @app.get("/api/v1/health")
    @limiter.exempt
    def health():
        return json_ok({"ok": True, "ts": datetime.utcnow().isoformat()})

    @app.get("/api/v1/db-ping")
    @limiter.exempt
    def db_ping():
        try:
            mongo_client.server_info()  # type: ignore
            return json_ok({"ok": True})
        except Exception as e:
            return json_ok({"ok": False, "detail": str(e)}, 500)

    # ----- Samples -----
    @app.get("/api/v1/samples")
    def samples_list():
        docs = list(samples.find().limit(parse_limit()))
        return json_ok({"data": sanitize_mongo(docs)})

    @app.post("/api/v1/samples")
    @require_api_key
    def samples_create():
        payload = request.get_json(silent=True) or {}
        doc = {
            "name": payload.get("name", "sample"),
            "created_at": datetime.utcnow(),
            "meta": payload.get("meta", {}),
        }
        res = samples.insert_one(doc)
        doc["_id"] = res.inserted_id
        return json_ok({"data": sanitize_mongo(doc)}, 201)

    @app.post("/api/v1/samples/clear")
    @require_api_key
    def samples_clear():
        if not current_app.config["ALLOW_CLEAR"]:
            return json_ok({"error": "forbidden", "detail": "Clearing disabled"}, 403)
        n = samples.delete_many({}).deleted_count
        return json_ok({"deleted": n})

    # ----- Commutes -----
    @app.get("/api/v1/commutes")
    def commute_list():
        items = list(commutes.find().sort("created_at", -1).limit(parse_limit()))
        return json_ok({"data": sanitize_mongo(items)})

    @app.post("/api/v1/commutes")
    @require_api_key
    def commute_create():
        payload = request.get_json(silent=True) or {}
        doc = {
            "origin": payload.get("origin"),
            "destination": payload.get("destination"),
            "mode": payload.get("mode", "driving"),
            "notes": payload.get("notes"),
            "created_at": datetime.utcnow(),
        }
        res = commutes.insert_one(doc)
        doc["_id"] = res.inserted_id
        return json_ok({"data": sanitize_mongo(doc)}, 201)

    @app.post("/api/v1/commutes/clear")
    @require_api_key
    def commute_clear():
        if not current_app.config["ALLOW_CLEAR"]:
            return json_ok({"error": "forbidden", "detail": "Clearing disabled"}, 403)
        n = commutes.delete_many({}).deleted_count
        return json_ok({"deleted": n})

    @app.get("/")
    def root():
        return json_ok({"service": "clean-commute-api", "prefix": "/api/v1"})

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
