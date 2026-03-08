import os
from flask import Flask

app = Flask(__name__)

@app.get("/")
def home():
    return "OK", 200

def run_webserver():
    port = int(os.getenv("PORT", "10000"))
    print("WEB SERVER INICIANDO")
    print(f"[webserver] usando porta {port}")
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
