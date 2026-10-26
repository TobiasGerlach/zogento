"""Exercise browser job orchestration and its local request boundaries."""

import re
import threading
import time
from urllib.parse import unquote

import pytest

from immich_book_ocr import web


@pytest.fixture
def client(tmp_path):
    app = web.create_app(tmp_path / "jobs")
    app.config["TESTING"] = True
    return app.test_client()


def _headers(client):
    page = client.get("/").get_data(as_text=True)
    token = re.search(r'name="csrf-token" content="([^"]+)"', page).group(1)
    return {"X-CSRF-Token": token, "Origin": "http://localhost"}


def _finished(client, job_id):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        response = client.get(f"/api/jobs/{job_id}")
        assert response.status_code == 200
        job = response.get_json()
        if job["status"] != "running":
            return job
        time.sleep(0.01)
    pytest.fail("Background job did not finish")


def _report(output):
    (output / "originals").mkdir(exist_ok=True)
    (output / "previews").mkdir(exist_ok=True)
    (output / "previews" / "1.jpg").write_bytes(b"image-preview")
    (output / "book.txt").write_text("A recognized book.\n", encoding="utf-8")
    return {
        "title": "<script>alert('book')</script>",
        "complete": True,
        "scans": 1,
        "pages": 2,
        "warnings": [],
        "entries": [
            {"source": "scan1.jpg", "preview": "previews/1.jpg", "split_fraction": 0.47,
             "split_confidence": 0.9, "warnings": [], "pages": []}
        ],
    }


def test_local_request_protection(client, monkeypatch):
    called = []
    monkeypatch.setattr(web, "run_album", lambda *args, **kwargs: called.append(True))
    payload = {"url": "https://immich.example/share/private"}
    assert client.post("/api/jobs", json=payload).status_code == 403
    headers = _headers(client)
    assert client.post("/api/jobs", json=payload, headers={**headers, "Origin": "https://evil.example"}).status_code == 403
    assert client.get("/", headers={"Host": "evil.example"}).status_code == 403
    assert client.post("/api/jobs", json=payload, headers={**headers, "X-CSRF-Token": "wrong"}).status_code == 403
    assert not called
    assert "frame-ancestors 'none'" in client.get("/").headers["Content-Security-Policy"]


def test_job_download_and_cached_split_correction(client, monkeypatch):
    calls = []

    def run_album(url, output, **kwargs):
        calls.append(("album", url, kwargs["options"]))
        kwargs["progress"]("Recognizing the first page")
        return _report(output)

    def run_local(input_dir, output, **kwargs):
        calls.append(("local", input_dir, kwargs["options"]))
        assert input_dir == output / "originals"
        assert kwargs["options"].split_overrides == {"scan1.jpg": 0.62}
        result = _report(output)
        result["entries"][0]["split_fraction"] = 0.62
        return result

    monkeypatch.setattr(web, "run_album", run_album)
    monkeypatch.setattr(web, "run_local", run_local)
    headers = _headers(client)
    response = client.post("/api/jobs", json={"url": "https://immich.example/share/private", "language": "deu+eng", "password": "1"}, headers=headers)
    assert response.status_code == 202
    job_id = response.get_json()["id"]
    job = _finished(client, job_id)
    assert job["status"] == "completed"
    assert job["report"]["pages"] == 2
    assert job["report"]["entries"][0]["source"] == "scan1.jpg"
    assert calls[0][2].language == "deu+eng"
    assert calls[0][2].order == "date"
    text = client.get(f"/api/jobs/{job_id}/text")
    assert text.get_data(as_text=True) == "A recognized book.\n"
    assert "attachment" in text.headers["Content-Disposition"]
    assert client.get(f"/api/jobs/{job_id}/preview/0").data == b"image-preview"
    response = client.post(f"/api/jobs/{job_id}/rerun", json={"splits": {"scan1.jpg": 0.62}}, headers=headers)
    assert response.status_code == 202
    assert _finished(client, job_id)["report"]["entries"][0]["split_fraction"] == 0.62
    assert [call[0] for call in calls] == ["album", "local"]


