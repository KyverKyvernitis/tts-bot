from flask import Flask


def create_app() -> Flask:
    app = Flask(__name__)

    @app.get("/")
    def home():
        return "OK", 200

    @app.head("/")
    def healthcheck():
        return "", 200

    return app


if __name__ == "__main__":
    app = create_app()
    app.run(host="0.0.0.0", port=10000)
