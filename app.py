# app.py
from flask import Flask, jsonify, request
from pymongo import MongoClient, errors
from dotenv import load_dotenv
from datetime import datetime, timezone
import os

# Load env vars for local dev (.env). On Heroku, Config Vars are used.
load_dotenv(".env")

MONGO_URI = os.getenv("MONGO_URI")
if not MONGO_URI:
    raise RuntimeError("MONGO_URI is not set")

# Fast-fail if DB is unreachable
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

# READ: list samples newest first (hide _id)
@app.route("/samples", methods=["GET"])
def samples():
    try:
        cursor = db.samples.find({}, {"_id": 0}).sort("createdAt", -1)
        return jsonify(
