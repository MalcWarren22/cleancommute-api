from flask import Flask, jsonify
app = Flask(__name__)

@app.get("/health")
def health():
    return jsonify({"status": "ok"})

# Dev entrypoint; gunicorn uses 'app' above in Procfile
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=3000)
