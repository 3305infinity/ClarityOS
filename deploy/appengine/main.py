"""
Moonwalk — App Engine entrypoint.
Serves static HTML pages and redirects /releases/* to GCS.
"""
import os
from flask import Flask, send_from_directory, redirect, abort

app = Flask(__name__)

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "public")
GCS_BUCKET = "getmoonwalk.top"
GCS_BASE = f"https://storage.googleapis.com/{GCS_BUCKET}"


# ── Root / landing page ─────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


# ── Blog pages ──────────────────────────────────────────────
@app.route("/blog/how-we-made-moonwalk")
@app.route("/blog/how-we-made-moonwalk/")
def blog_how_we_made():
    return send_from_directory(
        os.path.join(STATIC_DIR, "blog", "how-we-made-moonwalk"), "index.html"
    )


# ── Redirect binary downloads to GCS ────────────────────────
@app.route("/releases/<path:path>")
def releases(path):
    return redirect(f"{GCS_BASE}/releases/{path}", code=302)


# ── Redirect extension download ─────────────────────────────
@app.route("/downloads/<path:path>")
def downloads(path):
    return redirect(f"{GCS_BASE}/downloads/{path}", code=302)


# ── Health check for App Engine ──────────────────────────────
@app.route("/_ah/health")
def health():
    return "ok", 200


# ── 404 ─────────────────────────────────────────────────────
@app.errorhandler(404)
def not_found(e):
    return (
        '<html><body style="font-family:system-ui;text-align:center;padding:80px">'
        "<h1>404</h1><p>Page not found.</p>"
        '<a href="/" style="color:#007aff">← Back to Moonwalk</a></body></html>',
        404,
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), debug=True)
