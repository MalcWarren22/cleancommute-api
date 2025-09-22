import os
import json
from functools import wraps
from datetime import datetime
from typing import Any, Dict

from flask import Flask, request, current_app, Response
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from pymongo import MongoClient
from bson import ObjectId
from werkzeug.middleware.proxy_fix import ProxyFix
from pydantic import BaseModel, Field, ValidationError

# --- OpenAPI schema (extend as needed)
OPENAPI = {
    "openapi": "3.0.3",
    "info": {"title": "CleanCommute API", "version": "1.0.0"},
    "paths": {
        "/api/v1/health": {
            "get": {"summary": "Health check", "responses": {"200": {"description": "OK"}}}
        },
        "/api/v1/commutes": {
            "get": {
                "summary": "List commutes",
                "parameters": [
                    {"in": "query", "name": "limit", "schema": {"type": "integer"}},
                    {"in": "query", "name": "offset", "schema": {"type": "integer"}},
                ],
                "responses": {"200": {"description": "OK"}},
            },
            "post": {
                "summary": "Create commute",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["origin", "destination"],
                                "properties": {
                                    "origin": {"type": "string"},
                                    "destination": {"type": "string"},
                                    "mode": {
                                        "type": "string",
                                        "enum": ["driving", "transit", "bike", "walk"],
                                    },
                                    "notes": {"type": "string"},
                                },
                            }
                        }
                    },
                },
                "responses": {
                    "201": {"description": "Created"},
                    "401": {"description": "Unauthorized"},
                },
            },
        },
    },
}


