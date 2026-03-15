from flask import Flask, jsonify
from waitress import serve
import os

app = Flask(__name__)

_health_provider = None

def set_health_provider(provider):
    global _health_provider
    _health_provider = provider

@app.get("/")
def index():
    return "ok", 200

@app.get("/health")
def health():
    if callable(_health_provider):
        try:
            return jsonify(_health_provider()), 200
        except Exception as e:
            return jsonify({
                "ok": False,
                "healthy": False,
                "error": str(e),
            }), 500
    return jsonify({"ok": True}), 200

def run_webserver():
    port = int(os.getenv("PORT", "10000"))
    print(f"[webserver] usando porta {port}")
    serve(app, host="0.0.0.0", port=port, threads=4)
