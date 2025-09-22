import os
from flask import Flask, request, current_app, Response
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from pymongo import MongoClient
from emissions import estimate_emissions
from urllib.parse import urlparse


def create_app():
    app = Flask(__name__)
    CORS(app)

    # --- MongoDB Setup ---
    mongo_uri = os.environ.get("MONGO_URI")
    if not mongo_uri:
        raise RuntimeError("MONGO_URI is not set")

    mongo_client = MongoClient(mongo_uri)

    # Extract DB name from URI (fallback to "cleancommute")
    parsed = urlparse(mongo_uri)
    db_name = parsed.path.lstrip("/") or "cleancommute"
    app.db = mongo_client[db_name]

    # --- Rate Limiting ---
    limiter = Limiter(
        get_remote_address,
        app=app,
        storage_uri=os.environ.get("LIMITER_STORAGE_URI", "memory://"),
        default_limits=[os.environ.get("DEFAULT_LIMITS", "60 per minute")],
    )

    # --- Routes ---
    @app.route("/api/v1/health")
    def health():
        """Simple health check with DB name for debugging."""
        return {"status": "ok", "db": db_name}, 200

    @app.route("/api/v1/commutes", methods=["POST"])
    @limiter.limit("10 per minute")
    def add_commute():
        """Add a commute record and return emissions estimate."""
        data = request.json or {}
        distance = data.get("distance_km", 0)
        mode = data.get("mode", "car")
        passengers = data.get("passengers", 1)

        estimate = estimate_emissions(distance, mode, passengers=passengers)
        app.db.commutes.insert_one({**data, **estimate})
        return estimate, 201

    @app.route("/api/v1/commutes", methods=["GET"])
    def list_commutes():
        """List all commute records."""
        commutes = list(app.db.commutes.find({}, {"_id": 0}))
        return {"commutes": commutes}

    @app.route("/api/v1/commutes/clear", methods=["POST"])
    def clear_commutes():
        """Clear all commute records (dev only)."""
        app.db.commutes.delete_many({})
        return {"cleared": True}

    return app


# Entrypoint for Heroku / gunicorn
app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
