#!/usr/bin/env python3
"""Publish a folder of static files to Codev and print its URL.

Python 3.10 or newer. Explicit environment credentials need no dependencies;
interactive connections prepare an isolated native credential-store helper.

    python3 publish.py ./dist --spa
    python3 publish.py ./dist --site <site id> --base-version <version id>
    python3 publish.py ./dist --pair

Environment:
    CODEV_API_KEY   optional explicit automation credential; otherwise reuse
                    the saved connection, or connect once with --connect
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
import urllib.parse
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
    from auth import NoRedirect

    data = json.dumps(body).encode() if body is not None else None
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.build_opener(NoRedirect).open(
            req, timeout=timeout
        ) as response:
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


def source_project_root(directory: Path) -> Path | None:
    """Recognize source projects before an output upload loses edit history."""
    for candidate in (directory, *directory.parents):
        if (candidate / ".codev" / "project.json").is_file():
            return candidate
        package = candidate / "package.json"
        if package.is_file():
            try:
                data = json.loads(package.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
            if isinstance(data, dict):
                dependencies = set()
                for field in ("dependencies", "devDependencies"):
                    if isinstance(data.get(field), dict):
                        dependencies.update(data[field])
                if {"react", "vite"} <= dependencies:
                    return candidate
        if (candidate / ".git").exists():
            break
    return None


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
    base_url: str,
    finalize_url: str,
    token: str | None,
    base_version: str | None,
    *,
    activate: bool = True,
) -> dict:
    url = finalize_url if finalize_url.startswith("http") else base_url + finalize_url
    if (
        urllib.parse.urlsplit(url).netloc != urllib.parse.urlsplit(base_url).netloc
        or urllib.parse.urlsplit(url).scheme != urllib.parse.urlsplit(base_url).scheme
    ):
        raise PublishError(
            "The finalize URL does not belong to the trusted Codev API.", EXIT_AUTH
        )
    body = {"base_version_id": base_version} if base_version else {}
    if not activate:
        body["activate"] = False
    try:
        return request("POST", url, body or None, token)
    except ApiError as exc:
        if exc.exit_code in (EXIT_CONFLICT, EXIT_AUTH):
            raise
        raise PublishError(str(exc), EXIT_FINALIZE, exc.detail) from None


def pair(base_url: str, name: str, *, scopes: list[str] | None = None) -> str:
    """Compatibility helper: connect securely and keep the credential internal."""
    from auth import get_token

    return get_token(base_url, connect=True, name=name)


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
        "--connect",
        action="store_true",
        help="connect once if no usable saved connection exists",
    )
    parser.add_argument(
        "--temporary", action="store_true", help="use a connection only in this process"
    )
    parser.add_argument(
        "--open-browser",
        action="store_true",
        help="open the connection link in the default browser",
    )
    parser.add_argument("--account", help="choose a saved Codev account")
    parser.add_argument("--task", help="short action label for the consent page")
    parser.add_argument(
        "--claim-token-stdin",
        action="store_true",
        help="read anonymous-site save proof from stdin",
    )
    parser.add_argument(
        "--pair",
        action="store_true",
        help="alias for --connect; reuse or securely remember an agent connection",
    )
    parser.add_argument(
        "--output-only",
        action="store_true",
        help="explicitly publish built files without saving editable source",
    )
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument(
        "--preview",
        action="store_true",
        help="prepare a preview without changing the live version",
    )
    parser.add_argument(
        "--json", action="store_true", help="print one JSON object instead of lines"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    from auth import (
        AuthError,
        event,
        get_token,
    )
    from auth import request as auth_request
    from auth import (
        trusted_origin,
    )

    args = parse_args(argv)
    try:
        base_url = trusted_origin(api_url(args.base_url))
        directory = Path(args.directory).resolve()
        source_root = source_project_root(directory)
        if source_root is not None and (
            source_root == directory or not args.output_only
        ):
            raise PublishError(
                "This is a React/Vite or saved source project. Use project.py init, save, "
                "build --trust, and publish from its source root so the site can be edited "
                "on another computer. For an intentional built-files-only upload, publish "
                "the output folder with --output-only; it will not save source.",
                EXIT_USAGE,
            )
        files = build_manifest(directory)
        if args.site and not args.base_version and not args.preview:
            raise PublishError(
                "Pass --base-version from the site's state before editing. Resolve the site first; do not overwrite intervening changes.",
                EXIT_USAGE,
            )
        site_url = (
            args.site
            if args.site and ("://" in args.site or "." in args.site)
            else None
        )
        token = get_token(
            base_url,
            explicit=args.api_key,
            connect=args.connect or args.pair,
            temporary=args.temporary,
            open_browser=args.open_browser,
            account=args.account,
            site=site_url,
            task=args.task,
            name="Coding agent",
            claim_token=sys.stdin.read(512).strip() if args.claim_token_stdin else None,
        )
        result: dict = {}
        if args.site and not token:
            raise AuthError(
                "authorization_required",
                "Connect Codev once with --connect to update this site. An already claimed site does not need to be claimed again.",
            )
        if site_url:
            resolved = auth_request(
                base_url,
                "/v1/sites/lookup?" + urllib.parse.urlencode({"url": site_url}),
                token=token,
            )
            args.site = resolved["site"]["id"]
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
            base_url,
            created["finalize_url"],
            finalize_token,
            args.base_version,
            activate=not args.preview,
        )
        log("Preview ready." if args.preview else "Published.")

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
    except AuthError as exc:
        event(exc.code, message=str(exc), **exc.detail)
        return (
            EXIT_PAIRING
            if exc.code
            in ("connection_denied", "connection_cancelled", "connection_expired")
            else EXIT_AUTH
        )
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
        ):
            if result.get(key):
                print(f"{key}={result[key]}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
