#!/usr/bin/env python3
"""Connect once to Codev; credentials stay in the operating system store.

Python 3.10+. Explicit CODEV_API_KEY automation needs no dependencies.
Interactive connections bootstrap an isolated native-keyring helper.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

EDITING_SCOPES = {"sites:read", "sites:edit", "sources:read", "sources:write"}
DEFAULT_API_URL = "https://api.co.dev"
USER_AGENT = "codev/1.0"


class AuthError(Exception):
    def __init__(self, code: str, message: str, **detail):
        super().__init__(message)
        self.code = code
        self.detail = detail


def event(name: str, **detail) -> None:
    print(json.dumps({"event": name, **detail}), file=sys.stderr, flush=True)


def trusted_origin(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
        or not parsed.hostname
    ):
        raise AuthError(
            "api_origin_invalid",
            "Use a trusted Codev API origin without a path or credentials.",
        )
    if parsed.scheme != "https" and not (
        parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1", "::1")
    ):
        raise AuthError(
            "api_origin_invalid",
            "Codev connections require HTTPS, except for explicit localhost testing.",
        )
    return value.rstrip("/")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request(origin: str, path: str, body=None, *, token=None, method=None):
    origin = trusted_origin(origin)
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        origin + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers=headers,
        method=method or ("POST" if body is not None else "GET"),
    )
    try:
        with urllib.request.build_opener(NoRedirect).open(req, timeout=30) as response:
            raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise AuthError(
                    "response_invalid",
                    "Codev returned an oversized connection response.",
                )
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read(65536)).get("detail", {})
        except ValueError:
            payload = {}
        detail = payload if isinstance(payload, dict) else {}
        if isinstance(payload, list):
            issues = [
                ".".join(str(part) for part in item.get("loc", []))
                + ": "
                + item.get("msg", "Invalid value")
                for item in payload
                if isinstance(item, dict)
            ]
            detail = {
                "code": (
                    "backend_definition_invalid"
                    if path == "/v1/backend/validate"
                    else "request_invalid"
                ),
                "message": "; ".join(issues),
            }
        raise AuthError(
            detail.get("code", f"http_{exc.code}"),
            detail.get(
                "message",
                {
                    401: "Your Codev connection is no longer valid.",
                    403: "This connection does not have the required permission.",
                    404: "This account cannot access the site or connection.",
                }.get(exc.code, "Codev could not complete this request."),
            ),
            status=exc.code,
        ) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise AuthError(
            "network_unavailable",
            "Cannot reach Codev. Retry the same command to resume.",
        ) from None


def require_scopes(identity: dict, required_scopes) -> None:
    available = set(identity.get("scopes", []))
    if "sites:write" in available:
        available.add("sites:edit")
    missing = set(required_scopes) - available
    if missing:
        backend = any(scope.startswith("backend:") for scope in missing)
        raise AuthError(
            "backend_scope_required" if backend else "source_scope_required",
            (
                "This credential needs additional access for this app's backend. Use --connect with a saved agent connection, or update the explicit automation key in the dashboard."
                if backend
                else "This credential cannot access the requested private source. "
                "A saved connection can request source access with --connect. "
                "For an explicit API key, use a key with the required source permissions."
            ),
            required_scopes=sorted(required_scopes),
            missing_scopes=sorted(missing),
        )


def backend_request(scopes):
    permissions, sites = set(), set()
    for scope in scopes:
        if scope in EDITING_SCOPES:
            continue
        parts = scope.split(":")
        if (
            len(parts) != 3
            or parts[0] != "backend"
            or parts[1] not in {"read", "manage", "secrets", "data"}
        ):
            raise AuthError(
                "scope_unsupported", "This connection cannot request that permission."
            )
        try:
            sites.add(str(uuid.UUID(parts[2])))
        except ValueError:
            raise AuthError(
                "scope_unsupported", "Backend permissions need an exact app ID."
            ) from None
        permissions.add(parts[1])
    if len(sites) > 1:
        raise AuthError(
            "scope_unsupported", "Request backend access for one app at a time."
        )
    return next(iter(sites), None), sorted(permissions)


def config_directory() -> Path:
    if os.environ.get("CODEV_CONFIG_DIR"):
        return Path(os.environ["CODEV_CONFIG_DIR"]).expanduser().absolute()
    if sys.platform == "win32":
        return (
            Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
            / "Codev"
        )
    return Path.home() / ".config/codev"


class NativeStore:
    def __init__(self, root: Path):
        self.runtime = root / "keyring-runtime-v1"
        self.python = self.runtime / (
            "Scripts/python.exe" if sys.platform == "win32" else "bin/python"
        )

    def prepare(self):
        requirements = Path(__file__).with_name("auth-requirements.txt")
        digest = hashlib.sha256(requirements.read_bytes()).hexdigest()
        ready = self.runtime / ".ready"
        if self.python.is_file() and ready.is_file() and ready.read_text() == digest:
            return
        event(
            "preparing_connection",
            message="Preparing secure storage for your Codev connection.",
        )
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("PYTHON", "PIP_", "CODEV_", "KEYRING_"))
        }
        commands = [
            [sys.executable, "-I", "-m", "venv", str(self.runtime)],
            [
                str(self.python),
                "-I",
                "-m",
                "pip",
                "--isolated",
                "install",
                "--disable-pip-version-check",
                "--require-hashes",
                "--only-binary=:all:",
                "--requirement",
                str(requirements),
            ],
        ]
        try:
            for command in commands:
                subprocess.run(
                    command,
                    cwd=self.runtime.parent,
                    env=env,
                    capture_output=True,
                    timeout=180,
                    check=True,
                )
            ready.write_text(digest)
        except (OSError, subprocess.SubprocessError):
            raise AuthError(
                "credential_store_unavailable",
                "Secure storage could not be prepared. Retry setup, or choose a temporary connection with --temporary.",
            ) from None

    def call(self, action: str, service: str, account: str, value=None):
        env = {
            key: val
            for key, val in os.environ.items()
            if not key.startswith(("PYTHON", "PIP_", "CODEV_", "KEYRING_"))
        }
        try:
            result = subprocess.run(
                [
                    str(self.python),
                    "-I",
                    str(Path(__file__).with_name("auth_store.py")),
                ],
                input=json.dumps(
                    {
                        "action": action,
                        "service": service,
                        "account": account,
                        "value": value,
                    }
                ),
                capture_output=True,
                text=True,
                env=env,
                cwd=self.runtime,
                timeout=120,
            )
        except (OSError, subprocess.SubprocessError):
            raise AuthError(
                "credential_store_unavailable",
                "Secure storage is unavailable. Retry when it is ready; do not repeat Codev sign-in.",
            ) from None
        try:
            payload = json.loads(result.stdout)
        except ValueError:
            payload = {"ok": False, "code": "credential_store_unavailable"}
        if not payload.get("ok"):
            code = payload.get("code", "credential_store_unavailable")
            raise AuthError(
                code,
                "Unlock your operating system credential store and retry. Your Codev sign-in does not need to be repeated.",
            )
        return payload.get("value")


class Connections:
    def __init__(self, origin: str, *, root: Path | None = None, store=None):
        self.origin = trusted_origin(origin)
        self.root = root or config_directory()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = self.root / "connections.json"
        self.store = store or NativeStore(self.root)
        self.state = {}
        self.reload()

    def reload(self):
        try:
            self.state = json.loads(self.path.read_text())
        except FileNotFoundError:
            self.state = {"agent_installation_id": str(uuid.uuid4()), "origins": {}}
        except ValueError:
            raise AuthError(
                "connection_config_invalid",
                "Codev connection metadata is damaged. Restore it before connecting again.",
            ) from None

    def write(self):
        temporary = self.path.with_name(".connections-" + uuid.uuid4().hex)
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(self.state, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.path)

    @contextlib.contextmanager
    def lock(self):
        # One short configuration file and one pending journey per client.
        # Holding this lock while connecting makes simultaneous callers reuse
        # the first completed grant rather than opening duplicate tabs.
        with open(self.root / ".connection.lock", "a+b") as handle:
            if sys.platform == "win32":
                import msvcrt

                handle.write(b"\0")
                handle.flush()
                handle.seek(0)
            else:
                import fcntl
            waiting = False
            deadline = time.monotonic() + 900
            while True:
                try:
                    if sys.platform == "win32":
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except (BlockingIOError, OSError):
                    if time.monotonic() >= deadline:
                        raise AuthError(
                            "connection_busy",
                            "Another task is still connecting. Retry when it finishes.",
                        ) from None
                    if not waiting:
                        event(
                            "waiting_for_connection",
                            message="Another task is connecting Codev. This task will reuse that connection.",
                        )
                        waiting = True
                    time.sleep(0.25)
            try:
                self.reload()
                yield
            finally:
                if sys.platform == "win32":
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def installation(self, *, discover=True):
        record = self.state["origins"].get(self.origin)
        if discover:
            capability = request(self.origin, "/v1/capabilities").get(
                "agent_connections", {}
            )
            installation_id = capability.get("installation_id")
            if record and record["installation_id"] != installation_id:
                raise AuthError(
                    "installation_changed",
                    "This Codev installation changed. Use auth.py logout --all for this API origin, then connect to the new installation explicitly.",
                )
            if not record:
                record = {
                    "installation_id": installation_id,
                    "accounts": {},
                    "sites": {},
                }
                self.state["origins"][self.origin] = record
            record["supported"] = capability.get(
                "protocol_version"
            ) == 2 and capability.get("enabled", False)
            record["backend_permissions"] = capability.get("backend_permissions", [])
        return record

    def service(self, record):
        identity = json.dumps(
            [
                self.origin,
                record["installation_id"],
                self.state["agent_installation_id"],
            ]
        )
        return "Codev agent " + hashlib.sha256(identity.encode()).hexdigest()

    def saved(self, record, account=None, site=None, required_scopes=()):
        selected = (
            account
            or record.get("sites", {}).get(site)
            or record.get("selected_account")
        )
        accounts = record["accounts"]
        if account and account not in accounts:
            raise AuthError("account_not_connected", "Connect this account first.")
        if not accounts:
            return None
        if selected not in accounts and len(accounts) > 1 and not site:
            raise AuthError(
                "account_selection_required",
                "Choose which connected account to use with --account.",
                accounts=accounts,
            )
        self.store.prepare()
        stored_pending = self.store.call("get", self.service(record), "pending")
        pending = json.loads(stored_pending) if stored_pending else None
        if pending and pending.get("received_account"):
            self.resume_received(record, pending)
        candidates = (
            [selected] if account else list(dict.fromkeys([selected, *accounts]))
        )
        for account_id in candidates:
            if account_id not in accounts:
                continue
            token = self.store.call("get", self.service(record), account_id)
            if not token:
                continue
            try:
                identity = request(self.origin, "/v1/auth/agent/identity", token=token)
                if (
                    identity["account_id"] != account_id
                    or identity["installation_id"] != record["installation_id"]
                ):
                    raise AuthError(
                        "connection_identity_changed",
                        "This saved connection no longer matches its account.",
                    )
                if (
                    pending
                    and pending.get("backend_site_id")
                    and pending.get("existing_connection_id")
                    == identity["connection_id"]
                ):
                    granted = {
                        f"backend:{permission}:{pending['backend_site_id']}"
                        for permission in pending["backend_permissions"]
                    }
                    if granted <= set(identity["scopes"]):
                        # The process may have stopped after the server extended
                        # this existing key but before it recorded the receipt.
                        result = request(
                            self.origin,
                            "/v1/auth/agent/connections/exchange",
                            {
                                key: pending[key]
                                for key in ("request_id", "device_secret")
                            },
                        )
                        if (
                            result["status"] == "approved"
                            and result["key_id"] == identity["connection_id"]
                        ):
                            pending["received_account"] = account_id
                            self.store.call(
                                "set",
                                self.service(record),
                                "pending",
                                json.dumps(pending),
                            )
                            self.finish(
                                record,
                                pending,
                                token,
                                remembered=True,
                                identity=identity,
                            )
                        elif result["status"] in {
                            "cancelled",
                            "denied",
                            "expired",
                            "connected",
                        }:
                            self.store.call("delete", self.service(record), "pending")
                if site:
                    request(
                        self.origin,
                        "/v1/sites/lookup?" + urllib.parse.urlencode({"url": site}),
                        token=token,
                    )
                record["selected_account"] = account_id
                record["accounts"][account_id]["scopes"] = identity["scopes"]
                if site:
                    record["sites"][site] = account_id
                self.write()
                require_scopes(identity, required_scopes)
                return token
            except AuthError as exc:
                if exc.detail.get("status") == 401:
                    self.store.call("delete", self.service(record), account_id)
                    accounts.pop(account_id)
                    self.write()
                    event(
                        "connection_expired",
                        message="Your saved Codev connection needs to be renewed.",
                    )
                    continue
                if exc.detail.get("status") == 404 and site and not account:
                    continue
                raise
        if accounts and site:
            raise AuthError(
                "site_not_accessible",
                "Your connected accounts cannot edit this site. Check its address or connect the correct account.",
                accounts=list(accounts),
            )
        return None

    def connect(
        self,
        record,
        *,
        name="Coding agent",
        device_label=None,
        site=None,
        task=None,
        temporary=False,
        open_browser=False,
        claim_token=None,
        existing_token=None,
        backend_site_id=None,
        backend_permissions=(),
    ):
        if not record.get("supported"):
            raise AuthError(
                "connections_unavailable",
                "This Codev server needs the current connection protocol. Existing CODEV_API_KEY automation still works.",
            )
        if set(backend_permissions) - set(record.get("backend_permissions", [])):
            raise AuthError(
                "backend_connection_unavailable",
                "This Codev server does not support app-specific backend consent yet. Update the server; reconnecting for source access will not help.",
            )
        claim_only = bool(existing_token) and not backend_site_id
        pending = None
        service = self.service(record)
        if not temporary:
            self.store.prepare()
            stored = self.store.call("get", service, "pending")
            pending = json.loads(stored) if stored else None
        if pending and pending.get("received_account"):
            token = self.resume_received(record, pending)
            if token:
                return token
            pending = None
        if pending and (
            pending.get("site") != site
            or pending.get("task_label") != task
            or pending.get("claim_only", False) != claim_only
            or bool(pending.get("claim_token")) != bool(claim_token)
            or pending.get("backend_site_id") != backend_site_id
            or pending.get("backend_permissions", []) != list(backend_permissions)
        ):
            # A new active task must not silently resume a different ownership journey.
            request(
                self.origin,
                "/v1/auth/agent/connections/cancel",
                {key: pending[key] for key in ("request_id", "device_secret")},
            )
            self.store.call("delete", service, "pending")
            pending = None
        if pending is None:
            pending = {
                "request_id": str(uuid.uuid4()),
                "device_secret": secrets.token_urlsafe(32),
                "name": name,
                "device_label": device_label
                or {"darwin": "this Mac", "win32": "this Windows PC"}.get(
                    sys.platform, "this Linux device"
                ),
                "agent_installation_id": self.state["agent_installation_id"],
                "site": site,
                "task_label": task,
                "claim_token": claim_token,
                "claim_only": claim_only,
                "remember": not temporary,
            }
            if backend_site_id:
                pending.update(
                    backend_site_id=backend_site_id,
                    backend_permissions=list(backend_permissions),
                )
                if existing_token:
                    pending["existing_connection_id"] = request(
                        self.origin, "/v1/auth/agent/identity", token=existing_token
                    )["connection_id"]
            # Persist BEFORE making the request. A lost start response can be replayed.
            if not temporary:
                self.store.call("set", service, "pending", json.dumps(pending))
                self.write()
        device = {key: pending[key] for key in ("request_id", "device_secret")}
        body = {
            key: value
            for key, value in pending.items()
            if key not in {"received_account", "existing_connection_id"}
        }
        try:
            started = request(
                self.origin,
                "/v1/auth/agent/connections/start",
                body,
                token=existing_token,
            )
        except AuthError as exc:
            if exc.code == "connection_request_finished" and not temporary:
                self.store.call("delete", service, "pending")
            raise
        event(
            "authorization_required",
            verify_url=started["verify_url"],
            user_code=started["user_code"],
            message=(
                "Approve the requested backend permissions for this app in Codev; this command continues automatically."
                if backend_site_id
                else (
                    "Save this site to your connected account in the browser; the task continues automatically."
                    if existing_token
                    else "Connect Codev once to edit all your sites. Approve in the browser; the task continues automatically."
                )
            ),
        )
        url = urllib.parse.urlsplit(started["verify_url"])
        if url.scheme not in ("https", "http") or (
            url.scheme == "http"
            and url.hostname not in ("localhost", "127.0.0.1", "::1")
        ):
            raise AuthError(
                "authorization_url_invalid",
                "Codev returned an unsupported authorization link.",
            )
        if open_browser:
            webbrowser.open(started["verify_url"])
        deadline = (
            datetime.fromisoformat(started["expires_at"].replace("Z", "+00:00"))
            .replace(tzinfo=timezone.utc)
            .timestamp()
        )
        interval = max(5, started.get("poll_interval", 5))
        try:
            while time.time() < deadline:
                time.sleep(interval)
                try:
                    result = request(
                        self.origin, "/v1/auth/agent/connections/exchange", device
                    )
                except AuthError as exc:
                    if (
                        exc.code == "network_unavailable"
                        or exc.detail.get("status", 0) >= 500
                    ):
                        interval = min(30, interval * 2)
                        continue
                    raise
                status = result["status"]
                if status in ("pending", "slow_down"):
                    interval = max(interval, result.get("poll_interval", 5))
                    continue
                if status != "approved":
                    if not temporary:
                        self.store.call("delete", service, "pending")
                    raise AuthError(
                        f"connection_{status}",
                        f"Connection {status}. Your pending edits have not been published.",
                    )
                token = result.get("api_key") or existing_token
                remembered = not temporary
                identity = request(self.origin, "/v1/auth/agent/identity", token=token)
                if remembered:
                    try:
                        self.store.call("set", service, identity["account_id"], token)
                        pending["received_account"] = identity["account_id"]
                        self.store.call("set", service, "pending", json.dumps(pending))
                    except AuthError:
                        remembered = False
                        event(
                            "temporary_connection",
                            message="Secure storage is unavailable. This task can continue, but this connection may not be remembered.",
                        )
                self.finish(
                    record, pending, token, remembered=remembered, identity=identity
                )
                return token
        except KeyboardInterrupt:
            try:
                request(self.origin, "/v1/auth/agent/connections/cancel", device)
            except AuthError:
                pass
            if not temporary:
                self.store.call("delete", service, "pending")
            raise AuthError(
                "connection_cancelled",
                "Connection cancelled. The live site is unchanged.",
            ) from None
        if not temporary:
            self.store.call("delete", service, "pending")
        raise AuthError(
            "connection_expired",
            "The connection request expired. Retry when you are ready to connect.",
        )

    def resume_received(self, record, pending):
        """Recover a durable receipt, or discard a grant that expired before ack."""
        account_id = pending["received_account"]
        token = self.store.call("get", self.service(record), account_id)
        if token:
            try:
                self.finish(record, pending, token, remembered=True)
                return token
            except AuthError as exc:
                if exc.detail.get("status") != 401 and exc.code not in (
                    "connection_not_delivered",
                    "connection_request_finished",
                ):
                    raise
                if (
                    pending.get("existing_connection_id")
                    and exc.detail.get("status") != 401
                ):
                    # Expiring a grant receipt must not discard the underlying
                    # remembered editing connection, whose token is unchanged.
                    self.store.call("delete", self.service(record), "pending")
                    return token
        self.store.call("delete", self.service(record), "pending")
        self.store.call("delete", self.service(record), account_id)
        record["accounts"].pop(account_id, None)
        record["sites"] = {
            site: owner
            for site, owner in record["sites"].items()
            if owner != account_id
        }
        if record.get("selected_account") == account_id:
            record.pop("selected_account", None)
        self.write()
        event(
            "connection_expired",
            message="Your saved Codev connection needs to be renewed.",
        )
        return None

    def finish(self, record, pending, token, *, remembered, identity=None):
        identity = identity or request(
            self.origin, "/v1/auth/agent/identity", token=token
        )
        if remembered:
            record["accounts"][identity["account_id"]] = {
                key: identity[key]
                for key in ("account_email", "connection_id", "scopes")
            }
            record["selected_account"] = identity["account_id"]
            self.write()
        request(
            self.origin,
            "/v1/auth/agent/connections/acknowledge",
            {
                "request_id": pending["request_id"],
                "device_secret": pending["device_secret"],
                "remembered": remembered,
            },
        )
        if remembered:
            self.store.call("delete", self.service(record), "pending")
        else:
            try:
                self.store.call("delete", self.service(record), "pending")
            except (AuthError, OSError):
                pass
        event("connected", account=identity["account_email"], remembered=remembered)


def get_token(
    origin: str,
    *,
    explicit=None,
    connect=False,
    account=None,
    site=None,
    task=None,
    temporary=False,
    open_browser=False,
    name="Coding agent",
    claim_token=None,
    manager=None,
    new_account=False,
    required_scopes=(),
):
    # Explicit credentials never silently fall back or switch accounts.
    token = explicit if explicit is not None else os.environ.get("CODEV_API_KEY")
    if token is not None:
        if not token:
            raise AuthError(
                "explicit_credential_invalid",
                "The explicitly supplied Codev credential is empty.",
            )
        if required_scopes:
            require_scopes(
                request(origin, "/v1/auth/agent/identity", token=token),
                required_scopes,
            )
        if not claim_token:
            return token
    manager = manager or Connections(origin)
    backend_site_id, backend_permissions = backend_request(required_scopes)
    with manager.lock():
        if not connect and manager.origin not in manager.state["origins"]:
            return None
        record = manager.installation()
        try:
            token = token or (
                None
                if new_account
                else manager.saved(
                    record,
                    account=account,
                    site=None if claim_token else site,
                    required_scopes=required_scopes,
                )
            )
        except AuthError as exc:
            if exc.code == "backend_scope_required" and connect:
                token = manager.saved(record, account=account, site=site)
                token = manager.connect(
                    record,
                    site=None,
                    task=task,
                    temporary=temporary,
                    open_browser=open_browser,
                    name=name,
                    existing_token=token,
                    backend_site_id=backend_site_id,
                    backend_permissions=backend_permissions,
                )
                require_scopes(
                    request(origin, "/v1/auth/agent/identity", token=token),
                    required_scopes,
                )
                return token
            if exc.code != "source_scope_required" or not connect:
                raise
            event(
                "source_access_required",
                message="Your saved connection predates private source access. "
                "Approve source access once to continue; the existing key keeps its permissions.",
            )
            token = None
        if token:
            if claim_token:
                return manager.connect(
                    record,
                    site=site,
                    task=task,
                    temporary=temporary,
                    open_browser=open_browser,
                    name=name,
                    claim_token=claim_token,
                    existing_token=token,
                )
            return token
        if not connect:
            return None
        token = manager.connect(
            record,
            site=None if backend_site_id else site,
            task=task,
            temporary=temporary,
            open_browser=open_browser,
            name=name,
            claim_token=claim_token,
            backend_site_id=backend_site_id,
            backend_permissions=backend_permissions,
        )
        require_scopes(
            request(origin, "/v1/auth/agent/identity", token=token), required_scopes
        )
        return token


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--api",
        default=os.environ.get("CODEV_API_URL")
        or ("http://localhost:8004" if "{{" in DEFAULT_API_URL else DEFAULT_API_URL),
    )
    parser.add_argument("--account")
    commands = parser.add_subparsers(dest="command", required=True)
    login = commands.add_parser("login")
    login.add_argument("--name", default="Coding agent")
    login.add_argument("--site")
    login.add_argument("--task")
    login.add_argument("--temporary", action="store_true")
    login.add_argument("--open-browser", action="store_true")
    login.add_argument(
        "--new-account",
        action="store_true",
        help="connect another account without changing existing connections",
    )
    commands.add_parser("status")
    logout = commands.add_parser("logout")
    logout.add_argument(
        "--all",
        action="store_true",
        help="forget every account and pending connection for this API origin",
    )
    switch = commands.add_parser("switch")
    switch.add_argument("account_id")
    api_call = commands.add_parser(
        "request", help="call a site API using the saved connection"
    )
    api_call.add_argument("method", choices=("GET", "POST", "PATCH", "PUT", "DELETE"))
    api_call.add_argument("path")
    api_call.add_argument("--body-file", type=Path)
    args = parser.parse_args(argv)
    try:
        manager = Connections(args.api)
        if args.command == "request":
            path = urllib.parse.urlsplit(args.path)
            decoded_path = urllib.parse.unquote(path.path)
            if (
                path.scheme
                or path.netloc
                or path.fragment
                or "\\" in decoded_path
                or ".." in decoded_path.split("/")
                or any(ord(char) < 32 for char in args.path)
                or not (
                    path.path == "/v1/sites"
                    or path.path.startswith(("/v1/sites/", "/v1/versions/"))
                )
            ):
                raise AuthError(
                    "api_path_invalid",
                    "This command supports the site management API only.",
                )
            token = get_token(args.api, account=args.account, manager=manager)
            if not token:
                raise AuthError(
                    "authorization_required",
                    "Connect Codev once before managing sites.",
                )
            body = json.loads(args.body_file.read_text()) if args.body_file else None
            result = request(args.api, args.path, body, token=token, method=args.method)
        elif args.command == "login":
            if args.temporary:
                raise AuthError(
                    "temporary_task_required",
                    "Use --temporary on the edit or publish command so the connection can remain in memory for that task.",
                )
            token = get_token(
                args.api,
                connect=True,
                account=args.account,
                site=args.site,
                task=args.task,
                open_browser=args.open_browser,
                name=args.name,
                manager=manager,
                new_account=args.new_account,
            )
            result = request(args.api, "/v1/auth/agent/identity", token=token)
        else:
            with manager.lock():
                record = manager.installation(discover=args.command != "logout") or {
                    "accounts": {},
                    "sites": {},
                }
                if args.command == "switch":
                    if args.account_id not in record["accounts"]:
                        raise AuthError(
                            "account_not_connected", "Connect that account first."
                        )
                    record["selected_account"] = args.account_id
                    manager.write()
                elif args.command == "logout":
                    selected = args.account or record.get("selected_account")
                    account_ids = (
                        list(record["accounts"])
                        if args.all
                        else ([selected] if selected else [])
                    )
                    if account_ids or (args.all and record.get("installation_id")):
                        manager.store.prepare()
                        for account_id in account_ids:
                            manager.store.call(
                                "delete", manager.service(record), account_id
                            )
                            record["accounts"].pop(account_id, None)
                        record["sites"] = {
                            site: owner
                            for site, owner in record["sites"].items()
                            if owner not in account_ids
                        }
                        if record.get("selected_account") in account_ids:
                            record.pop("selected_account", None)
                        if args.all:
                            manager.store.call(
                                "delete", manager.service(record), "pending"
                            )
                            manager.state["origins"].pop(manager.origin, None)
                        manager.write()
                    event(
                        "disconnected_locally",
                        message="Connection forgotten on this device. You can revoke server access in Connected agents.",
                    )
                result = {
                    "api_origin": manager.origin,
                    "selected_account": record.get("selected_account"),
                    "accounts": record["accounts"],
                }
        print(json.dumps(result))
        return 0
    except AuthError as exc:
        event(exc.code, message=str(exc), **exc.detail)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
