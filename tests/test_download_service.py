from pathlib import Path

import pytest
import yt_dlp

from services import download_service as ds
from services import runtime as runtime_tools


@pytest.fixture(autouse=True)
def no_ffmpeg(monkeypatch):
    monkeypatch.setattr(runtime_tools, "find_ffmpeg", lambda: None)


def _settings(tmp_path, **kw):
    s = {"download_dir": str(tmp_path / "dl"), "video_quality": "1080p", "video_container": "mp4",
         "max_retries": 2, "sleep_requests": 0, "js_runtime": "node"}
    s.update(kw)
    return s


JOB = {"id": 1, "video_id": "abcdefghijk", "channel_name": "My: Channel?", "video_type": "video",
       "redownload": 0}


class FakeYDL:
    """Simulates yt-dlp: calls progress hooks and writes the output file."""

    def __init__(self, content=b"video-bytes", error=None, info_extra=None, hook_steps=3,
                 ext="mp4", height=360):
        self.content, self.error, self.info_extra = content, error, info_extra or {}
        self.hook_steps, self.ext, self.height = hook_steps, ext, height

    def __call__(self, opts):
        self.opts = opts
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=True):
        self.url = url
        info = {"id": "abcdefghijk", "title": "A title", "ext": self.ext, "height": self.height,
                "upload_date": "20240315", "duration": 125, "channel": "My: Channel?",
                "channel_id": "UCx", "live_status": "not_live", **self.info_extra}
        for f in self.opts.get("match_filter") and [self.opts["match_filter"]] or []:
            if f(info, incomplete=False):
                return info
        if self.error:
            raise self.error
        home = Path(self.opts["paths"]["home"])
        rel = self.opts["outtmpl"]["default"].replace("%%", "%")
        rel = rel.replace("%(upload_date>%Y-%m-%d)s", "2024-03-15").replace("%(title).150B", "A title") \
                 .replace("%(id)s", "abcdefghijk").replace("%(ext)s", self.ext)
        target = home / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        filename = str(target) + ".part"
        for step in range(1, self.hook_steps + 1):
            for hook in self.opts["progress_hooks"]:
                hook({"status": "downloading", "filename": filename, "downloaded_bytes": step * 100,
                      "total_bytes": self.hook_steps * 100, "speed": 1000.0, "eta": self.hook_steps - step,
                      "info_dict": {"vcodec": "avc1", "acodec": "mp4a"}})
        target.write_bytes(self.content)
        for hook in self.opts["progress_hooks"]:
            hook({"status": "finished", "filename": str(target), "info_dict": {}})
        info["requested_downloads"] = [{"filepath": str(target)}]
        return info


def test_successful_download_returns_verified_file(tmp_path):
    fake = FakeYDL()
    updates = []
    result = ds.download_video(JOB, _settings(tmp_path), progress=updates.append, ydl_factory=fake)
    path = Path(result["file_path"])
    assert path.is_file()
    # organised by (sanitised) channel name, id in the file name
    assert path.parent.name == "My_ Channel_"
    assert path.name == "2024-03-15 - A title [abcdefghijk].mp4"
    assert result["file_size"] == len(b"video-bytes")
    assert result["info"]["upload_date"] == "20240315" and result["info"]["duration"] == 125
    assert result["format_desc"] == "360p mp4 (no FFmpeg)"
    pct = [u["progress"] for u in updates if u.get("progress") is not None]
    assert pct == [33.3, 66.7, 100.0]
    assert any(u.get("speed") == 1000.0 for u in updates)
    # yt-dlp options: resume partial files, never overwrite, single-file format without FFmpeg
    assert fake.opts["continuedl"] is True and fake.opts["overwrites"] is False
    assert fake.opts["format"] == "b" and "merge_output_format" not in fake.opts
    assert fake.opts["format_sort"] == ["res:1080", "ext:mp4:m4a"]
    assert fake.url == "https://www.youtube.com/watch?v=abcdefghijk"


def test_shorts_go_to_subfolder(tmp_path):
    result = ds.download_video({**JOB, "video_type": "short"}, _settings(tmp_path), ydl_factory=FakeYDL())
    assert Path(result["file_path"]).parent.name == "Shorts"


def test_format_options_with_ffmpeg():
    opts, label = ds.format_options({"video_quality": "720p", "video_container": "mkv"}, has_ffmpeg=True)
    assert opts["format"] == "bv*+ba/b" and opts["merge_output_format"] == "mkv"
    assert opts["format_sort"] == ["res:720"]
    opts, _ = ds.format_options({"video_quality": "best", "video_container": "any"}, has_ffmpeg=True)
    assert opts["format_sort"] == [] and "merge_output_format" not in opts


def test_cancel_and_pause_raise_stop_requested(tmp_path):
    for reason in ("cancel", "pause"):
        with pytest.raises(ds.StopRequested) as exc:
            ds.download_video(JOB, _settings(tmp_path), should_stop=lambda r=reason: r, ydl_factory=FakeYDL())
        assert exc.value.reason == reason


