"""No live Immich server, credentials, or network are used in these tests."""

import base64
import hashlib
import json
from pathlib import Path
from unittest.mock import Mock

import pytest
import requests

from immich_book_ocr import immich


ALBUM = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
ONE = "11111111-1111-4111-8111-111111111111"
TWO = "22222222-2222-4222-8222-222222222222"
THREE = "33333333-3333-4333-8333-333333333333"
URL = f"https://photos.example/albums/{ALBUM}"
CONTENT = b"original scan bytes"


def asset(identifier=ONE, name="scan1.jpg", day=1, kind="IMAGE"):
    return {"id": identifier, "originalFileName": name, "type": kind,
            "fileCreatedAt": f"2026-01-{day:02}T12:00:00Z"}


def response(data=None, *, status=200, content=None, headers=None):
    result = Mock(status_code=status)
    result.headers = headers or ({"Content-Length": str(len(content))} if content else {})
    result.json.return_value = data
    result.iter_content.return_value = iter([content or CONTENT])
    return result


@pytest.fixture
def session(monkeypatch):
    value = Mock()
    value.headers = {}
    monkeypatch.setattr(immich.requests, "Session", lambda: value)
    monkeypatch.setattr(immich.time, "sleep", lambda _: None)
    return value


def legacy(items, direction="asc"):
    return response({"albumName": "Book", "assetCount": len(items), "order": direction, "assets": items})


def test_private_album_downloads_originals_in_album_direction_and_counts_videos(session, tmp_path):
    items = [asset(), asset(TWO, day=2), asset(THREE, kind="VIDEO")]
    session.request.side_effect = [legacy(items, "desc"), response(content=CONTENT), response(content=CONTENT)]
    result = immich.download_album(URL, tmp_path, api_key="secret", order="album")
    assert [item.id for item in result.assets] == [TWO, ONE]
    assert [item.order for item in result.assets] == [0, 1]
    assert result.skipped_videos == 1
    assert result.warnings == ["Excluded 1 video asset(s)."]
    assert all(item.path.read_bytes() == CONTENT for item in result.assets)
    assert session.headers["x-api-key"] == "secret"
    assert session.request.call_args_list[-1].args[1].endswith(f"/assets/{ONE}/original")
    assert session.request.call_args_list[-1].kwargs["allow_redirects"] is False


def test_filename_order_is_natural_not_lexicographic(session, tmp_path):
    session.request.side_effect = [legacy([asset(TWO, "scan10.jpg"), asset(ONE, "scan2.jpg")]),
                                   response(content=CONTENT), response(content=CONTENT)]
    result = immich.download_album(URL, tmp_path, api_key="secret", order="filename")
    assert [item.filename for item in result.assets] == ["scan2.jpg", "scan10.jpg"]


@pytest.mark.parametrize("ordering", [{}, {"order": "date"}], ids=["default", "explicit-date"])
def test_capture_order_overrides_reverse_filenames_and_descending_album(session, tmp_path, ordering):
    session.request.side_effect = [legacy([asset(TWO, "scan2.jpg", day=2), asset(ONE, "scan10.jpg")], "desc"),
                                   response(content=CONTENT), response(content=CONTENT)]
    result = immich.download_album(URL, tmp_path, api_key="secret", **ordering)
    assert [item.id for item in result.assets] == [ONE, TWO]
    assert [item.filename for item in result.assets] == ["scan10.jpg", "scan2.jpg"]
    assert [item.order for item in result.assets] == [0, 1]


def test_default_capture_order_compares_timezone_offsets(session, tmp_path):
    earlier = {**asset(ONE, "z.jpg"), "fileCreatedAt": "2026-01-02T01:00:00+02:00"}
    later = {**asset(TWO, "a.jpg"), "fileCreatedAt": "2026-01-01T23:30:00Z"}
    session.request.side_effect = [legacy([later, earlier], "desc"),
                                   response(content=CONTENT), response(content=CONTENT)]
    result = immich.download_album(URL, tmp_path, api_key="secret")
    assert [item.id for item in result.assets] == [ONE, TWO]


@pytest.mark.parametrize("primary_date", [None, "", "not-a-date"])
def test_capture_order_uses_local_date_when_primary_is_missing_or_invalid(session, tmp_path, primary_date):
    earlier = {**asset(ONE), "fileCreatedAt": primary_date, "localDateTime": "2026-01-01T09:00:00"}
    session.request.side_effect = [legacy([asset(TWO, day=2), earlier]),
                                   response(content=CONTENT), response(content=CONTENT)]
    result = immich.download_album(URL, tmp_path, api_key="secret")
    assert [item.id for item in result.assets] == [ONE, TWO]


