from flask import Flask, jsonify, request
from pymongo import MongoClient, errors
from dotenv import load_dotenv
from datetime import datetime, timezone
import os

load_dotenv(".env")
MONGO_URI = os.getenv("MONGO_URI")

client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000, retryWrites=True) if MONGO_URI else None
db = client["cleancommute"] if client else None

app = Flask(__name__)

def _serialize(doc):
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
    if not db:
        return {"db": "missing_config"}, 503
    try:
        db.command("ping")
        return {"db": "ok"}
    except Exception as e:
        return {"db": "error", "detail": str(e)}, 500

@app.route("/samples", methods=["GET"])
def samples():
    if not db:
        return {"error": "db not configured"}, 503
    try:
        cursor = db.samples.find({}, {"_id": 0}).sort("createdAt", -1)
        return jsonify([_serialize(d) for d in cursor])
    except errors.PyMongoError as e:
        return {"error": "db read failed", "detail": str(e)}, 500

@app.route("/samples", methods=["POST"])
def add_sample():
    if not db:
        return {"error": "db not configured"}, 503
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return {"error": "JSON body must be an object"}, 400
    data["createdAt"] = datetime.now(timezone.utc)
    try:
        result = db.samples.insert_one(data)
        return {"inserted_id": str(result.inserted_id)}, 201
    except errors.PyMongoError as e:
        return {"error": "db write failed", "detail": str(e)}, 500

if __name__ == "__main__":
    app.run(debug=True)

