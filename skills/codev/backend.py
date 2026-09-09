#!/usr/bin/env python3
"""Manage Codev backend configuration, write-only secrets, records, and recovery.

Secret input uses a hidden prompt or stdin, never a command-line value.
Data restoration requires a reviewed comparison digest and current data revision.
"""

from __future__ import annotations

import argparse
import getpass
import json
import re
import sys
import urllib.parse
from pathlib import Path
from uuid import UUID, uuid4

from auth import AuthError, event, get_token
from auth import request as auth_request
from project_api import ProjectAPI
from project_files import validate_backend
from project_workspace import Workspace
from publish import PublishError, api_url


class CommandError(ValueError):
    """A safe argument diagnostic containing no input values."""


def document(path, *, limit=1048576):
    with Path(path).open("rb") as source:
        raw = source.read(limit + 1)
    if len(raw) > limit:
        raise CommandError("Input file exceeds the operation's size limit.")
    try:
        return json.loads(raw)
    except ValueError:
        raise CommandError("Input file must contain valid JSON.") from None


def env_values(path):
    if Path(path).is_symlink():
        raise CommandError("Environment imports must use a regular local file.")
    with Path(path).open(encoding="utf-8") as source:
        raw = source.read(1048577)
    if len(raw) > 1048576:
        raise CommandError("Environment file exceeds 1 MiB.")
    values = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, value = line.removeprefix("export ").partition("=")
        name = name.strip()
        if (
            not separator
            or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", name)
            or name in values
        ):
            raise CommandError(
                "Environment file contains an invalid or repeated variable name."
            )
        value = value.strip()
        if value.startswith(('"', "'")):
            if len(value) < 2 or value[-1] != value[0]:
                raise CommandError(
                    "Environment imports support single-line literal values only."
                )
            value = value[1:-1]
        if not value or len(value) > 8192:
            raise CommandError(
                "Environment variable values must contain 1 to 8192 characters."
            )
        values[name] = value
    return values


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--api")
    result.add_argument("--site", help="owned site UUID, or use an attached workspace")
    result.add_argument("--directory", type=Path, default=Path.cwd())
    result.add_argument("--environment", default="production")
    result.add_argument("--connect", action="store_true")
    result.add_argument("--account")
    result.add_argument("--open-browser", action="store_true")
    result.add_argument("--temporary", action="store_true")
    commands = result.add_subparsers(dest="command", required=True)
    for name in (
        "status",
        "config-get",
        "secrets",
        "activate-preview",
        "disable",
        "backups",
        "recoveries",
    ):
        commands.add_parser(name)
    config = commands.add_parser("config-save")
    config.add_argument("--file", type=Path, default=Path("codev.backend.json"))
    validation = commands.add_parser(
        "validate",
        help="validate a backend definition before connecting; no app or credential required",
    )
    validation.add_argument("--file", type=Path, default=Path("codev.backend.json"))
    for name in ("secret-set", "secret-revoke"):
        command = commands.add_parser(name)
        command.add_argument("name")
        if name == "secret-set":
            command.add_argument("--stdin", action="store_true")
            command.add_argument(
                "--uses",
                type=Path,
                required=True,
                help="JSON array of approved {url, method} endpoints",
            )
    imported = commands.add_parser("env-import")
    imported.add_argument("file", type=Path)
    imported.add_argument(
        "--bindings",
        type=Path,
        required=True,
        help="JSON object mapping each variable name to approved endpoints",
    )
    imported.add_argument(
        "--apply",
        action="store_true",
        help="write the listed variables to this environment",
    )
    data = commands.add_parser("data")
    data.add_argument(
        "action",
        choices=["list", "get", "create", "patch", "delete", "history", "restore"],
    )
    data.add_argument("collection")
    data.add_argument("--record")
    data.add_argument("--revision", type=int)
    data.add_argument("--restore-revision", type=int)
    data.add_argument("--file", type=Path)
    data.add_argument("--key")
    data.add_argument("--after")
    for name in ("backup", "export"):
        command = commands.add_parser(name)
        command.add_argument("--history", action="store_true")
        command.add_argument("--key", default=None)
    for name in (
        "backup-status",
        "backup-retry",
        "download",
        "recovery-status",
        "recovery-changes",
        "recovery-retry",
        "recovery-cancel",
    ):
        command = commands.add_parser(name)
        command.add_argument("id")
        if name == "download":
            command.add_argument("destination", type=Path)
        if name == "recovery-changes":
            command.add_argument("--after")
    recovery = commands.add_parser("recovery-create")
    recovery.add_argument("checkpoint")
    recovery.add_argument(
        "--mode", choices=["inspect", "selective", "replace"], default="inspect"
    )
    recovery.add_argument("--selection", type=Path, help="JSON array of record UUIDs")
    recovery.add_argument("--key")
    approval = commands.add_parser("recovery-approve")
    approval.add_argument("id")
    approval.add_argument("--report-digest", required=True)
    approval.add_argument("--expected-revision", required=True, type=int)
    copied = commands.add_parser("copy-preview")
    copied.add_argument(
        "--file",
        type=Path,
        required=True,
        help="Reviewed record_ids, expected_data_revision, and optional omit_fields",
    )
    copied.add_argument("--key", required=True)
    retired = commands.add_parser("retire")
    retired.add_argument("collection")
    retired.add_argument("--field")
    retired.add_argument("--expected-revision", required=True, type=int)
    compatible = commands.add_parser("compatibility")
    compatible.add_argument("version")
    return result


