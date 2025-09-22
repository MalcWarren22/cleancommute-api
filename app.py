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


def create_app() -> Flask:
    app = Flask(__name__)
    # Respect reverse proxy headers (Heroku, etc.)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)  # type: ignore

    # ---- Config (env-backed) -------------------------------------------------
    app.config["API_KEY"] = os.getenv("API_KEY", "")
    app.config["MONGO_URI"] = os.getenv("MONGO_URI", "")
    app.config["MONGO_DB"] = os.getenv("MONGO_DB", "cleancommute")
    app.config["DEFAULT_LIMITS"] = os.getenv("DEFAULT_LIMITS", "100 per 15 minutes")
    app.config["ALLOW_CLEAR"] = os.getenv("ALLOW_CLEAR", "false").lower() == "true"
    app.config["FRONTEND_ORIGIN"] = os.getenv("FRONTEND_ORIGIN", "")
    limiter_storage = os.getenv("LIMITER_STORAGE_URI")  # e.g., redis://...

    # CORS (only under /api/* to the configured frontend)
    CORS(app, resources={r"/api/*": {"origins": app.config["FRONTEND_ORIGIN"]}})

    # Rate limiting: fall back to in-memory when storage not provided (avoids CI warning)
    limiter = Limiter(
        key_func=get_remote_address,
        default_limits=[app.config["DEFAULT_LIMITS"]],
        storage_uri=limiter_storage or "memory://",
    )
    limiter.init_app(app)

    # ---- Mongo ----------------------------------------------------------------
    if not app.config["MONGO_URI"]:
        raise RuntimeError("MONGO_URI is required")

    mongo_client = MongoClient(app.config["MONGO_URI"])
    # Ensure a default DB name exists even if URI omits one (e.g., localhost)
    db = mongo_client.get_database(app.config["MONGO_DB"])  # safe default
    samples = db["samples"]
    commutes = db["commutes"]

    # ---- Helpers --------------------------------------------------------------
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
        return current_app.response_class(
            body, mimetype="application/json", status=status
        )

    # Enforce API key on mutating endpoints
    def require_api_key(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            expected = current_app.config.get("API_KEY", "")
            if not expected:  # allow in dev if no key configured
                return f(*args, **kwargs)
            provided = request.headers.get("x-api-key")
