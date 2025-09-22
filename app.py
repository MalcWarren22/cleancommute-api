from flask import Flask, jsonify, request   # add request here
from pymongo import MongoClient
from dotenv import load_dotenv
import os

# Load env vars (works locally, ignored on Heroku)
load_dotenv(".env")

# Get URI from env
MONGO_URI = os.getenv("MONGO_URI")

# Init client + DB
client = MongoClient(MONGO_URI)
db = client["cleancommute"]

app = Flask(__name__)

@app.route("/health")
def health():
    return {"status": "ok"}

@app.route("/db-ping")
def db_ping():
    try:
        db.command("ping")
        return {"db": "ok"}
    except Exception as e:
        return {"db": "error", "detail": str(e)}

@app.route("/samples", methods=["GET"])
def samples():
    items = list(db.samples.find({}, {"_id": 0}))
    return jsonify(items)

@app.route("/samples", methods=["POST"])
def add_sample():
    data = request.get_json(silent=True)
    if not data:
        return {"error": "JSON body required"}, 400
    result = db.samples.insert_one(data)
    return {"inserted_id": str(result.inserted_id)}, 201

if __name__ == "__main__":
    app.run(debug=True)
