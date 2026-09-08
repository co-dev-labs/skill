"""Authenticated source API transport, with retry-safe mutations."""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from auth import USER_AGENT
from project_files import local_source_path
from publish import ApiError, PublishError


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward an API bearer token to a different origin.
        return None


class ProjectAPI:
    def __init__(self, origin: str, token: str):
        parsed = urllib.parse.urlsplit(origin)
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError(
                "Use an API origin without credentials, query, or fragment."
            )
        if parsed.scheme != "https" and not (
            parsed.scheme == "http"
            and parsed.hostname in ("localhost", "127.0.0.1", "::1")
        ):
            raise ValueError(
                "The API must use HTTPS, except for localhost development."
            )
        self.origin = origin.rstrip("/")
        self.token = token
        self.opener = urllib.request.build_opener(NoRedirect)

    def request(
        self,
        method: str,
        path: str,
        body=None,
        *,
        key: str | None = None,
        revision: int | None = None,
    ):
        if not path.startswith("/v1/") or path.startswith("//"):
            raise ValueError("Unexpected API resource path.")
        headers = {
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
            "Authorization": f"Bearer {self.token}",
        }
        if key:
            headers["Idempotency-Key"] = key
        if revision is not None:
            headers["If-Match"] = f'"{revision}"'
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode()
        # Only GET and explicitly idempotent mutations are automatically retried.
        attempts = 3 if method == "GET" or key or path.endswith("/finalize") else 1
        for attempt in range(attempts):
            req = urllib.request.Request(
                self.origin + path, data=data, headers=headers, method=method
            )
            try:
                with self.opener.open(req, timeout=120) as response:
                    raw = response.read(16 * 1024 * 1024 + 1)
                    if len(raw) > 16 * 1024 * 1024:
                        raise ValueError("API response exceeds the protocol limit.")
                    return json.loads(raw) if raw else None
            except urllib.error.HTTPError as exc:
                if exc.code >= 500 and attempt + 1 < attempts:
                    time.sleep(attempt + 1)
                    continue
                try:
                    detail = json.loads(exc.read(65536)).get("detail", "Request failed")
                except ValueError:
                    detail = "Request failed"
                raise ApiError(exc.code, detail) from None
            except (urllib.error.URLError, TimeoutError) as exc:
                if attempt + 1 == attempts:
                    raise PublishError(
                        "Cannot reach Codev. Retry the same command to resume safely.",
                        1,
                    ) from exc
                time.sleep(attempt + 1)

    def get(self, path: str):
        return self.request("GET", path)

    def post(self, path: str, body, *, key: str | None = None):
        return self.request("POST", path, body, key=key)

    def activate(self, path, body, *, key=None, wait_seconds=900):
        deadline = time.monotonic() + wait_seconds
        while True:
            try:
                return self.post(path, body, key=key)
            except ApiError as error:
                if (
                    error.detail.get("code") != "checkpoint_pending"
                    or time.monotonic() >= deadline
                ):
                    raise
                print(
                    "Waiting for the deployment's data checkpoint to be verified...",
                    file=__import__("sys").stderr,
                    flush=True,
                )
                time.sleep(5)

    def download(self, path, destination, *, max_bytes=1073741824):
        import tempfile

        if not path.startswith("/v1/") or path.startswith("//"):
            raise ValueError("Unexpected download path.")
        destination = Path(destination)
        if destination.exists() or destination.is_symlink():
            raise ValueError(
                "Choose a new export file; existing files are never overwritten."
            )
        descriptor, temporary = tempfile.mkstemp(
            prefix=".codev-download-", dir=destination.parent
        )
        try:
            request = urllib.request.Request(
                self.origin + path,
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "User-Agent": USER_AGENT,
                },
            )
            with (
                os.fdopen(descriptor, "wb") as output,
                self.opener.open(request, timeout=120) as response,
            ):
                size = 0
                while chunk := response.read(1048576):
                    size += len(chunk)
                    if size > max_bytes:
                        raise ValueError(
                            "Export exceeds the configured download limit."
                        )
                    output.write(chunk)
            from import_data import validate_archive

            validate_archive(temporary, max_bytes)
            os.link(temporary, destination)
        finally:
            os.unlink(temporary)
        return {"file": str(destination), "bytes": size, "verified": True}


def upload_verified(uploads: list[dict], files: list[dict], directory: Path) -> None:
    """Upload only requested manifest entries, after checking their exact bytes."""
    expected = {file["path"]: file for file in files}

    def upload(instruction: dict) -> None:
        file = expected.get(instruction["path"])
        if file is None:
            raise ValueError(
                "The server requested a file outside the submitted manifest."
            )
        url = instruction["put_url"]
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" and not (
            parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1")
        ):
            raise ValueError("Uploads must use HTTPS.")
        descriptor = os.open(
            local_source_path(directory, file["path"]),
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
        with os.fdopen(descriptor, "rb") as stream:
            data = stream.read(file["size"] + 1)
        if (
            len(data) != file["size"]
            or hashlib.sha256(data).hexdigest() != file["sha256"]
        ):
            raise ValueError(
                f'File changed before upload: {file["path"]}. Retry after edits finish.'
            )
        opener = urllib.request.build_opener(NoRedirect)
        for attempt in range(3):
            request = urllib.request.Request(
                url, data=data, headers=instruction.get("headers", {}), method="PUT"
            )
            try:
                with opener.open(request, timeout=120):
                    return
            except (urllib.error.URLError, TimeoutError):
                if attempt == 2:
                    raise PublishError(
                        f'Upload failed for {file["path"]}. Retry the command to resume.',
                        5,
                    ) from None
                time.sleep(attempt + 1)

    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(upload, uploads))