def execute(api, base, args):
    command = args.command
    if command in {"status", "config-get"}:
        return api.get(base + "/config")
    if command == "config-save":
        definition = document(args.file, limit=65536)
        return api.request("PUT", base + "/config", definition)
    if command == "secrets":
        return api.get(base + "/secrets")
    if command in {"activate-preview", "disable"}:
        return api.post(base + "/" + command, {})
    if command.startswith("secret-"):
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", args.name):
            raise CommandError("Use an uppercase environment-variable name.")
        path = base + "/secrets/" + args.name
        if command == "secret-revoke":
            api.request("DELETE", path)
            return {"name": args.name, "configured": False}
        value = (
            sys.stdin.read(8193).rstrip("\r\n")
            if args.stdin
            else getpass.getpass("Secret value: ")
        )
        if not 1 <= len(value) <= 8192:
            raise CommandError("Secret values must contain 1 to 8192 characters.")
        return api.request(
            "PUT", path, {"value": value, "uses": document(args.uses, limit=65536)}
        )
    if command == "env-import":
        values, bindings = env_values(args.file), document(args.bindings, limit=65536)
        if set(values) != set(bindings):
            raise CommandError(
                "Supply an explicit endpoint binding for every imported name, with no extra names."
            )
        metadata = {
            "environment": args.environment,
            "names": sorted(values),
            "applied": False,
        }
        print(json.dumps(metadata), file=sys.stderr)
        if args.apply:
            for name, value in values.items():
                api.request(
                    "PUT",
                    base + "/secrets/" + name,
                    {"value": value, "uses": bindings[name]},
                )
            metadata["applied"] = True
        return metadata
    if command == "data":
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", args.collection):
            raise CommandError("Invalid collection name.")
        path = base + "/data/" + args.collection
        if args.action not in {"list", "create"}:
            if not args.record:
                raise CommandError("Supply --record for this operation.")
            path += "/" + str(UUID(args.record))
        if args.action in {"list", "get", "history"}:
            return api.get(
                path
                + ("/history" if args.action == "history" else "")
                + (
                    "?" + urllib.parse.urlencode({"after": args.after})
                    if args.after
                    else ""
                )
            )
        if args.action != "create" and (not args.revision or args.revision < 1):
            raise CommandError(
                "Supply the current --revision; concurrent edits are never overwritten."
            )
        if args.action in {"create", "patch"} and not args.file:
            raise CommandError("Supply the record JSON through --file.")
        body = document(args.file, limit=65536) if args.file else None
        if args.action == "create":
            if not args.key:
                raise CommandError(
                    "Supply a stable --key and reuse it when retrying this create."
                )
            return api.request("POST", path, {"data": body}, key=args.key)
        if args.action == "restore":
            if not args.restore_revision:
                raise CommandError(
                    "Choose --restore-revision after reading record history."
                )
            return api.request(
                "POST",
                path + "/restore",
                {"revision": args.restore_revision},
                revision=args.revision,
            )
        return api.request(
            "DELETE" if args.action == "delete" else "PATCH",
            path,
            body,
            revision=args.revision,
        )
    if command in {"backups", "recoveries"}:
        return api.get(base + ("/backups" if command == "backups" else "/recovery"))
    if command in {"backup", "export"}:
        key = args.key or str(uuid4())
        print(json.dumps({"operation_key": key}), file=sys.stderr)
        return api.post(
            base + ("/backups" if command == "backup" else "/exports"),
            {"include_history": args.history},
            key=key,
        )
    if command in {"backup-status", "backup-retry", "download"}:
        path = base + "/backups/" + str(UUID(args.id))
        if command == "download":
            return api.download(path + "/download", args.destination)
        return (
            api.post(path + "/retry", {})
            if command == "backup-retry"
            else api.get(path)
        )
    if command == "recovery-create":
        key = args.key or str(uuid4())
        print(json.dumps({"operation_key": key}), file=sys.stderr)
        return api.post(
            base + "/recovery",
            {
                "checkpoint_id": str(UUID(args.checkpoint)),
                "mode": args.mode,
                "selection": document(args.selection) if args.selection else [],
            },
            key=key,
        )
    if command.startswith("recovery-"):
        path = base + "/recovery/" + str(UUID(args.id))
        if command == "recovery-approve":
            return api.post(
                path + "/approve",
                {
                    "report_digest": args.report_digest,
                    "expected_revision": args.expected_revision,
                },
            )
        if command == "recovery-status":
            return api.get(path)
        if command == "recovery-changes":
            return api.get(
                path
                + "/changes"
                + (
                    "?" + urllib.parse.urlencode({"after": args.after})
                    if args.after
                    else ""
                )
            )
        return api.post(
            path + ("/retry" if command == "recovery-retry" else "/cancel"), {}
        )
    if command == "copy-preview":
        if args.environment != "preview":
            raise CommandError("Select --environment preview for test-data copying.")
        return api.post(
            base + "/copy-from-production",
            document(args.file, limit=65536),
            key=args.key,
        )
    if command == "retire":
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", args.collection):
            raise CommandError("Invalid collection name.")
        return api.post(
            base + "/collections/" + args.collection + "/retire",
            {"field": args.field, "expected_data_revision": args.expected_revision},
        )
    if command == "compatibility":
        return api.get(base + "/versions/" + str(UUID(args.version)) + "/compatibility")
    raise CommandError("Unknown backend operation.")


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.environment not in {"production", "preview"} and not re.fullmatch(
            r"recovery_[a-f0-9]{32}", args.environment
        ):
            raise CommandError(
                "Select production, preview, or an exact recovery environment."
            )
        origin = api_url(args.api)
        workspace = Workspace(args.directory)
        if (
            workspace.state.get("api_origin")
            and workspace.state["api_origin"] != origin
        ):
            raise CommandError("Select this workspace's trusted API origin explicitly.")
        if args.command in {"validate", "config-save"}:
            if not args.file.is_absolute():
                args.file = args.directory / args.file
            validation = validate_backend(origin, document(args.file, limit=65536))
            if args.command == "validate":
                print(json.dumps(validation))
                return 0
        if (
            not auth_request(origin, "/v1/capabilities")
            .get("managed_backend", {})
            .get("enabled")
        ):
            raise CommandError("Managed backends are not enabled on this Codev server.")
        site = str(UUID(args.site or workspace.state.get("site_id", "")))
        permission = "data"
        if args.command in {"status", "config-get", "secrets", "compatibility"}:
            permission = "read"
        elif args.command in {"config-save", "activate-preview", "disable", "retire"}:
            permission = "manage"
        elif args.command in {"secret-set", "secret-revoke", "env-import"}:
            permission = "secrets"
        token = get_token(
            origin,
            connect=args.connect,
            account=args.account,
            open_browser=args.open_browser,
            temporary=args.temporary,
            required_scopes={f"backend:{permission}:{site}"},
        )
        if not token:
            raise CommandError(
                "Run this command with --connect to approve backend access for this app in Codev. For advanced automation, supply an explicit CODEV_API_KEY with the required app permission."
            )
        api = ProjectAPI(origin, token)
        result = execute(api, f"/v1/sites/{site}/backend/{args.environment}", args)
        print(json.dumps(result, indent=2))
        return 0
    except AuthError as error:
        event(error.code, message=str(error), **error.detail)
        return 3
    except PublishError as error:
        print(str(error), file=sys.stderr)
        if error.detail:
            print(json.dumps(error.detail), file=sys.stderr)
        return error.exit_code
    except CommandError as error:
        print(str(error), file=sys.stderr)
        return 2
    except (ValueError, OSError):
        # Never echo a dotenv line, secret request body, URL credential, or DSN.
        print(
            "Backend command failed. Check the operation's arguments, explicit site permission, and input-file format. No secret values are printed.",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
