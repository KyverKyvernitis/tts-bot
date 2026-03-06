from flask import Flask

def create_app() -> Flask:
    app = Flask(__name__)

    @app.get("/")
    def home():
        return "OK", 200

    @app.get("/health")
    def health():
        return "healthy", 200

    return app
