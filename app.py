import os
from functools import wraps
from datetime import datetime
from typing import Any

from flask import Flask, request, current_app
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from pymongo import MongoClient
from bson import ObjectId
from werkzeug.middleware.proxy_fix import ProxyFix


def create_app() -> Flask:
    app = Flask(__name__)

    # Security headers, proxy fix for Heroku
    app.wsgi_app = ProxyFix(app.wsgi_app)

    # Enable CORS
    CORS(app, origins=os.getenv("ALLOWED_ORIGINS", "*").split(","))

    # Rate limiting
    limiter = Limiter(
        key_func=get_remote_address,
        default_limits=[os.getenv("DEFAULT_LIMITS", "200 per day;50 per hour")],
        storage_uri=os.getenv("LIMITER_STORAGE_URI", "memory://"),
    )
    limiter.init_app(app)

    # MongoDB setup
    mongo_client = MongoClient(os.getenv("MONGO_URI"))
    app.db = mongo_client.get_default_database()

    # Health route
    @app.route("/api/v1/health")
    def health():
        return {"status": "ok", "ts": datetime.utcnow().isoformat()}

    # Example protected route
    def require_api_key(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            api_key = os.getenv("API_KEY")
            if request.headers.get("x-api-key") != api_key:
                return {"error": "unauthorized"}, 401
            return f(*args, **kwargs)
        return decorated

    @app.route("/api/v1/samples", methods=["GET"])
    @require_api_key
    def get_samples():
        items = list(app.db.samples.find())
        for item in items:
            item["_id"] = str(item["_id"])
        return {"samples": items}

    return app


app = create_app()

if __name__ == "__main__":
    app.run(debug=True)
