"""Download complete Immich albums without persisting authentication secrets.

Supports the asset arrays used by Immich 1/2, paginated metadata search used
by Immich 3, and the timeline fallback needed by early Immich 3 shared links.
Completed originals are verified and reused when a run is resumed; an interrupted
individual image is downloaded again. No thumbnails are substituted for scans.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urlsplit, urlunsplit
from uuid import UUID

import requests


class ImmichError(RuntimeError):
    """A user-facing error that does not contain request URLs or credentials."""


class _HTTPError(ImmichError):
    def __init__(self, status: int):
        self.status = status
        if status in (401, 403):
            advice = "Check the API key/share password and album/download permissions."
        elif status == 404:
            advice = "Check the album link, server address, and whether the share expired."
        elif 300 <= status < 400:
            advice = "Use the final server URL; authenticated redirects are not followed."
        else:
            advice = "Check that the Immich server is available."
        super().__init__(f"Immich returned HTTP {status}. {advice}")


def _request_error_message(error: requests.RequestException) -> str:
    """Describe transport failures without including URLs or credentials."""
    # Specific connection/timeout subclasses must precede their base classes.
    reasons = (
        (requests.exceptions.SSLError,
         "The TLS/SSL connection failed. Check http versus https and the server certificate."),
        (requests.exceptions.ProxyError,
         "The proxy connection failed. Check the proxy settings and whether the proxy is reachable."),
        (requests.exceptions.ConnectTimeout,
         "The connection timed out. Check the server address and whether Immich is reachable."),
        (requests.exceptions.Timeout,
         "The request timed out while waiting for Immich. Check the server and network connection."),
        (requests.exceptions.InvalidHeader,
         "The request headers are invalid. Check the API key for unintended spaces or newlines."),
        ((requests.exceptions.InvalidURL, requests.exceptions.MissingSchema, requests.exceptions.InvalidSchema),
         "The server address is invalid. Check the Immich URL and its http or https scheme."),
        (requests.exceptions.ConnectionError,
         "The network connection failed. Check that Immich is running and that the server address, "
         "VPN, firewall, and local network permissions allow access."),
    )
    for kind, reason in reasons:
        if isinstance(error, kind):
            return f"Unable to contact Immich after three attempts. {reason}"
    return "Unable to contact Immich after three attempts; check the connection."


@dataclass(frozen=True)
class DownloadedAsset:
    id: str
    filename: str
    path: Path
    order: int


@dataclass
class DownloadResult:
    assets: list[DownloadedAsset]
    album_name: str
    skipped_videos: int
    warnings: list[str]


def _uuid(value: Any) -> str:
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        raise ImmichError("Immich supplied an invalid album or asset ID.") from None


def _parse_url(url: str) -> tuple[str, str, str]:
    try:
        parsed = urlsplit(url.strip())
        valid = parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username and not parsed.password
        # Accessing port also validates it, without exposing the malformed URL.
        parsed.port
    except ValueError:
        valid = False
    if not valid:
        raise ImmichError("Enter an http(s) Immich album/share URL without embedded credentials.")
    match = re.fullmatch(r"(.*)/(share|albums|s)/([^/]+)/?", parsed.path)
    if not match:
        raise ImmichError("Expected an Immich link ending in /share/<key>, /s/<slug>, or /albums/<album-id>.")
    prefix, kind, identifier = match.groups()
    # Immich >= 1.136 shortens shared links to /s/<slug>; the slug is not a share key.
    kind = "slug" if kind == "s" else kind
    identifier = unquote(identifier)
    if kind == "albums":
        identifier = _uuid(identifier)
    elif not identifier or any(ord(c) < 33 or c in "/\\" for c in identifier):
        raise ImmichError("The shared album link contains an invalid share key.")
    prefix = prefix.rstrip("/")
    api_path = prefix if prefix.endswith("/api") else prefix + "/api"
    return urlunsplit((parsed.scheme, parsed.netloc, api_path, "", "")), kind, identifier


class _Client:
    def __init__(self, base: str, *, key: str | None = None, slug: str | None = None, api_key: str | None = None):
        self.base = base
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "immich-book-ocr/1", "Accept": "application/json"})
        self.params: dict[str, str] = {}
        if key:
            self.params["key"] = key
        elif slug:
            self.params["slug"] = slug
        elif api_key:
            self.session.headers["x-api-key"] = api_key

    def request(self, method: str, route: str, **kwargs: Any) -> requests.Response:
        params = {**self.params, **kwargs.pop("params", {})}
        for attempt in range(3):
            try:
                response = self.session.request(
                    method, self.base + route, params=params, timeout=(15, 120),
                    allow_redirects=False, **kwargs,
                )
            except requests.RequestException as error:
                if attempt == 2:
                    raise ImmichError(_request_error_message(error)) from None
            else:
                if 200 <= response.status_code < 300:
                    return response
                status = response.status_code
                response.close()
                if status not in {408, 429, 500, 502, 503, 504} or attempt == 2:
                    raise _HTTPError(status)
            time.sleep(0.5 * 2**attempt)
        raise AssertionError("unreachable")

    def json(self, method: str, route: str, **kwargs: Any) -> Any:
        response = self.request(method, route, **kwargs)
        try:
            return response.json()
        except ValueError:
            raise ImmichError("Immich returned invalid JSON; check that the URL points to an Immich server.") from None
        finally:
            response.close()

    def shared_link(self, password: str | None) -> dict[str, Any]:
        try:
            shared = self.json("GET", "/shared-links/me")
        except _HTTPError as error:
            if error.status not in {401, 403} or password is None:
                raise
            try:
                # Immich >= 2.6 sets a shared-link authentication cookie here.
                shared = self.json("POST", "/shared-links/login", json={"password": password})
            except _HTTPError as login_error:
                if login_error.status not in {404, 405}:
                    raise
                # Older servers require this query parameter on subsequent calls.
                self.params["password"] = password
                shared = self.json("GET", "/shared-links/me")
        if not isinstance(shared, dict):
            raise ImmichError("Immich returned an invalid shared-link response.")
        if shared.get("allowDownload") is False:
            raise ImmichError("Original downloads are disabled for this shared link. Enable downloads in Immich first.")
        # A /s/<slug> link authenticates by slug; swap in the share key it returns so
        # every later asset, album, and timeline call uses the long-standing key path.
        if "slug" in self.params:
            key = shared.get("key")
            if isinstance(key, str) and key and not any(ord(c) < 33 or c in "/\\" for c in key):
                self.params.pop("slug")
                self.params["key"] = key
        return shared

    def search_assets(self, album_id: str, direction: str) -> list[dict[str, Any]]:
        assets: list[dict[str, Any]] = []
        seen: set[str] = set()
        page = 1
        while True:
            data = self.json("POST", "/search/metadata", json={
                "albumIds": [album_id], "size": 1000, "page": page,
                # Unlike timeline's flag, search withStacked=False EXCLUDES
                # every stacked image. True keeps every original scan.
                "order": direction, "withStacked": True,
            })
            block = data.get("assets") if isinstance(data, dict) else None
            if not isinstance(block, dict) or not isinstance(block.get("items"), list):
                raise ImmichError("Immich returned an invalid album search response.")
            items = block["items"]
            for item in items:
                if not isinstance(item, dict):
                    raise ImmichError("Immich returned invalid asset metadata.")
                asset_id = _uuid(item.get("id"))
                if asset_id in seen:
                    raise ImmichError("Album pagination repeated an asset; the album may have changed. Run again.")
                seen.add(asset_id)
                assets.append(item)
            next_page = block.get("nextPage")
            if next_page is None:
                if "nextPage" not in block and len(items) >= 1000:
                    raise ImmichError("Immich omitted pagination information; cannot verify that every scan was listed.")
                return assets
            try:
                next_number = int(next_page)
            except (TypeError, ValueError):
                raise ImmichError("Immich returned an invalid pagination cursor.") from None
            if next_number <= page or not items:
                raise ImmichError("Immich pagination did not advance; cannot safely list every scan.")
            page = next_number

    def timeline_assets(self, album_id: str, direction: str) -> list[dict[str, Any]]:
        params = {"albumId": album_id, "order": direction, "withStacked": "false"}
        buckets = self.json("GET", "/timeline/buckets", params=params)
        if not isinstance(buckets, list):
            raise ImmichError("Immich returned an invalid timeline bucket list.")
        assets: list[dict[str, Any]] = []
        seen: set[str] = set()
        for bucket in buckets:
            if not isinstance(bucket, dict) or not bucket.get("timeBucket"):
                raise ImmichError("Immich returned an invalid timeline bucket.")
            data = self.json("GET", "/timeline/bucket", params={**params, "timeBucket": bucket["timeBucket"]})
            # Some versions serialize the column-oriented response into a JSON string.
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except ValueError:
                    raise ImmichError("Immich returned invalid timeline JSON.") from None
            ids = data.get("id") if isinstance(data, dict) else None
            if ids is None and isinstance(data, list):
                ids = [item.get("id") for item in data if isinstance(item, dict)]
            if not isinstance(ids, list) or ("count" in bucket and len(ids) != bucket["count"]):
                raise ImmichError("Immich timeline counts do not match; cannot verify every scan was listed.")
            for value in ids:
                asset_id = _uuid(value)
                if asset_id in seen:
                    raise ImmichError("Immich timeline repeated an asset; the album may have changed. Run again.")
                seen.add(asset_id)
                asset = self.json("GET", f"/assets/{asset_id}")
                if not isinstance(asset, dict) or _uuid(asset.get("id")) != asset_id:
                    raise ImmichError("Immich returned invalid asset metadata.")
                assets.append(asset)
        return assets


def _safe_name(value: str) -> str:
    value = value.replace("\\", "/").rsplit("/", 1)[-1]
    value = re.sub(r"[^\w. -]", "_", value, flags=re.UNICODE).strip(" .")
    value = value[-150:]
    while len(value.encode("utf-8")) > 150:
        value = value[1:]
    return value or "scan"


def _natural(value: str) -> tuple[Any, ...]:
    return tuple((1, int(part)) if part.isdecimal() else (0, part.casefold()) for part in re.split(r"(\d+)", value))


def _date(asset: dict[str, Any]) -> float | None:
    for field in ("fileCreatedAt", "localDateTime"):
        try:
            value = datetime.fromisoformat(str(asset.get(field)).replace("Z", "+00:00"))
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return value.timestamp()
        except (ValueError, TypeError, OverflowError):
            continue
    return None


def _digest(path: Path, algorithm: str = "sha256") -> str:
    digest = hashlib.new(algorithm)
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _checksum(asset: dict[str, Any]) -> str | None:
    value = asset.get("checksum")
    if not isinstance(value, str) or not value:
        return None
    if re.fullmatch(r"[a-fA-F0-9]{40}", value):
        return value.lower()
    try:
        decoded = base64.b64decode(value, validate=True)
        return decoded.hex() if len(decoded) == 20 else None
    except ValueError:
        return None


def _download(client: _Client, asset: dict[str, Any], path: Path) -> bool:
    """Return True when a previously completed image was verified and reused."""
    marker = path.with_name(path.name + ".download.json")
    partial = path.with_name(path.name + ".part")
    marker_partial = marker.with_name(marker.name + ".part")
    for target in (path, marker, partial, marker_partial):
        if target.is_symlink():
            raise ImmichError("A download destination is a symbolic link; choose a fresh output directory.")
    identity = hashlib.sha256((client.base + "/" + asset["id"]).encode()).hexdigest()
    checksum = _checksum(asset)
    try:
        saved = json.loads(marker.read_text(encoding="utf-8"))
        if (isinstance(saved, dict) and saved.get("source") == identity and saved.get("checksum") == checksum
                and path.is_file() and path.stat().st_size > 0 and path.stat().st_size == saved.get("size")
                and _digest(path) == saved.get("sha256")):
            return True
    except (OSError, ValueError):
        pass
    for attempt in range(3):
        response = client.request("GET", f"/assets/{_uuid(asset['id'])}/original", stream=True,
                                  headers={"Accept": "application/octet-stream", "Accept-Encoding": "identity"})
        try:
            if response.status_code != 200:
                raise ImmichError("Immich returned only a partial original image; download stopped.")
            content_type = response.headers.get("Content-Type", "").lower()
            if any(value in content_type for value in ("text/html", "application/json", "text/plain")):
                raise ImmichError("Immich returned a document instead of an original image.")
            size = 0
            with partial.open("wb") as output:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        output.write(chunk)
                        size += len(chunk)
            expected = response.headers.get("Content-Length")
            if not size or (expected is not None and size != int(expected)):
                raise requests.ConnectionError("Incomplete original")
            if checksum and _digest(partial, "sha1") != checksum:
                raise requests.ConnectionError("Original checksum mismatch")
            metadata = {"source": identity, "size": size, "sha256": _digest(partial), "checksum": checksum}
            partial.replace(path)
            marker_partial.write_text(json.dumps(metadata), encoding="utf-8")
            marker_partial.replace(marker)
            return False
        except (requests.RequestException, ValueError):
            if attempt == 2:
                raise ImmichError("An original image failed integrity/download checks after three attempts. Run again to resume.") from None
        finally:
            response.close()
        time.sleep(0.5 * 2**attempt)
    raise AssertionError("unreachable")


def download_album(
    url: str, destination: Path, *, api_key: str | None = None, password: str | None = None,
    order: str = "date", progress: Callable[[str], None] | None = None,
) -> DownloadResult:
    """Download all originals, or raise instead of producing an incomplete album.

    ``date`` defaults to oldest capture first and requires capture timestamps;
    ``album`` follows the album's ascending/descending capture-date preference;
    ``filename`` is natural ascending filename order.
    ``DownloadedAsset.order`` is zero-based. Videos are counted and excluded.
    """
    if order not in {"album", "filename", "date"}:
        raise ImmichError("Order must be album, filename, or date.")
    base, kind, identifier = _parse_url(url)
    if kind == "albums" and not api_key:
        raise ImmichError("A private /albums/ link needs an Immich API key with album.read, asset.read, and asset.download permissions.")
    emit = progress or (lambda _: None)
    client = _Client(
        base,
        key=identifier if kind == "share" else None,
        slug=identifier if kind == "slug" else None,
        api_key=api_key,
    )
    warnings: list[str] = []
    try:
        emit("Reading Immich album metadata…")
        shared = client.shared_link(password) if kind != "albums" else None
        album = shared.get("album") if shared else None
        if kind == "albums":
            album_id = identifier
        elif isinstance(album, dict) and album.get("id"):
            album_id = _uuid(album["id"])
        elif shared and shared.get("type") == "ALBUM":
            raise ImmichError("The shared album metadata is missing; verify that the album still exists.")
        else:
            album_id = None
        if album_id:
            album = client.json("GET", f"/albums/{album_id}")
            if not isinstance(album, dict):
                raise ImmichError("Immich returned invalid album metadata.")
        album = album or {}
        album_name = str(album.get("albumName") or "Shared scans")
        direction = album.get("order", "asc") if order == "album" else "asc"
        if direction not in {"asc", "desc"}:
            raise ImmichError("Immich returned an unsupported album display order; select filename or date order.")
        expected = album.get("assetCount")
        assets = album.get("assets") if album_id else shared.get("assets") if shared else None
        if album_id and (not isinstance(assets, list) or (isinstance(expected, int) and len(assets) != expected)):
            emit("Listing all album pages…")
            try:
                assets = client.search_assets(album_id, direction)
            except _HTTPError as error:
                if error.status not in {400, 403, 404, 405, 422}:
                    raise
                emit("Using the Immich timeline to list all scans…")
                assets = client.timeline_assets(album_id, direction)
        if not isinstance(assets, list):
            raise ImmichError("Immich did not return an asset list.")
        if isinstance(expected, int) and len(assets) != expected:
            raise ImmichError(f"Album lists {expected} assets but {len(assets)} were found. The album may have changed; run again.")
        seen: set[str] = set()
        images: list[dict[str, Any]] = []
        skipped_videos = 0
        for item in assets:
            if not isinstance(item, dict):
                raise ImmichError("Immich returned invalid asset metadata.")
            asset_id = _uuid(item.get("id"))
            if asset_id in seen:
                raise ImmichError("Immich returned duplicate asset IDs; cannot confirm a complete album.")
            seen.add(asset_id)
            if str(item.get("type", "")).upper() == "VIDEO":
                skipped_videos += 1
            elif str(item.get("type", "")).upper() == "IMAGE":
                images.append({**item, "id": asset_id})
            else:
                raise ImmichError("The album contains an unknown asset type; no assets were silently skipped.")
        if order == "filename":
            if any(not item.get("originalFileName") for item in images):
                raise ImmichError("Some original filenames are hidden; enable shared metadata or choose another order.")
            images.sort(key=lambda item: (_natural(item["originalFileName"]), item["id"]))
        elif all(_date(item) is not None for item in images):
            # Legacy album arrays do not necessarily honor the album's order field.
            # Timeline uses the original filename to break equal timestamp ties.
            images.sort(key=lambda item: (_date(item), str(item.get("originalFileName", "")), item["id"]),
                        reverse=direction == "desc")
        elif order == "date":
            raise ImmichError("Some capture dates are missing; choose album or filename order.")
        else:
            warnings.append("Some capture dates are unavailable; retained the server asset order. Verify scan order before OCR.")
        if skipped_videos:
            warnings.append(f"Excluded {skipped_videos} video asset(s).")
        destination = Path(destination)
        destination.mkdir(parents=True, exist_ok=True)
        downloaded: list[DownloadedAsset] = []
        filenames = [_safe_name(str(asset.get("originalFileName") or asset["id"])) for asset in images]
        name_counts = Counter(name.casefold() for name in filenames)
        reserved_names = set(name_counts)
        for index, asset in enumerate(images):
            filename = filenames[index]
            path = destination / f"{asset['id']}_{filename}"
            if name_counts[filename.casefold()] > 1:
                filename = path.name
                while filename.casefold() in reserved_names:
                    filename = "_" + filename
                reserved_names.add(filename.casefold())
            reused = _download(client, asset, path)
            downloaded.append(DownloadedAsset(asset["id"], filename, path, index))
            emit(f"{'Verified cached' if reused else 'Downloaded'} image {index + 1}/{len(images)}")
        return DownloadResult(downloaded, album_name, skipped_videos, warnings)
    finally:
        client.session.close()
