"""WSGI entry point for production servers (gunicorn, uwsgi).

Exposes a module-level ``app`` that a WSGI server can import and serve, e.g.
``gunicorn wsgi:app``. Do not use Flask's built-in server in production.
"""

from app import create_app

app = create_app()

if __name__ == "__main__":
    # Local development only. debug stays False by default; set FLASK_DEBUG=1
    # deliberately if you need the reloader, and never expose it publicly.
    import os

    debug = os.environ.get("FLASK_DEBUG") == "1"
    app.run(host="127.0.0.1", port=5000, debug=debug)