def create_app() -> Flask:
    app = Flask(__name__)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)  # type: ignore

    # --- Config
    app.config["API_KEY"] = os.getenv("API_KEY", "")
    app.config["MONGO_URI"] = os.getenv("MONGO_URI", "")
    app.config["MONGO_DB"] = os.getenv("MONGO_DB", "cleancommute")
    app.config["DEFAULT_LIMITS"] = os.getenv("DEFAULT_LIMITS", "100 per 15 minutes")
    app.config["ALLOW_CLEAR"] = os.getenv("ALLOW_CLEAR", "false").lower() == "true"
    app.config["FRONTEND_ORIGIN"] = os.getenv("FRONTEND_ORIGIN", "")
    limiter_storage = os.getenv("LIMITER_STORAGE_URI")

    CORS(app, resources={r"/api/*": {"origins": app.config["FRONTEND_ORIGIN"]}})

    limiter = Limiter(
        key_func=get_remote_address,
        default_limits=[app.config["DEFAULT_LIMITS"]],
        storage_uri=limiter_storage if limiter_storage else None,
    )
    limiter.init_app(app)

    if not app.config["MONGO_URI"]:
        raise RuntimeError("MONGO_URI is required")

    # --- Mongo
    mongo_client = MongoClient(app.config["MONGO_URI"])
    db = mongo_client[app.config["MONGO_DB"]]
    samples_col = db.get_collection("samples")
    commutes_col = db.get_collection("commutes")

    # Indexes
    try:
        commutes_col.create_index([("created_at", -1)])
        commutes_col.create_index([("mode", 1), ("created_at", -1)])
        samples_col.create_index([("created_at", -1)])
    except Exception:
        pass

    # --- Helpers
    def sanitize_mongo(obj: Any) -> Any:
        if isinstance(obj, ObjectId):
            return str(obj)
        if isinstance(obj, list):
            return [sanitize_mongo(x) for x in obj]
        if isinstance(obj, dict):
            return {k: sanitize_mongo(v) for k, v in obj.items()}
        return obj

    def json_ok(payload: Any, status: int = 200):
        return current_app.response_class(
            json.dumps(payload, default=str), mimetype="application/json", status=status
        )

    def require_api_key(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            expected = current_app.config.get("API_KEY", "")
            if expected == "":
                return f(*args, **kwargs)
            provided = request.headers.get("x-api-key")
            if provided != expected:
                resp = json_ok(
                    {"error": "unauthorized", "detail": "Missing or invalid API key"},
                    401,
                )
                resp.headers["WWW-Authenticate"] = 'API-Key realm="clean-commute-api"'
                return resp
            return f(*args, **kwargs)

        return wrapper

    def get_pagination():
        try:
            limit = min(max(int(request.args.get("limit", 50)), 1), 200)
        except ValueError:
            limit = 50
        try:
            offset = max(int(request.args.get("offset", 0)), 0)
        except ValueError:
            offset = 0
        return limit, offset

    # --- Validation models
    class CommuteIn(BaseModel):
        origin: str
        destination: str
        mode: str = Field(default="driving", pattern="^(driving|transit|bike|walk)$")
        notes: str | None = None

    class SampleIn(BaseModel):
        name: str
        meta: dict = {}

    # --- Error handlers
    @app.errorhandler(404)
    def not_found(e):
        return json_ok({"error": "not_found", "detail": str(e)}, 404)

    @app.errorhandler(429)
    def rate_limit_handler(e):
        return json_ok({"error": "rate_limited", "detail": str(e)}, 429)

    @app.errorhandler(Exception)
    def unhandled_error(e):
        return json_ok({"error": "internal_error", "detail": str(e)}, 500)

    # --- Routes
    @app.get("/api/v1/health")
    def health():
        return json_ok({"ok": True, "ts": datetime.utcnow().isoformat()})

    @app.get("/health")
    def health_alias():
        return json_ok({"ok": True, "ts": datetime.utcnow().isoformat()})

    @app.get("/api/v1/db-ping")
    def db_ping():
        try:
            mongo_client.admin.command("ping")
            return json_ok({"ok": True})
        except Exception as exc:
            return json_ok({"ok": False, "detail": str(exc)}, 500)

    @app.get("/api/v1/samples")
    def samples_list():
        limit, offset = get_pagination()
        docs = list(samples_col.find().sort("created_at", -1).skip(offset).limit(limit))
        has_more = samples_col.count_documents({}) > offset + len(docs)
        return json_ok(
            {"data": sanitize_mongo(docs), "next_offset": offset + len(docs), "has_more": has_more}
        )

    @app.post("/api/v1/samples")
    @require_api_key
    def samples_create():
        try:
            payload = SampleIn(**(request.get_json(silent=True) or {})).model_dump()
        except ValidationError as e:
            return json_ok({"error": "bad_request", "detail": e.errors()}, 400)
        doc = {"created_at": datetime.utcnow(), **payload}
        res = samples_col.insert_one(doc)
        doc["_id"] = res.inserted_id
        return json_ok({"data": sanitize_mongo(doc)}, 201)

    @app.post("/api/v1/samples/clear")
    @require_api_key
    def samples_clear():
        if not current_app.config["ALLOW_CLEAR"]:
            return json_ok({"error": "forbidden", "detail": "Clearing disabled"}, 403)
        deleted = samples_col.delete_many({}).deleted_count
        return json_ok({"deleted": deleted})

    @app.get("/api/v1/commutes")
    def commute_list():
        limit, offset = get_pagination()
        items = list(commutes_col.find().sort("created_at", -1).skip(offset).limit(limit))
        has_more = commutes_col.count_documents({}) > offset + len(items)
        return json_ok(
            {"data": sanitize_mongo(items), "next_offset": offset + len(items), "has_more": has_more}
        )

    @app.post("/api/v1/commutes")
    @require_api_key
    def commute_create():
        try:
            payload = CommuteIn(**(request.get_json(silent=True) or {})).model_dump()
        except ValidationError as e:
            return json_ok({"error": "bad_request", "detail": e.errors()}, 400)
        doc = {"created_at": datetime.utcnow(), **payload}
        res = commutes_col.insert_one(doc)
        doc["_id"] = res.inserted_id
        return json_ok({"data": sanitize_mongo(doc)}, 201)

    @app.post("/api/v1/commutes/clear")
    @require_api_key
    def commute_clear():
        if not current_app.config["ALLOW_CLEAR"]:
            return json_ok({"error": "forbidden", "detail": "Clearing disabled"}, 403)
        deleted = commutes_col.delete_many({}).deleted_count
        return json_ok({"deleted": deleted})

    @app.get("/")
    def root():
        return json_ok({"service": "clean-commute-api", "prefix": "/api/v1"})

    @app.get("/openapi.json")
    def openapi():
        return current_app.response_class(
            json.dumps(OPENAPI, default=str), mimetype="application/json"
        )

    @app.get("/docs")
    def docs():
        html = """
<!doctype html><html><head><meta charset="utf-8"><title>CleanCommute API</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/redoc/bundles/redoc.standalone.css">
</head><body>
<redoc spec-url='/openapi.json'></redoc>
<script src="https://cdn.jsdelivr.net/npm/redoc/bundles/redoc.standalone.js"></script>
</body></html>
"""
        return Response(html, mimetype="text/html")

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