@pytest.mark.parametrize("capture_fields", [{}, {"fileCreatedAt": "invalid"}, {"localDateTime": "invalid"}])
def test_default_capture_order_rejects_missing_dates_despite_filenames_and_upload_dates(session, tmp_path, capture_fields):
    undated = asset(ONE, "scan1.jpg")
    undated.pop("fileCreatedAt")
    undated.update(capture_fields)
    undated.update({"createdAt": "2026-01-01T12:00:00Z", "updatedAt": "2026-01-02T12:00:00Z"})
    session.request.side_effect = [legacy([undated, asset(TWO, "scan2.jpg", day=2)])]
    with pytest.raises(immich.ImmichError, match="capture dates are missing"):
        immich.download_album(URL, tmp_path, api_key="secret")
    assert session.request.call_count == 1
    assert not list(tmp_path.iterdir())


def test_album_breaks_timestamp_ties_by_filename_as_timeline_does(session, tmp_path):
    session.request.side_effect = [legacy([asset(ONE, "b.jpg"), asset(TWO, "a.jpg")]),
                                   response(content=CONTENT), response(content=CONTENT)]
    result = immich.download_album(URL, tmp_path, api_key="secret", order="album")
    assert [item.id for item in result.assets] == [TWO, ONE]


def test_v3_album_follows_all_search_pages(session, tmp_path):
    session.request.side_effect = [
        response({"albumName": "Book", "assetCount": 2, "order": "asc"}),
        response({"assets": {"items": [asset()], "nextPage": "2"}}),
        response({"assets": {"items": [asset(TWO, day=2)], "nextPage": None}}),
        response(content=CONTENT), response(content=CONTENT),
    ]
    result = immich.download_album(URL, tmp_path, api_key="secret")
    assert len(result.assets) == 2
    pages = [call.kwargs["json"]["page"] for call in session.request.call_args_list if call.args[0] == "POST"]
    assert pages == [1, 2]
    assert session.request.call_args_list[1].kwargs["json"]["albumIds"] == [ALBUM]
    assert session.request.call_args_list[1].kwargs["json"]["withStacked"] is True
    assert "withExif" not in session.request.call_args_list[1].kwargs["json"]


def test_album_count_mismatch_fails_before_any_download(session, tmp_path):
    session.request.side_effect = [response({"assetCount": 3}),
                                   response({"assets": {"items": [asset()], "nextPage": None}})]
    with pytest.raises(immich.ImmichError, match="3 assets but 1"):
        immich.download_album(URL, tmp_path, api_key="secret")
    assert session.request.call_count == 2
    assert not list(tmp_path.iterdir())


def test_repeated_search_page_does_not_loop_or_silently_dedupe(session, tmp_path):
    session.request.side_effect = [response({"assetCount": 2}),
                                   response({"assets": {"items": [asset()], "nextPage": "2"}}),
                                   response({"assets": {"items": [asset()], "nextPage": "3"}})]
    with pytest.raises(immich.ImmichError, match="repeated an asset"):
        immich.download_album(URL, tmp_path, api_key="secret")


def test_modern_password_login_uses_session_and_preserves_base_path(session, tmp_path):
    session.request.side_effect = [response(status=401),
                                   response({"album": {"id": ALBUM}, "allowDownload": True}),
                                   legacy([asset()]), response(content=CONTENT)]
    result = immich.download_album("https://photos.example/immich/api/share/hidden-key", tmp_path,
                                   password="hidden-password", api_key="unnecessary-api-key")
    assert len(result.assets) == 1
    calls = session.request.call_args_list
    assert calls[1].args == ("POST", "https://photos.example/immich/api/shared-links/login")
    assert calls[1].kwargs["json"] == {"password": "hidden-password"}
    assert all(call.kwargs["params"]["key"] == "hidden-key" for call in calls)
    assert all("password" not in call.kwargs["params"] for call in calls)
    assert "x-api-key" not in session.headers
    stored = "".join(path.read_text() for path in tmp_path.glob("*.json"))
    assert "hidden-key" not in stored and "hidden-password" not in stored


def test_older_password_share_uses_query_fallback(session, tmp_path):
    session.request.side_effect = [response(status=401), response(status=404),
                                   response({"album": {"id": ALBUM}, "allowDownload": True}),
                                   legacy([asset()]), response(content=CONTENT)]
    immich.download_album("https://photos.example/share/key", tmp_path, password="password")
    assert session.request.call_args_list[2].kwargs["params"] == {"key": "key", "password": "password"}
    assert session.request.call_args_list[-1].kwargs["params"]["password"] == "password"


