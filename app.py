import os
import json
from functools import wraps
from datetime import datetime
from typing import Any, Dict, List, Union

from flask import Flask, jsonify, request, current_app
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from pymongo import MongoClient
from bson import ObjectId
from werkzeug.middleware.proxy_fix import ProxyFix

# ---------- App & Config ----------
def create_app() -> Flask:
    app = Flask(__name__)

    # Security / HTTPS behind proxies (Heroku)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)  # type: ignore

    # Env
    app.config["API_KEY"] = os.getenv("API_KEY", "")
    app.config["MONGO_URI"] = os.getenv("MONGO_URI", "")
    app.config["MONGO_DB"] = os.getenv("MONGO_DB", "cleancommute")
    app.config["DEFAULT_LIMITS"] = os.getenv("DEFAULT_LIMITS", "100 per 15 minutes")
    app.config["ALLOW_CLEAR"] = os.getenv("ALLOW_CLEAR", "false").lower() == "true"
    app.config["FRONTEND_ORIGIN"] = os.getenv("FRONTEND_ORIGIN", "")
    limiter_storage = os.getenv("LIMITER_STORAGE_URI")  # e.g., redis://...

    # CORS (single origin)
    cors_kwargs = {"resources": {r"/api/*": {"origins": app.config["FRONTEND_ORIGIN"]}}}
    CORS(app, **cors_kwargs)

    # Rate Limiter
    limiter = Limiter(
        key_func=get_remote_address,
        default_limits=[app.config["DEFAULT_LIMITS"]],
        storage_uri=limiter_storage if limiter_storage else None,
    )
    limiter.init_app(app)

    # Mongo
    if not app.config["MONGO_URI"]:
        raise RuntimeError("MONGO_URI is required")
    mongo_client = MongoClient(app.config["MONGO_URI"])
    db = mongo_client[app.config["MONGO_DB"]]
    samples = db["samples"]
    commutes = db["commutes"]

    # ---------- Helpers ----------
    def sanitize_mongo(obj: Any) -> Any:
        """Recursively convert Mongo/ BSON types (ObjectId, etc.) to JSON-safe values."""
        if isinstance(obj, ObjectId):
            return str(obj)
        if isinstance(obj, list):
            return [sanitize_mongo(x) for x in obj]
        if isinstance(obj, dict):
            return {k: sanitize_mongo(v) for k, v in obj.items()}
        return obj

    def json_ok(payload: Dict[str, Any], status: int = 200):
        # Use Flask's response class with json.dumps to leverage default=str if needed
        body = json.dumps(payload, default=str)
        return current_app.response_class(body, mimetype="application/json", status=status)

    def require_api_key(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            expected = current_app.config.get("API_KEY", "")
            provided = request.headers.get("x-api-key", "")
            if expected and provided != expected:
                return json_ok({"error": "forbidden", "detail": "Invalid API key"}, 403)
            return f(*args, **kwargs)
        return wrapper

    # ---------- Error Handlers ----------
    @app.errorhandler(429)
    def rate_limit_handler(e):
        return json_ok({"error": "rate_limited", "detail": str(e)}, 429)

    @app.errorhandler(Exception)
    def unhandled_error(e):
        # Avoid leaking stack traces; keep it consistent for smoke tests
        return json_ok({"error": "internal_error", "detail": str(e)}, 500)

    # ---------- Routes ----------
    @app.get("/api/v1/health")
    def health():
        return json_ok({"ok": True, "ts": datetime.utcnow().isoformat()})

    @app.get("/api/v1/db-ping")
    def db_ping():
        try:
            # cheap ping by asking server_info (doesn't create a cursor)
            mongo_client.server_info()  # type: ignore
            return json_ok({"ok": True})
        except Exception as e:
            return json_ok({"ok": False, "detail": str(e)}, 500)

    # ----- Samples -----
    @app.get("/api/v1/samples")
    def samples_list():
        docs = list(samples.find().limit(100))
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
        items = list(commutes.find().sort("created_at", -1).limit(100))
        return json_ok({"data": sanitize_mongo(items)})

    @app.post("/api/v1/commutes")
    @require_api_key
    def commute_create():
        payload = request.get_json(silent=True) or {}
        # Minimal schema—extend as your model grows
        doc = {
            "origin": payload.get("origin"),
            "destination": payload.get("destination"),
            "mode": payload.get("mode", "driving"),
            "notes": payload.get("notes"),
            "created_at": datetime.utcnow(),
        }
        res = commutes.insert_one(doc)
        doc["_id"] = res.inserted_id
        # Sanitize BEFORE returning so ObjectId never hits Flask JSON encoder
        return json_ok({"data": sanitize_mongo(doc)}, 201)

    @app.post("/api/v1/commutes/clear")
    @require_api_key
    def commute_clear():
        if not current_app.config["ALLOW_CLEAR"]:
            return json_ok({"error": "forbidden", "detail": "Clearing disabled"}, 403)
        n = commutes.delete_many({}).deleted_count
        return json_ok({"deleted": n})

    # Root
    @app.get("/")
    def root():
        return json_ok({"service": "clean-commute-api", "prefix": "/api/v1"})

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
