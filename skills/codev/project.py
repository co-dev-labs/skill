#!/usr/bin/env python3
"""Save, reopen, build, and restore React/Vite projects without Git.

Python 3.10+. Install the companion modules beside this script.
Credentials come from the OS store or explicit CODEV_API_KEY, never project files.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import urllib.parse
from pathlib import Path

from auth import EDITING_SCOPES, AuthError, event, get_token
from auth import request as auth_request
from project_api import ProjectAPI, upload_verified
from project_build import run_build, tool_version
from project_files import validate_path
from project_workspace import Workspace, materialize
from publish import PublishError, api_url


def save(api, workspace: Workspace, summary: str) -> dict:
    state = workspace.require()
    files = workspace.files()
    body = {
        "parent_revision_id": state["revision_id"],
        "expected_generation": state["state_generation"],
        "summary": summary,
        "build_config": state["build_config"],
        "files": files,
    }
    prepared = api.post(
        f'/v1/sites/{state["site_id"]}/revisions',
        body,
        key=workspace.operation("save", body),
    )
    # Refuse to upload bytes collected from a tree that changed while the
    # server prepared the upload. Finalization also verifies every digest.
    if workspace.files() != files:
        raise ValueError("Source changed while saving. Retry after edits finish.")
    upload_verified(prepared["uploads"], files, workspace.root)
    saved = api.post(prepared["finalize_url"], {})
    state.update(
        {
            "revision_id": saved["revision"]["id"],
            "content_digest": saved["revision"]["content_digest"],
            "state_generation": saved["site"]["state_generation"],
        }
    )
    workspace.write()
    return saved


def open_project(api, url: str, destination: Path, revision: str | None) -> dict:
    query = {"url": url}
    if revision:
        query["revision"] = revision
    resolved = api.get("/v1/sites/resolve?" + urllib.parse.urlencode(query))
    if not resolved["source_available"]:
        raise ValueError(
            "This site has no saved source. Built HTML/JS cannot recover the original React project; attach your original local source with init --site first."
        )
    site = resolved["site"]
    manifest = api.get(
        f'/v1/sites/{site["id"]}/revisions/{resolved["selected_revision_id"]}'
    )
    destination = destination.absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError(
            "Open needs a new directory. Existing workspaces are never overwritten."
        )
    if not destination.parent.is_dir():
        raise ValueError("Create the destination's parent directory first.")
    with tempfile.TemporaryDirectory(
        prefix=".codev-open-", dir=destination.parent
    ) as temporary:
        staging = Path(temporary) / "project"
        materialize(api, manifest, staging)
        Workspace(staging).attach(api.origin, site, manifest, manifest["build_config"])
        if destination.exists():
            raise ValueError(
                "The destination was created while downloading. Choose a fresh directory."
            )
        staging.rename(destination)
    return {**resolved, "workspace": str(destination)}


def initialize(api, workspace: Workspace, args) -> dict:
    if workspace.state.get("protocol_version"):
        raise ValueError("This workspace is already attached. Use status or save.")
    if not (workspace.root / "package.json").is_file():
        raise ValueError("Initialize from the root of an existing React/Vite project.")
    validate_path(args.output_directory)
    config = {
        "framework": "react-vite",
        "package_manager": args.package_manager,
        "node_version": tool_version("node"),
        "package_manager_version": tool_version(args.package_manager),
        "build_script": args.build_script,
        "output_directory": args.output_directory,
        "environment": {},
    }
    if args.site:
        resolved = api.get(
            "/v1/sites/resolve?" + urllib.parse.urlencode({"url": args.site})
        )
        site = resolved["site"]
        if site["head_revision_id"]:
            raise ValueError(
                "This site already has source. Open it into a fresh workspace instead."
            )
    else:
        body = {"slug": args.slug, "display_name": args.name}
        site = api.post("/v1/sites", body, key=workspace.operation("init", body))
    workspace.attach(api.origin, site, None, config)
    return save(api, workspace, args.summary)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--api", help="Codev API origin")
    result.add_argument(
        "--connect",
        action="store_true",
        help="reuse a saved connection or connect once",
    )
    result.add_argument("--account", help="choose a saved Codev account")
    result.add_argument("--temporary", action="store_true")
    result.add_argument("--open-browser", action="store_true")
    result.add_argument(
        "--task", help="short action label shown with connection consent"
    )
    result.add_argument(
        "--claim-token-stdin",
        action="store_true",
        help="read existing anonymous-site save proof from stdin",
    )
    result.add_argument(
        "--directory", type=Path, default=Path.cwd(), help="local project root"
    )
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("pair", help="request explicit approval for source access")
    resolve = commands.add_parser(
        "resolve", help="identify an owned site even when source storage is disabled"
    )
    resolve.add_argument("url")
    init = commands.add_parser(
        "init", help="attach and save an existing React/Vite project"
    )
    init.add_argument("--slug")
    init.add_argument("--name")
    init.add_argument("--site", help="existing claimed site's URL")
    init.add_argument(
        "--package-manager", choices=("npm", "pnpm", "yarn"), default="npm"
    )
    init.add_argument("--build-script", default="build")
    init.add_argument("--output-directory", default="dist")
    init.add_argument("--summary", default="Initial source snapshot")
    opened = commands.add_parser(
        "open", help="download source by pasting its domain or preview URL"
    )
    opened.add_argument("url")
    opened.add_argument("destination", type=Path)
    opened.add_argument(
        "--revision", help="live, latest, or an exact source revision id"
    )
    commands.add_parser("status")
    saved = commands.add_parser("save")
    saved.add_argument("--summary", required=True)
    built = commands.add_parser(
        "build", help="build saved source without changing the live site"
    )
    built.add_argument("--trust", action="store_true")
    commands.add_parser("publish", help="make this workspace's successful build live")
    history = commands.add_parser("history")
    history.add_argument("--before", type=int)
    restore = commands.add_parser(
        "restore", help="restore server state; local files are never overwritten"
    )
    restore.add_argument(
        "id", help="output version id, or revision id with --source-only"
    )
    restore.add_argument("--source-only", action="store_true")
    restore.add_argument("--deployment-only", action="store_true")
    return result


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        workspace = Workspace(args.directory)
        origin = api_url(args.api)
        if (
            workspace.state.get("api_origin")
            and workspace.state["api_origin"] != origin
        ):
            raise ValueError(
                "This workspace belongs to a different Codev API. Select its trusted origin explicitly with --api."
            )
        if args.command in ("init", "open"):
            capability = auth_request(origin, "/v1/capabilities")["source_projects"]
            available = (
                capability["enabled"]
                if args.command == "init"
                else capability["downloads_enabled"]
            )
            if not available:
                raise ValueError(
                    "Private source storage is not available on this Codev server. "
                    "Configure a separate R2_SOURCE_BUCKET with object read/write access; "
                    "enable SOURCES_ENABLED to save projects. Reconnecting cannot enable storage."
                )
        if args.command == "pair":
            get_token(
                origin,
                connect=True,
                account=args.account,
                open_browser=args.open_browser,
                required_scopes=EDITING_SCOPES,
            )
            print(json.dumps({"status": "connected"}))
            return 0
        site = (
            args.url
            if args.command in ("open", "resolve")
            else getattr(args, "site", None) or workspace.state.get("site_url")
        )
        token = get_token(
            origin,
            connect=args.connect,
            account=args.account,
            site=site,
            task=args.task,
            temporary=args.temporary,
            open_browser=args.open_browser,
            claim_token=sys.stdin.read(512).strip() if args.claim_token_stdin else None,
            required_scopes=(
                ()
                if args.command == "resolve"
                else (
                    {"sites:read", "sources:read"}
                    if args.command in ("open", "status", "history")
                    else EDITING_SCOPES
                )
            ),
        )
        if not token:
            raise AuthError(
                "authorization_required",
                "Connect Codev once with --connect. Browser sign-in and claiming are only requested if needed.",
            )
        api = ProjectAPI(origin, token)
        if args.command == "resolve":
            result = api.get(
                "/v1/sites/lookup?" + urllib.parse.urlencode({"url": args.url})
            )
        elif args.command == "open":
            result = open_project(api, args.url, args.destination, args.revision)
        elif args.command == "init":
            result = initialize(api, workspace, args)
        else:
            state = workspace.require()
            prefix = f'/v1/sites/{state["site_id"]}'
            if args.command == "status":
                site = api.get(prefix)
                result = {
                    "site": site,
                    "local_revision_id": state["revision_id"],
                    "dirty": workspace.dirty(),
                    "server_changed": site["state_generation"]
                    != state["state_generation"],
                }
            elif args.command == "save":
                result = save(api, workspace, args.summary)
            elif args.command == "build":
                result = run_build(api, workspace, trust=args.trust)
            elif args.command == "history":
                result = api.get(
                    prefix
                    + "/history"
                    + (f"?before={args.before}" if args.before else "")
                )
            elif args.command == "publish":
                build = state.get("build", {})
                if (
                    workspace.dirty()
                    or build.get("revision_id") != state["revision_id"]
                ):
                    raise ValueError(
                        "Save and successfully build the current source before publishing."
                    )
                body = {"expected_generation": state["state_generation"]}
                result = api.post(
                    f'{prefix}/versions/{build["version_id"]}/activate',
                    body,
                    key=workspace.operation(
                        "publish", {**body, "version_id": build["version_id"]}
                    ),
                )
                state["state_generation"] = result["state_generation"]
                workspace.write()
            elif args.command == "restore":
                if args.source_only and args.deployment_only:
                    raise ValueError(
                        "Choose either source-only or deployment-only restore."
                    )
                if workspace.dirty():
                    raise ValueError(
                        "Save local changes before restoring server state."
                    )
                body = {"expected_generation": state["state_generation"]}
                if not args.source_only:
                    body["deployment_only"] = args.deployment_only
                resource = "revisions" if args.source_only else "versions"
                result = api.post(
                    f"{prefix}/{resource}/{args.id}/restore",
                    body,
                    key=workspace.operation(
                        "restore", {**body, "id": args.id, "resource": resource}
                    ),
                )
                result = {
                    "restored": result,
                    "next_step": "Open the site URL into a fresh directory to edit the restored source. This workspace was not overwritten.",
                }
        print(json.dumps(result, indent=2))
        return 0
    except AuthError as exc:
        event(exc.code, message=str(exc), **exc.detail)
        return 3
    except PublishError as exc:
        print(str(exc), file=sys.stderr)
        if exc.detail:
            print(json.dumps(exc.detail), file=sys.stderr)
        return exc.exit_code
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