def test_link_alone_uses_both_languages_and_capture_date(client, monkeypatch):
    calls = []

    def run_album(url, output, **kwargs):
        calls.append(kwargs)
        return _report(output)

    monkeypatch.setattr(web, "run_album", run_album)
    response = client.post("/api/jobs", json={"url": "https://immich.example/share/book"}, headers=_headers(client))
    assert response.status_code == 202
    assert _finished(client, response.get_json()["id"])["status"] == "completed"
    assert calls[0]["options"].language == "eng+deu"
    assert calls[0]["options"].order == "date"
    assert calls[0]["password"] is None
    assert calls[0]["api_key"] is None


def test_credentials_and_album_link_are_removed_from_job_errors(client, monkeypatch):
    secret_url = "https://immich.example/share/a-private-share-key"
    api_key = "very-private-api-key"
    password = "very-private-password"

    def failed(url, output, **kwargs):
        kwargs["progress"](f"Contacting {url} with {kwargs['api_key']} and {kwargs['password']}")
        raise RuntimeError(f"Request to {url} failed: key={kwargs['api_key']}; password={kwargs['password']}")

    monkeypatch.setattr(web, "run_album", failed)
    response = client.post("/api/jobs", json={"url": secret_url, "api_key": api_key, "password": password}, headers=_headers(client))
    job_id = response.get_json()["id"]
    job = _finished(client, job_id)
    assert job["status"] == "failed"
    public = client.get(f"/api/jobs/{job_id}").get_data(as_text=True)
    assert secret_url not in public
    assert api_key not in public
    assert password not in public
    assert client.get(f"/api/jobs/{job_id}/text").status_code == 409


def test_only_one_album_can_run_at_a_time(client, monkeypatch):
    release = threading.Event()

    def waiting(url, output, **kwargs):
        assert release.wait(timeout=3)
        return _report(output)

    monkeypatch.setattr(web, "run_album", waiting)
    headers = _headers(client)
    payload = {"url": "https://immich.example/share/book"}
    first = client.post("/api/jobs", json=payload, headers=headers)
    try:
        assert first.status_code == 202
        second = client.post("/api/jobs", json=payload, headers=headers)
        assert second.status_code == 409
        assert second.get_json()["job_id"] == first.get_json()["id"]
    finally:
        release.set()
    assert _finished(client, first.get_json()["id"])["status"] == "completed"


def test_invalid_options_are_rejected_before_job_starts(client, monkeypatch):
    monkeypatch.setattr(web, "run_album", lambda *args, **kwargs: pytest.fail("Should not start an invalid job"))
    headers = _headers(client)
    payload = {"url": "https://immich.example/share/book"}
    for change in ({"url": "file:///etc/passwd"}, {"url": "https://name:secret@example.com"}, {"language": "eng;anything"}, {"order": "random"}, {"reading_order": "wrong"}, {"single_page": "false"}):
        assert client.post("/api/jobs", json={**payload, **change}, headers=headers).status_code == 400


def test_preview_paths_cannot_escape_job_directory(client, monkeypatch, tmp_path):
    (tmp_path / "outside.jpg").write_bytes(b"private")

    def malicious_path(url, output, **kwargs):
        report = _report(output)
        report["entries"][0]["preview"] = str(tmp_path / "outside.jpg")
        return report

    monkeypatch.setattr(web, "run_album", malicious_path)
    headers = _headers(client)
    response = client.post("/api/jobs", json={"url": "https://immich.example/share/book"}, headers=headers)
    job_id = response.get_json()["id"]
    _finished(client, job_id)
    assert client.get(f"/api/jobs/{job_id}/preview/0").status_code == 404
    assert client.post(f"/api/jobs/{job_id}/rerun", json={"splits": {"unknown.jpg": 0.5}}, headers=headers).status_code == 400
    assert client.post(f"/api/jobs/{job_id}/rerun", json={"splits": {"scan1.jpg": 0.99}}, headers=headers).status_code == 400


