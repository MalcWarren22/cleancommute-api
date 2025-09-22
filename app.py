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
from werkzeug.exceptions import HTTPException


def create_app() -> Flask:
    app = Flask(__name__)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)  # type: ignore

    # ---------- Config ----------
    app.config["API_KEY"] = os.getenv("API_KEY", "")
    app.config["MONGO_URI"] = os.getenv("MONGO_URI", "")
    app.config["MONGO_DB"] = os.getenv("MONGO_DB", "cleancommute")
    app.config["DEFAULT_LIMITS"] = os.getenv("DEFAULT_LIMITS", "100 per 15 minutes")
    app.config["ALLOW_CLEAR"] = os.getenv("ALLOW_CLEAR", "false").strip().lower() in {
        "true",
        "1",
        "yes",
        "y",
        "on",
    }
    app.config["FRONTEND_ORIGIN"] = os.getenv("FRONTEND_ORIGIN", "")

    # ---------- CORS ----------
    cors_origin = app.config["FRONTEND_ORIGIN"] or "*"
    CORS(app, resources={r"/api/*": {"origins": cors_origin}})

    # ---------- Rate Limiter ----------
    limiter_storage = os.getenv("LIMITER_STORAGE_URI")  # e.g., rediss://...
    limiter = Limiter(
        key_func=get_remote_address,
        default_limits=[app.config["DEFAULT_LIMITS"]],
        storage_uri=limiter_storage if limiter_storage else None,
    )
    limiter.init_app(app)

    # ---------- Mongo (do not crash app if not configured) ----------
    mongo_client = None
    db = None
    samples = None
    commutes = None
    mongo_status = "not_configured"
    if app.config["MONGO_URI"]:
        try:
            mongo_client = MongoClient(app.config["MONGO_URI"], serverSelectionTimeoutMS=5000)
            mongo_client.server_info()  # trigger connection
            db = mongo_client[app.config["MONGO_DB"]]
            samples = db["samples"]
            commutes = db["commutes"]
            mongo_status = "connected"
        except Exception as e:
            # App still boots; /db-ping will report the error.
            mongo_status = f"error: {e}"

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
        try:
            n = int(request.args.get("limit", default))
        except (TypeError, ValueError):
            n = default
        return max(1, min(maximum, n))

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

    # ---------- Error Handlers (fix 404s being turned into 500s) ----------
    @app.errorhandler(HTTPException)
    def http_error(e: HTTPException):
        return json_ok({"error": e.name, "detail": e.description}, e.code)

    @app.errorhandler(429)
    def rate_limit_handler(e):
        return json_ok({"error": "rate_limited", "detail": str(e)}, 429)

    @app.errorhandler(Exception)
    def unhandled_error(e):
        return json_ok({"error": "internal_error", "detail": str(e)}, 500)

    # ---------- Health ----------
    @app.get("/api/v1/health")
    @limiter.exempt
    def health():
        return json_ok({"ok": True, "ts": datetime.utcnow().isoformat(), "db": mongo_status})

    # Heroku/GHA friendly root health
    @app.get("/health")
    @limiter.exempt
    def health_alias():
        return json_ok({"ok": True, "ts": datetime.utcnow().isoformat(), "db": mongo_status})

    # ---------- DB Ping ----------
    @app.get("/api/v1/db-ping")
    @limiter.exempt
    def db_ping():
        if not mongo_client:
            return json_ok({"ok": False, "detail": "MONGO_URI not configured"}, 500)
        try:
            mongo_client.admin.command("ping")
            return json_ok({"ok": True})
        except Exception as e:
            return json_ok({"ok": False, "detail": str(e)}, 500)

    # ---------- Samples ----------
    @app.get("/api/v1/samples")
    def samples_list():
        if not samples:
            return json_ok({"data": []})
        docs = list(samples.find().limit(parse_limit()))
        return json_ok({"data": sanitize_mongo(docs)})

    @app.post("/api/v1/samples")
    @require_api_key
    def samples_create():
        if not samples:
            return json_ok({"error": "unavailable", "detail": "Mongo not configured"}, 503)
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
        if not samples:
            return json_ok({"error": "unavailable", "detail": "Mongo not configured"}, 503)
        if not current_app.config["ALLOW_CLEAR"]:
            return json_ok({"error": "forbidden", "detail": "Clearing disabled"}, 403)
        n = samples.delete_many({}).deleted_count
        return json_ok({"deleted": n})

    # ---------- Commutes ----------
    @app.get("/api/v1/commutes")
    def commute_list():
        if not commutes:
            return json_ok({"data": []})
        items = list(commutes.find().sort("created_at", -1).limit(parse_limit()))
        return json_ok({"data": sanitize_mongo(items)})

    @app.post("/api/v1/commutes")
    @require_api_key
    def commute_create():
        if not commutes:
            return json_ok({"error": "unavailable", "detail": "Mongo not configured"}, 503)
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
        if not commutes:
            return json_ok({"error": "unavailable", "detail": "Mongo not configured"}, 503)
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