def test_short_slug_share_link_authenticates_by_slug_then_uses_returned_key(session, tmp_path):
    session.request.side_effect = [
        response({"album": {"id": ALBUM}, "allowDownload": True, "key": "real-key"}),
        legacy([asset()]), response(content=CONTENT),
    ]
    result = immich.download_album("http://photos.example:30041/s/test", tmp_path)
    assert len(result.assets) == 1
    calls = session.request.call_args_list
    assert calls[0].args == ("GET", "http://photos.example:30041/api/shared-links/me")
    assert calls[0].kwargs["params"] == {"slug": "test"}
    assert all(call.kwargs["params"] == {"key": "real-key"} for call in calls[1:])


def test_short_slug_share_link_keeps_slug_when_no_key_is_returned(session, tmp_path):
    session.request.side_effect = [
        response({"album": {"id": ALBUM}, "allowDownload": True}),
        legacy([asset()]), response(content=CONTENT),
    ]
    immich.download_album("https://photos.example/immich/s/book-scans", tmp_path)
    calls = session.request.call_args_list
    assert calls[0].args == ("GET", "https://photos.example/immich/api/shared-links/me")
    assert all(call.kwargs["params"] == {"slug": "book-scans"} for call in calls)


def test_disabled_originals_fail_instead_of_downloading_thumbnails(session, tmp_path):
    session.request.return_value = response({"allowDownload": False})
    with pytest.raises(immich.ImmichError, match="Original downloads are disabled"):
        immich.download_album("https://photos.example/share/key", tmp_path)
    assert session.request.call_count == 1


def test_early_v3_shared_album_falls_back_to_complete_timeline(session, tmp_path):
    session.request.side_effect = [
        response({"album": {"id": ALBUM}, "allowDownload": True}), response({"assetCount": 2}),
        response(status=403), response([{"timeBucket": "2026-01-01", "count": 1}, {"timeBucket": "2026-01-02", "count": 1}]),
        response({"id": [ONE]}), response(asset()), response(json.dumps({"id": [TWO]})), response(asset(TWO, day=2)),
        response(content=CONTENT), response(content=CONTENT),
    ]
    result = immich.download_album("https://photos.example/share/key", tmp_path)
    assert [item.id for item in result.assets] == [ONE, TWO]
    bucket_calls = [call for call in session.request.call_args_list if call.args[1].endswith("/timeline/bucket")]
    assert len(bucket_calls) == 2
    assert all(call.kwargs["params"]["withStacked"] == "false" for call in bucket_calls)


def test_completed_downloads_are_verified_before_resume(session, tmp_path):
    session.request.side_effect = [legacy([asset()]), response(content=CONTENT), legacy([asset()])]
    first = immich.download_album(URL, tmp_path, api_key="secret")
    second = immich.download_album(URL, tmp_path, api_key="secret")
    assert second.assets == first.assets
    assert session.request.call_count == 3


def test_corrupted_completed_file_is_downloaded_again(session, tmp_path):
    session.request.side_effect = [legacy([asset()]), response(content=CONTENT)]
    result = immich.download_album(URL, tmp_path, api_key="secret")
    result.assets[0].path.write_bytes(b"x" * len(CONTENT))
    session.request.side_effect = [legacy([asset()]), response(content=CONTENT)]
    result = immich.download_album(URL, tmp_path, api_key="secret")
    assert result.assets[0].path.read_bytes() == CONTENT


def test_interrupted_download_restarts_current_image_and_uses_atomic_completion(session, tmp_path):
    broken = response(content=CONTENT)

    def interrupt():
        yield b"partial"
        raise requests.ConnectionError("https://photos.example?key=secret")

    broken.iter_content.return_value = interrupt()
    session.request.side_effect = [legacy([asset()]), broken, response(content=CONTENT)]
    result = immich.download_album(URL, tmp_path, api_key="secret")
    assert result.assets[0].path.read_bytes() == CONTENT
    assert not list(tmp_path.glob("*.part"))


def test_sha1_checksum_is_verified_not_just_content_length(session, tmp_path):
    metadata = asset()
    metadata["checksum"] = base64.b64encode(hashlib.sha1(CONTENT).digest()).decode()
    session.request.side_effect = [legacy([metadata]), response(content=b"wrong"), response(content=CONTENT)]
    result = immich.download_album(URL, tmp_path, api_key="secret")
    assert result.assets[0].path.read_bytes() == CONTENT
    assert session.request.call_count == 3


