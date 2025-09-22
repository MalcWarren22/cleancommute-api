# app.py
from flask import Flask, jsonify, request
from pymongo import MongoClient, errors
from dotenv import load_dotenv
from datetime import datetime, timezone
import os

# Load env vars for local dev (Heroku uses Config Vars)
load_dotenv(".env")

MONGO_URI = os.getenv("MONGO_URI")
if not MONGO_URI:
    raise RuntimeError("MONGO_URI environment variable is not set")

# Create client (short timeout so failures return fast)
client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000, retryWrites=True)
db = client["cleancommute"]

app = Flask(__name__)

def _serialize(doc: dict) -> dict:
    """Convert non-JSON types (e.g., datetime) to strings."""
    out = {}
    for k, v in doc.items():
        if isinstance(v, datetime):
            out[k] = v.astimezone(timezone.utc).isoformat()
        else:
            out[k] = v
    return out

@app.route("/health")
def health():
    return {"status": "ok"}

@app.route("/db-ping")
def db_ping():
    try:
        db.command("ping")
        return {"db": "ok"}
    except Exception as e:
        return {"db": "error", "detail": str(e)}, 500

# READ
@app.route("/samples", methods=["GET"])
def samples():
    try:
        cursor = db.samples.find({}, {"_id": 0}).sort("createdAt", -1)
        items = [_serialize(d) for d in cursor]
        return jsonify(items)
    except errors.PyMongoError as e:
        return {"error": "db read failed", "detail": str(e)}, 500

# CREATE
@app.route("/samples", methods=["POST"])
def add_sample():
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return {"error": "JSON body must be an object"}, 400

    # Add a server-side timestamp
    data["createdAt"] = datetime.now(timezone.utc)

    try:
        result = db.samples.insert_one(data)
        return {"inserted_id": str(result.inserted_id)}, 201
    except errors.PyMongoError as e:
        return {"error": "db write failed", "detail": str(e)}, 500

if __name__ == "__main__":
    # Local dev server; Heroku uses Gunicorn via Procfile
    app.run(debug=True)