def test_download_error_propagates(tmp_path):
    fake = FakeYDL(error=yt_dlp.utils.DownloadError("ERROR: HTTP Error 429: Too Many Requests"))
    with pytest.raises(yt_dlp.utils.DownloadError):
        ds.download_video(JOB, _settings(tmp_path), ydl_factory=fake)


def test_missing_ffmpeg_gives_clear_non_retryable_error(tmp_path):
    from services.errors import classify_error
    fake = FakeYDL(error=yt_dlp.utils.DownloadError(
        "ERROR: [youtube] abcdefghijk: Requested format is not available. Use --list-formats"))
    with pytest.raises(ds.FFmpegRequired) as exc:
        ds.download_video(JOB, _settings(tmp_path), ydl_factory=fake)
    info = classify_error(exc.value)
    assert info.kind == "ffmpeg" and info.retryable is False and not info.pause_queue
    assert "winget install Gyan.FFmpeg" in str(exc.value)


def test_format_error_with_ffmpeg_is_not_rewritten(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_tools, "find_ffmpeg", lambda: {"ffmpeg": "f", "ffprobe": None, "dir": "d"})
    fake = FakeYDL(error=yt_dlp.utils.DownloadError("ERROR: Requested format is not available"))
    with pytest.raises(yt_dlp.utils.DownloadError):
        ds.download_video(JOB, _settings(tmp_path), ydl_factory=fake)
    assert fake.opts["ffmpeg_location"] == "d" and fake.opts["format"] == "bv*+ba/b"


def test_live_stream_is_skipped(tmp_path):
    with pytest.raises(ds.SkippedVideo):
        ds.download_video(JOB, _settings(tmp_path), ydl_factory=FakeYDL(info_extra={"live_status": "is_live"}))


def test_empty_file_fails_integrity(tmp_path):
    with pytest.raises(ds.IntegrityError):
        ds.download_video(JOB, _settings(tmp_path), ydl_factory=FakeYDL(content=b""))


def test_existing_and_partial_file_detection(tmp_path):
    dl = tmp_path / "dl"
    folder = dl / "My_ Channel_"
    folder.mkdir(parents=True)
    (folder / "2024-01-01 - x [abcdefghijk].mp4.part").write_bytes(b"partial")
    (folder / "2024-01-01 - x [abcdefghijk].f137.mp4").write_bytes(b"stream")
    (folder / "other [zzzzzzzzzzz].mp4").write_bytes(b"other")
    assert ds.find_existing_file(dl, JOB) is None          # partial files never count as done
    assert len(ds.partial_files(dl, JOB)) == 2
    done = folder / "2024-01-01 - x [abcdefghijk].mp4"
    done.write_bytes(b"done")
    assert ds.find_existing_file(dl, JOB) == done
    assert ds.remove_partial_files(dl, JOB) == 2
    assert done.exists() and (folder / "other [zzzzzzzzzzz].mp4").exists()


def test_verify_rejects_leftover_part_and_outside_paths(tmp_path):
    dl = tmp_path / "dl"
    dl.mkdir()
    f = dl / "a.mp4"
    f.write_bytes(b"x")
    Path(str(f) + ".part").write_bytes(b"x")
    with pytest.raises(ds.IntegrityError):
        ds.verify_file(f, dl, None)
    outside = tmp_path / "b.mp4"
    outside.write_bytes(b"x")
    with pytest.raises(ds.IntegrityError):
        ds.verify_file(outside, dl, None)


def test_redownload_replaces_old_file_only_after_success(tmp_path):
    dl = tmp_path / "dl"
    folder = dl / "My_ Channel_"
    folder.mkdir(parents=True)
    old = folder / "2024-03-15 - A title [abcdefghijk].webm"
    old.write_bytes(b"old")
    # failure: old file must survive
    with pytest.raises(yt_dlp.utils.DownloadError):
        ds.download_video({**JOB, "redownload": 1}, _settings(tmp_path),
                          ydl_factory=FakeYDL(error=yt_dlp.utils.DownloadError("ERROR: timed out")))
    assert old.read_bytes() == b"old"
    result = ds.download_video({**JOB, "redownload": 1}, _settings(tmp_path), ydl_factory=FakeYDL(content=b"new"))
    assert Path(result["file_path"]).name == "2024-03-15 - A title [abcdefghijk].mp4"
    assert Path(result["file_path"]).read_bytes() == b"new"
    assert not old.exists()


@pytest.mark.parametrize("name, expected", [
    ("../../Windows", "_.._Windows"), ("CON", "CON_"), ("  ...  ", "Unknown channel"),
    (None, "Unknown channel"), ("a/b\\c", "a_b_c"),
])
def test_safe_folder_name(name, expected):
    assert ds.safe_folder_name(name) == expected