def test_repeated_truncation_never_creates_completed_file(session, tmp_path):
    session.request.side_effect = [legacy([asset()])] + [response(content=b"bad", headers={"Content-Length": "20"}) for _ in range(3)]
    with pytest.raises(immich.ImmichError, match="integrity/download checks"):
        immich.download_album(URL, tmp_path, api_key="secret")
    assert not list(tmp_path.glob("*.jpg"))
    assert not list(tmp_path.glob("*.json"))


@pytest.mark.parametrize(("error_type", "explanation"), [
    (requests.exceptions.SSLError, "TLS/SSL connection failed"),
    (requests.exceptions.ProxyError, "proxy connection failed"),
    (requests.exceptions.ConnectTimeout, "connection timed out"),
    (requests.exceptions.ReadTimeout, "timed out while waiting for Immich"),
    (requests.exceptions.Timeout, "timed out while waiting for Immich"),
    (requests.exceptions.InvalidHeader, "API key for unintended spaces or newlines"),
    (requests.exceptions.InvalidURL, "server address is invalid"),
    (requests.exceptions.MissingSchema, "server address is invalid"),
    (requests.exceptions.InvalidSchema, "server address is invalid"),
    (requests.exceptions.ConnectionError, "network connection failed"),
    (requests.exceptions.RequestException, "check the connection"),
])
def test_request_failures_explain_cause_without_echoing_credentials(session, tmp_path, error_type, explanation):
    session.request.side_effect = error_type("https://photos.example/share/SECRET?password=PASSWORD")
    progress = []
    with pytest.raises(immich.ImmichError) as failure:
        immich.download_album("https://photos.example/share/SECRET", tmp_path, password="PASSWORD", progress=progress.append)
    report = str(failure.value) + " ".join(progress)
    assert explanation in str(failure.value)
    assert "three attempts" in str(failure.value)
    assert "SECRET" not in report and "PASSWORD" not in report and "photos.example" not in report
    assert failure.value.__suppress_context__
    assert session.request.call_count == 3


def test_server_filenames_cannot_escape_destination(session, tmp_path):
    session.request.side_effect = [legacy([asset(name="../../outside.jpg")]), response(content=CONTENT)]
    result = immich.download_album(URL, tmp_path, api_key="secret")
    assert result.assets[0].path.parent == tmp_path
    assert result.assets[0].filename == "outside.jpg"


def test_duplicate_filenames_have_unique_stable_override_keys(session, tmp_path):
    session.request.side_effect = [legacy([asset(), asset(TWO)]), response(content=CONTENT), response(content=CONTENT)]
    result = immich.download_album(URL, tmp_path, api_key="secret")
    assert len({item.filename for item in result.assets}) == 2
    assert [item.filename for item in result.assets] == [f"{ONE}_scan1.jpg", f"{TWO}_scan1.jpg"]


def test_existing_symlink_is_not_followed(session, tmp_path):
    target = tmp_path / "keep.txt"
    target.write_text("keep")
    (tmp_path / f"{ONE}_scan1.jpg.part").symlink_to(target)
    session.request.side_effect = [legacy([asset()])]
    with pytest.raises(immich.ImmichError, match="symbolic link"):
        immich.download_album(URL, tmp_path, api_key="secret")
    assert target.read_text() == "keep"


@pytest.mark.parametrize("url", ["file:///etc/passwd", "https://user:secret@photos.example/share/key", "https://photos.example/library", "https://photos.example:bad/share/key", "https://photos.example/s/", "https://photos.example/s/a/b"])
def test_invalid_urls_fail_without_network_or_echoing_input(session, tmp_path, url):
    with pytest.raises(immich.ImmichError) as failure:
        immich.download_album(url, tmp_path)
    assert url not in str(failure.value)
    session.request.assert_not_called()


def test_private_album_requires_explicit_api_key(session, tmp_path):
    with pytest.raises(immich.ImmichError, match="needs an Immich API key"):
        immich.download_album(URL, tmp_path)
    session.request.assert_not_called()


def test_redirect_is_not_followed_with_credentials(session, tmp_path):
    session.request.return_value = response(status=302)
    with pytest.raises(immich.ImmichError, match="redirects are not followed"):
        immich.download_album(URL, tmp_path, api_key="secret")
    assert session.request.call_count == 1


def test_unknown_types_are_not_silently_skipped(session, tmp_path):
    session.request.return_value = legacy([asset(kind="OTHER")])
    with pytest.raises(immich.ImmichError, match="unknown asset type"):
        immich.download_album(URL, tmp_path, api_key="secret")
