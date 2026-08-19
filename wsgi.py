"""WSGI entry point for production servers (gunicorn, uwsgi).

Exposes a module-level ``app`` that a WSGI server can import and serve, e.g.
``gunicorn wsgi:app``. Do not use Flask's built-in server in production.
"""

from dotenv import load_dotenv

# Load .env into the environment before the app factory reads it, so local runs
# don't need SECRET_KEY passed on the command line. In production the platform
# provides real environment variables and no .env file is present, which is fine
# — load_dotenv is a no-op when there's nothing to load.
load_dotenv()

from app import create_app  # noqa: E402  (import after load_dotenv is intentional)

app = create_app()

if __name__ == "__main__":
    # Local development only. debug stays False by default; set FLASK_DEBUG=1
    # deliberately if you need the reloader, and never expose it publicly.
    import os

    debug = os.environ.get("FLASK_DEBUG") == "1"
    app.run(host="127.0.0.1", port=5000, debug=debug)
