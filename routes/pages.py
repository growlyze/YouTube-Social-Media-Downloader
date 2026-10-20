"""HTML pages. Pages render a shell; data is loaded from the JSON API."""

from flask import Blueprint, current_app, render_template

import config
from database import repository
from services import drive_auth, queue_control
from services import runtime as runtime_tools

bp = Blueprint("pages", __name__)


@bp.get("/")
def dashboard():
    settings = repository.get_settings()
    return render_template("dashboard.html", active="dashboard",
                           stats=repository.count_videos(),
                           sources=repository.list_sources()[:8],
                           jobs=repository.list_discovery_jobs(8),
                           settings=settings,
                           downloads=repository.download_counts(),
                           uploads=repository.upload_counts(),
                           drive_connected=drive_auth.is_connected(),
                           queue_running=current_app.extensions["download_worker"].is_running(),
                           runtime=runtime_tools.status(settings.get("js_runtime", "node")),
                           pause=queue_control.status(),
                           data_dir=config.DATA_DIR)


@bp.get("/downloads")
def downloads_page():
    return render_template("downloads.html", active="downloads",
                           settings=repository.get_settings())


@bp.get("/local")
def local_page():
    return render_template("local_files.html", active="local",
                           settings=repository.get_settings())


@bp.get("/history")
def history_page():
    return render_template("history.html", active="history")


@bp.get("/uploads")
def uploads_page():
    return render_template("uploads.html", active="uploads",
                           settings=repository.get_settings())


@bp.get("/library")
def library():
    return render_template("library.html", active="library",
                           sources=repository.list_sources(),
                           settings=repository.get_settings())


@bp.get("/settings")
def settings_page():
    return render_template("settings.html", active="settings",
                           settings=repository.get_settings(),
                           qualities=config.VIDEO_QUALITIES,
                           containers=config.VIDEO_CONTAINERS,
                           runtimes=config.JS_RUNTIMES,
                           data_dir=config.DATA_DIR)

