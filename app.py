"""Local media downloader: Flask entry point.

Run with:  .venv\\Scripts\\python app.py   then open http://127.0.0.1:5000
"""

import logging
import re
import secrets
from logging.handlers import RotatingFileHandler

from flask import Flask, abort, jsonify, request

import config
from database import db, repository
from routes.channels import bp as channels_bp
from routes.downloads import bp as downloads_bp
from routes.drive import bp as drive_bp
from routes.drive import oauth_bp
from routes.pages import bp as pages_bp
from routes.settings import bp as settings_bp
from routes.space import bp as space_bp
from workers.discovery_worker import DiscoveryWorker
from workers.download_worker import DownloadWorker
from workers.space_worker import SpaceCheckWorker
from workers.upload_worker import UploadWorker


class RedactFilter(logging.Filter):
    """Strip OAuth tokens and similar secrets from log records."""
    _patterns = [
        (re.compile(r"ya29\.[\w\-.]+"), "ya29.[REDACTED]"),
        (re.compile(r"(?i)(access_token|refresh_token|client_secret|authorization)"
                    r"([\"']?\s*[:=]\s*[\"']?)[^\s\"',}]+"), r"\1\2[REDACTED]"),
    ]

    def filter(self, record):
        message = record.getMessage()
        for pattern, replacement in self._patterns:
            message = pattern.sub(replacement, message)
        record.msg, record.args = message, None
        return True


def setup_logging():
    config.ensure_dirs()
    root = logging.getLogger()
    if any(getattr(h, "_ytdl", False) for h in root.handlers):
        return
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    file_handler = RotatingFileHandler(config.LOG_DIR / "app.log", maxBytes=2_000_000,
                                       backupCount=3, encoding="utf-8")
    console = logging.StreamHandler()
    for handler in (file_handler, console):
        handler.setFormatter(fmt)
        handler.addFilter(RedactFilter())
        handler._ytdl = True
        root.addHandler(handler)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)


def auto_download_source(source_id, download_worker):
    """Queue every discovered video of a channel/playlist and start the queue.

    Adding a channel or playlist therefore starts downloading by itself.
    Videos that are already queued, downloaded, or unavailable are skipped by
    `enqueue_downloads`, so re-discovering a source adds only what is new."""
    settings = repository.get_settings()
    ids = repository.matching_video_ids(
        repository.VideoFilter(source_id=source_id, availability="available"), limit=100000)
    added = 0
    for start in range(0, len(ids), 5000):
        result = repository.enqueue_downloads(
            ids[start:start + 5000],
            include_shorts=settings.get("include_shorts", True),
            include_live=settings.get("include_streams", False),
            download_dir=settings["download_dir"])
        added += result["added"]
    if added:
        logging.getLogger("app").info(
            "Auto-queued %d video(s) from %s; starting the download queue", added, source_id)
        download_worker.start_queue()
        download_worker.wake()
    return added


def create_app(db_path=None, start_workers=True, discovery_worker=None, download_worker=None,
               upload_worker=None, space_worker=None):
    app = Flask(__name__)
    app.config["CSRF_TOKEN"] = secrets.token_urlsafe(32)
    app.config["JSON_SORT_KEYS"] = False

    db.init_db(db_path or config.DB_PATH)

    app.extensions["upload_worker"] = upload_worker or UploadWorker()
    app.extensions["download_worker"] = download_worker or DownloadWorker(
        upload_waker=app.extensions["upload_worker"].wake)
    app.extensions["discovery_worker"] = discovery_worker or DiscoveryWorker(
        on_discovered=lambda source_id: auto_download_source(
            source_id, app.extensions["download_worker"]))
    app.extensions["space_worker"] = space_worker or SpaceCheckWorker()
    if start_workers:
        app.extensions["discovery_worker"].start()
        app.extensions["download_worker"].start()
        app.extensions["upload_worker"].start()
        app.extensions["space_worker"].start()

    allowed_hosts = {f"127.0.0.1:{config.PORT}", f"localhost:{config.PORT}",
                     "127.0.0.1", "localhost"}

    @app.before_request
    def protect_local_api():
        # Reject requests whose Host header is not this machine (DNS rebinding).
        if not app.testing and request.host not in allowed_hosts:
            abort(400, "Invalid host header")
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("Origin")
            if origin and re.sub(r"^https?://", "", origin) != request.host:
                abort(403, "Cross-origin request blocked")
            # A random per-run token embedded in our pages; other websites
            # open in the browser cannot read it, so they cannot forge it.
            if request.headers.get("X-CSRF-Token") != app.config["CSRF_TOKEN"]:
                abort(403, "Missing or invalid CSRF token")

    @app.after_request
    def security_headers(resp):
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        # Revalidate every time so the browser never runs stale JS/CSS after an update.
        resp.headers.setdefault("Cache-Control", "no-cache")
        if request.endpoint == "pages.settings_page":
            # The Google Picker (folder chooser) loads Google's script, injects
            # its own styled dialog and shows a Google-hosted frame. Only the
            # Settings page needs this; all other pages keep the strict policy.
            resp.headers["Content-Security-Policy"] = (
                "default-src 'self'; "
                "script-src 'self' https://apis.google.com https://www.gstatic.com; "
                "style-src 'self' 'unsafe-inline' https://www.gstatic.com; "
                "img-src 'self' data: https://*.gstatic.com https://*.googleusercontent.com "
                "https://*.google.com; "
                "frame-src https://docs.google.com https://drive.google.com https://*.google.com "
                "https://content.googleapis.com; "
                "connect-src 'self' https://*.googleapis.com; frame-ancestors 'none'"
            )
        else:
            resp.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "img-src 'self' data: https://i.ytimg.com https://*.ggpht.com "
                "https://*.googleusercontent.com https://*.cdninstagram.com https://*.fbcdn.net "
                "https://*.fbsbx.com https://*.tiktokcdn.com https://*.byteoversea.com "
                "https://*.ibytedtos.com https://pbs.twimg.com; "
                "connect-src 'self'; frame-ancestors 'none'"
            )
        return resp

    @app.errorhandler(400)
    @app.errorhandler(403)
    @app.errorhandler(404)
    @app.errorhandler(405)
    def http_error(err):
        if request.path.startswith("/api/"):
            return jsonify(error=err.description), err.code
        return err

    @app.context_processor
    def inject_globals():
        return {"csrf_token": app.config["CSRF_TOKEN"], "docker_mode": config.IN_DOCKER,
                "downloads_label": config.DOWNLOADS_LABEL}

    app.register_blueprint(pages_bp)
    app.register_blueprint(channels_bp, url_prefix="/api")
    app.register_blueprint(downloads_bp, url_prefix="/api")
    app.register_blueprint(drive_bp, url_prefix="/api")
    app.register_blueprint(oauth_bp)
    app.register_blueprint(settings_bp, url_prefix="/api")
    app.register_blueprint(space_bp, url_prefix="/api")
    return app


def main():
    setup_logging()
    app = create_app()
    logging.getLogger("app").info("Open http://%s:%s in your browser", config.HOST, config.PORT)
    # threaded=True so polling requests are served while workers run.
    app.run(host=config.HOST, port=config.PORT, debug=False, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