def test_retry_failed_album_reuses_its_download_directory(client, monkeypatch):
    outputs = []

    def interrupted(url, output, **kwargs):
        outputs.append(output)
        (output / "originals").mkdir(exist_ok=True)
        (output / "originals" / "downloaded.jpg").write_bytes(b"already downloaded")
        raise RuntimeError("The connection was interrupted")

    def resumed(url, output, **kwargs):
        outputs.append(output)
        assert (output / "originals" / "downloaded.jpg").read_bytes() == b"already downloaded"
        assert kwargs["password"] == "retried-password"
        assert kwargs["options"].language == "deu"
        return _report(output)

    headers = _headers(client)
    monkeypatch.setattr(web, "run_album", interrupted)
    payload = {"url": "https://immich.example/share/book"}
    first = client.post("/api/jobs", json=payload, headers=headers)
    job_id = first.get_json()["id"]
    assert _finished(client, job_id)["status"] == "failed"
    monkeypatch.setattr(web, "run_album", resumed)
    retry = client.post("/api/jobs", json={**payload, "resume_job": job_id, "language": "deu", "password": "retried-password"}, headers=headers)
    assert retry.status_code == 202
    assert retry.get_json()["id"] == job_id
    assert _finished(client, job_id)["status"] == "completed"
    assert outputs[0] == outputs[1]
    assert client.post("/api/jobs", json={**payload, "resume_job": job_id}, headers=headers).status_code == 400


def _titled_report(output, title):
    report = _report(output)
    report["title"] = title
    return report


@pytest.mark.parametrize("title,folder", [
    ("Effektives Bibelstudium", "effektives_bibelstudium"),
    ("Müller: Der Römerbrief (2. Aufl.)", "müller_der_römerbrief_2_aufl"),
])
def test_finished_job_folder_is_named_after_the_book(client, monkeypatch, tmp_path, title, folder):
    monkeypatch.setattr(web, "run_album", lambda url, output, **kw: _titled_report(output, title))
    headers = _headers(client)
    response = client.post("/api/jobs", json={"url": "https://immich.example/share/book"}, headers=headers)
    job_id = response.get_json()["id"]
    assert _finished(client, job_id)["status"] == "completed"
    assert (tmp_path / "jobs" / folder / "book.txt").is_file()
    assert not (tmp_path / "jobs" / job_id).exists()
    # The renamed folder must still serve its own files.
    assert client.get(f"/api/jobs/{job_id}/preview/0").status_code == 200
    download = client.get(f"/api/jobs/{job_id}/text")
    assert download.status_code == 200
    # A non-ASCII name arrives RFC 5987 encoded, alongside a folded fallback.
    disposition = download.headers["Content-Disposition"]
    extended = re.search(r"filename\*=UTF-8''([^;]+)", disposition)
    sent = unquote(extended.group(1)) if extended else re.search(r'filename="?([^";]+)', disposition).group(1)
    assert sent == f"{folder}.txt"


def test_a_second_copy_of_a_book_does_not_overwrite_the_first(client, monkeypatch, tmp_path):
    monkeypatch.setattr(web, "run_album", lambda url, output, **kw: _titled_report(output, "Doppelt"))
    headers = _headers(client)
    folders = []
    for _ in range(2):
        response = client.post("/api/jobs", json={"url": "https://immich.example/share/book"}, headers=headers)
        assert _finished(client, response.get_json()["id"])["status"] == "completed"
    folders = sorted(p.name for p in (tmp_path / "jobs").iterdir() if p.is_dir())
    assert folders == ["doppelt", "doppelt-2"]


@pytest.mark.parametrize("title", ["../../escape", "/etc/passwd", "..", "   ", "", None, 5])
def test_a_hostile_or_empty_album_title_cannot_choose_the_folder(client, monkeypatch, tmp_path, title):
    monkeypatch.setattr(web, "run_album", lambda url, output, **kw: _titled_report(output, title))
    headers = _headers(client)
    response = client.post("/api/jobs", json={"url": "https://immich.example/share/book"}, headers=headers)
    job_id = response.get_json()["id"]
    assert _finished(client, job_id)["status"] == "completed"
    root = tmp_path / "jobs"
    made = [p for p in root.iterdir() if p.is_dir()]
    assert len(made) == 1
    assert made[0].parent == root, "the job folder escaped the output root"
    assert made[0].name != ".." and "/" not in made[0].name


def test_a_failed_job_keeps_its_folder_for_retry(client, monkeypatch, tmp_path):
    def explode(url, output, **kwargs):
        (output / "originals").mkdir(exist_ok=True)
        raise RuntimeError("Immich went away")

    monkeypatch.setattr(web, "run_album", explode)
    headers = _headers(client)
    response = client.post("/api/jobs", json={"url": "https://immich.example/share/book"}, headers=headers)
    job_id = response.get_json()["id"]
    assert _finished(client, job_id)["status"] == "failed"
    # No title is known, so the download directory must stay where the retry expects it.
    assert (tmp_path / "jobs" / job_id / "originals").is_dir()
