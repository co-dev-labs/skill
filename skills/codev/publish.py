#!/usr/bin/env python3
"""Publish a folder of static files to Codev and print its URL.

Standard library only, Python 3.10 or newer, no installation needed.

    python3 publish.py ./dist --spa
    python3 publish.py ./dist --site <site id> --base-version <version id>
    python3 publish.py ./dist --pair

Environment:
    CODEV_API_KEY   an API key (ck_live_...); without one the site is
                    anonymous, expires in 24 hours and prints a claim URL
    CODEV_API_URL   the API origin (default: https://api.co.dev)

Exit codes: 0 ok, 1 unexpected error, 2 usage or folder problem, 3 not
authorized, 4 version conflict (prints current_version_id), 5 upload
failed, 6 finalize failed, 7 pairing expired or denied.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath

DEFAULT_API_URL = "https://api.co.dev"
FALLBACK_API_URL = "http://localhost:8004"
RESERVED_PREFIX = ".codev"
SKIP_FILES = {".DS_Store", "Thumbs.db", "desktop.ini"}
SKIP_DIRS = {".git", "__MACOSX"}
UPLOAD_ATTEMPTS = 3
REQUEST_TIMEOUT = 60
UPLOAD_TIMEOUT = 600

EXIT_OK = 0
EXIT_UNEXPECTED = 1
EXIT_USAGE = 2
EXIT_AUTH = 3
EXIT_CONFLICT = 4
EXIT_UPLOAD = 5
EXIT_FINALIZE = 6
EXIT_PAIRING = 7

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".json": "application/json",
    ".map": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".avif": "image/avif",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".xml": "application/xml",
    ".webmanifest": "application/manifest+json",
    ".wasm": "application/wasm",
    ".pdf": "application/pdf",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mp3": "audio/mpeg",
}


class PublishError(Exception):
    def __init__(self, message: str, exit_code: int, detail: dict | None = None):
        super().__init__(message)
        self.exit_code = exit_code
        self.detail = detail or {}


class ApiError(PublishError):
    """A non-2xx answer from the API, with its structured detail if any."""

    def __init__(self, status: int, detail):
        self.status = status
        structured = detail if isinstance(detail, dict) else {}
        message = structured.get("message") or (
            detail if isinstance(detail, str) else json.dumps(detail)
        )
        exit_code = {401: EXIT_AUTH, 403: EXIT_AUTH, 409: EXIT_CONFLICT}.get(
            status, EXIT_UNEXPECTED
        )
        if structured.get("code") == "version_conflict":
            exit_code = EXIT_CONFLICT
        super().__init__(f"API {status}: {message}", exit_code, structured)


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def api_url(explicit: str | None) -> str:
    url = explicit or os.environ.get("CODEV_API_URL")
    if not url:
        url = FALLBACK_API_URL if "{{" in DEFAULT_API_URL else DEFAULT_API_URL
    return url.rstrip("/")


def request(
    method: str,
    url: str,
    body: dict | None = None,
    token: str | None = None,
    timeout: int = REQUEST_TIMEOUT,
):
    """JSON request; returns the parsed body. Raises ApiError on 4xx/5xx."""
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = raw.decode(errors="replace")
        detail = payload.get("detail") if isinstance(payload, dict) else payload
        raise ApiError(exc.code, detail) from None
    except urllib.error.URLError as exc:
        raise PublishError(f"Cannot reach {url}: {exc.reason}", EXIT_UNEXPECTED)
    return json.loads(raw) if raw else None


def content_type_for(path: Path) -> str:
    known = CONTENT_TYPES.get(path.suffix.lower())
    if known:
        return known
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


def build_manifest(directory: Path) -> list[dict]:
    """Hash every file under `directory`; paths are relative and POSIX."""
    if not directory.is_dir():
        raise PublishError(f"{directory} is not a directory", EXIT_USAGE)
    entries: list[dict] = []
    for root, dirs, files in os.walk(directory):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for name in sorted(files):
            if name in SKIP_FILES:
                continue
            full = Path(root) / name
            relative = PurePosixPath(full.relative_to(directory).as_posix())
            if relative.parts and relative.parts[0] == RESERVED_PREFIX:
                raise PublishError(
                    f"{relative} is under the reserved {RESERVED_PREFIX}/ folder",
                    EXIT_USAGE,
                )
            digest = hashlib.sha256()
            with open(full, "rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            entries.append(
                {
                    "path": str(relative),
                    "size": full.stat().st_size,
                    "content_type": content_type_for(full),
                    "sha256": digest.hexdigest(),
                }
            )
    if not entries:
        raise PublishError(f"{directory} has no files to publish", EXIT_USAGE)
    return entries


def request_manifest(
    base_url: str,
    files: list[dict],
    *,
    site: str | None,
    slug: str | None,
    name: str | None,
    spa: bool | None,
    base_version: str | None,
    token: str | None,
) -> dict:
    if site:
        body: dict = {"files": files}
        if spa is not None:
            body["spa_mode"] = spa
        if base_version:
            body["base_version_id"] = base_version
        return request("POST", f"{base_url}/v1/sites/{site}/versions", body, token)
    body = {"files": files, "spa_mode": bool(spa)}
    if slug:
        body["slug"] = slug
    if name:
        body["display_name"] = name
    return request("POST", f"{base_url}/v1/publishes", body, token)


def put_file(upload: dict, directory: Path) -> None:
    data = (directory / upload["path"]).read_bytes()
    last_error: Exception | None = None
    for attempt in range(1, UPLOAD_ATTEMPTS + 1):
        req = urllib.request.Request(
            upload["put_url"],
            data=data,
            method=upload.get("method", "PUT"),
            headers=dict(upload.get("headers") or {}),
        )
        try:
            with urllib.request.urlopen(req, timeout=UPLOAD_TIMEOUT) as response:
                response.read()
            return
        except urllib.error.HTTPError as exc:
            last_error = exc
            if 400 <= exc.code < 500 and exc.code != 429:
                break
        except urllib.error.URLError as exc:
            last_error = exc
        if attempt < UPLOAD_ATTEMPTS:
            time.sleep(0.5 * attempt)
    raise PublishError(f"Upload of {upload['path']} failed: {last_error}", EXIT_UPLOAD)


def upload_all(uploads: list[dict], directory: Path, concurrency: int) -> None:
    if not uploads:
        return
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        for _ in pool.map(lambda u: put_file(u, directory), uploads):
            pass


def finalize(
    base_url: str, finalize_url: str, token: str | None, base_version: str | None
) -> dict:
    url = finalize_url if finalize_url.startswith("http") else base_url + finalize_url
    body = {"base_version_id": base_version} if base_version else None
    try:
        return request("POST", url, body, token)
    except ApiError as exc:
        if exc.exit_code == EXIT_CONFLICT:
            raise
        raise PublishError(str(exc), EXIT_FINALIZE, exc.detail) from None


def pair(base_url: str, name: str) -> str:
    """Run the device-code pairing and return the new API key."""
    started = request("POST", f"{base_url}/v1/auth/agent/request-code", {"name": name})
    log("")
    log("Codev needs your approval to create an API key for this agent.")
    log(f"  Open:  {started['verify_url']}")
    log(f"  Code:  {started['user_code']}")
    log("")
    deadline = time.time() + started.get("expires_in", 600)
    interval = max(1, int(started.get("poll_interval", 5)))
    while time.time() < deadline:
        time.sleep(interval)
        result = request(
            "POST",
            f"{base_url}/v1/auth/agent/exchange",
            {"pairing_id": started["pairing_id"]},
        )
        status = result.get("status")
        if status == "approved":
            log("Approved. Save the key below as CODEV_API_KEY.")
            return result["api_key"]
        if status != "pending":
            raise PublishError(f"Pairing {status}", EXIT_PAIRING)
    raise PublishError("Pairing expired before it was approved", EXIT_PAIRING)


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="publish.py",
        description="Publish a folder of static files to Codev.",
    )
    parser.add_argument("directory", help="the folder to publish, e.g. ./dist")
    parser.add_argument("--site", help="publish a new version of this site id")
    parser.add_argument("--slug", help="hostname label for a new site")
    parser.add_argument("--name", help="display name for a new site")
    parser.add_argument(
        "--spa",
        action="store_true",
        default=None,
        help="serve index.html for unknown paths (single-page apps)",
    )
    parser.add_argument(
        "--base-version",
        help="the version id you built on; fails with exit 4 if it moved",
    )
    parser.add_argument("--base-url", help="API origin (or CODEV_API_URL)")
    parser.add_argument("--api-key", help="API key (or CODEV_API_KEY)")
    parser.add_argument(
        "--pair",
        action="store_true",
        help="obtain an API key first by pairing with a signed-in human",
    )
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument(
        "--json", action="store_true", help="print one JSON object instead of lines"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        base_url = api_url(args.base_url)
        token = args.api_key or os.environ.get("CODEV_API_KEY") or None
        result: dict = {}

        if args.pair and not token:
            token = pair(base_url, args.name or "Claude Code")
            result["api_key"] = token

        directory = Path(args.directory).resolve()
        files = build_manifest(directory)
        log(f"Hashed {len(files)} files in {directory}")

        created = request_manifest(
            base_url,
            files,
            site=args.site,
            slug=args.slug,
            name=args.name,
            spa=args.spa,
            base_version=args.base_version,
            token=token,
        )
        uploads = created.get("uploads") or []
        log(
            f"Uploading {len(uploads)} files "
            f"({len(created.get('skipped') or [])} already stored)"
        )
        upload_all(uploads, directory, args.concurrency)

        finalize_token = token or created.get("publish_token")
        finalized = finalize(
            base_url, created["finalize_url"], finalize_token, args.base_version
        )
        log("Published.")

        result.update(
            {
                "site_url": finalized.get("site_url") or created["site_url"],
                "site_id": created["site_id"],
                "version_id": created["version_id"],
                "preview_url": created.get("preview_url"),
                "is_current": finalized.get("is_current"),
            }
        )
        if created.get("claim_url"):
            result["claim_url"] = created["claim_url"]
            result["expires_at"] = created.get("expires_at")
            log("This site is anonymous and expires in 24 hours unless claimed.")
    except PublishError as exc:
        log(f"Error: {exc}")
        if exc.detail.get("current_version_id"):
            print(f"current_version_id={exc.detail['current_version_id']}")
        if exc.detail.get("missing") or exc.detail.get("mismatched"):
            log(f"  missing: {exc.detail.get('missing')}")
            log(f"  mismatched: {exc.detail.get('mismatched')}")
        return exc.exit_code

    if args.json:
        print(json.dumps(result))
    else:
        for key in (
            "site_url",
            "site_id",
            "version_id",
            "preview_url",
            "claim_url",
            "expires_at",
            "api_key",
        ):
            if result.get(key):
                print(f"{key}={result[key]}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
