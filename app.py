import os
import json
from functools import wraps
from datetime import datetime, timezone
from typing import Any, Dict
from dotenv import load_dotenv
load_dotenv()


from flask import Flask, request, current_app
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from pymongo import MongoClient
from pymongo.errors import ServerSelectionTimeoutError
from bson import ObjectId
from werkzeug.middleware.proxy_fix import ProxyFix


def create_app() -> Flask:
    app = Flask(__name__)
    # Respect reverse proxy headers (Heroku, etc.)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)  # type: ignore

    # ---- Config (env-backed) -------------------------------------------------
    app.config["API_KEY"] = os.getenv("API_KEY", "")
    # Default to CI/Actions Mongo service if not provided
    app.config["MONGO_URI"] = os.getenv("MONGO_URI", "mongodb://localhost:27017")
    app.config["MONGO_DB"] = os.getenv("MONGO_DB", "cleancommute")
    app.config["DEFAULT_LIMITS"] = os.getenv("DEFAULT_LIMITS", "100 per 15 minutes")
    app.config["ALLOW_CLEAR"] = os.getenv("ALLOW_CLEAR", "false").lower() == "true"
    app.config["FRONTEND_ORIGIN"] = os.getenv("FRONTEND_ORIGIN", "*")
    limiter_storage = os.getenv("LIMITER_STORAGE_URI")  # e.g., redis://...

    # CORS (only under /api/* to the configured frontend)
    CORS(app, resources={r"/api/*": {"origins": app.config["FRONTEND_ORIGIN"]}})

    # Rate limiting — pass storage_uri directly with app to avoid warning
    Limiter(
        app=app,
        key_func=get_remote_address,
        default_limits=[app.config["DEFAULT_LIMITS"]],
        storage_uri=limiter_storage or "memory://",
    )

    # ---- Mongo ----------------------------------------------------------------
    mongo_client = MongoClient(
        app.config["MONGO_URI"],
        serverSelectionTimeoutMS=3000,
        connectTimeoutMS=3000,
        socketTimeoutMS=3000,
        retryWrites=True,
    )
    db = mongo_client.get_database(app.config["MONGO_DB"])
    samples = db["samples"]
    commutes = db["commutes"]

    # Try a ping, but don’t kill the app in CI/dev
    try:
        mongo_client.admin.command("ping")
    except ServerSelectionTimeoutError:
        # still start; endpoints that need DB will surface 503s
        pass

    # ---- Helpers --------------------------------------------------------------
    def _now_utc_iso() -> str:
        # timezone-aware, consistent "Z" suffix
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

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

    def json_err(message: str, status: int = 400):
        return json_ok({"ok": False, "error": message}, status=status)

    # Enforce API key on mutating endpoints
    def require_api_key(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            expected = current_app.config.get("API_KEY", "")
            # If API_KEY isn’t configured (e.g., CI), allow the call
            if expected:
                provided = request.headers.get("x-api-key")
                if provided != expected:
                    return json_err("unauthorized", 401)
            return f(*args, **kwargs)
        return wrapper

    # Envelope helper
    def ok(data: Dict[str, Any] | list | None = None, **extra):
        payload: Dict[str, Any] = {
            "ok": True,
            "ts": _now_utc_iso(),
        }
        if data is not None:
            payload["data"] = sanitize_mongo(data)  # ensure JSON-safe ids
        if extra:
            payload.update(extra)
        return json_ok(payload)

    # ---- Routes ---------------------------------------------------------------
    @app.get("/api/v1/health")
    def health():
        return ok(
            {
                "service": "clean-commute-api",
                "env": os.getenv("FLASK_ENV", "production"),
            }
        )

    @app.get("/api/v1/db-ping")
    def db_ping():
        try:
            mongo_client.admin.command("ping")
            return ok({"mongo": "up"})
        except ServerSelectionTimeoutError as e:
            return json_err(f"mongo_unreachable: {e}", 503)

    # ---- Samples --------------------------------------------------------------
    @app.get("/api/v1/samples")
    def get_samples():
        items = list(samples.find({}, {"_id": 0}).limit(100))
        return ok(items)

    @app.post("/api/v1/samples")
    @require_api_key
    def add_sample():
        doc = request.get_json(silent=True) or {}
        doc["created_at"] = _now_utc_iso()
        samples.insert_one(doc)
        return ok(doc), 201

    @app.post("/api/v1/samples/clear")
    @require_api_key
    def clear_samples():
        if not app.config["ALLOW_CLEAR"]:
            return json_err("clearing disabled (set ALLOW_CLEAR=true to enable)", 403)
        samples.delete_many({})
        return ok({"cleared": True})

    # ---- Commutes -------------------------------------------------------------
    @app.get("/api/v1/commutes")
    def get_commutes():
        items = list(commutes.find({}, {"_id": 0}).limit(500))
        return ok(items)

    @app.post("/api/v1/commutes")
    @require_api_key
    def add_commute():
        payload = request.get_json(silent=True) or {}
        required = ["origin", "destination", "mode"]
        missing = [k for k in required if k not in payload]
        if missing:
            return json_err(f"missing fields: {', '.join(missing)}", 400)
        payload["created_at"] = _now_utc_iso()
        commutes.insert_one(payload)
        return ok(payload), 201

    @app.post("/api/v1/commutes/clear")
    @require_api_key
    def clear_commutes():
        if not app.config["ALLOW_CLEAR"]:
            return json_err("clearing disabled (set ALLOW_CLEAR=true to enable)", 403)
        commutes.delete_many({})
        return ok({"cleared": True})

    # Root helper
    @app.get("/")
    def root():
        return ok(
            {
                "endpoints": [
                    "/api/v1/health",
                    "/api/v1/db-ping",
                    "/api/v1/samples [GET, POST, POST /clear]",
                    "/api/v1/commutes [GET, POST, POST /clear]",
                ]
            }
        )

    return app


# Expose a module-level app for gunicorn (`wsgi:application` or `app:app`)
app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
