"""Build an exact saved source snapshot locally, with a leased build record."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from uuid import uuid4

from project_api import upload_verified
from project_workspace import materialize, output_files


def tool_version(command: str) -> str:
    result = subprocess.run(
        [command, "--version"], check=True, capture_output=True, text=True, timeout=15
    )
    return result.stdout.strip().removeprefix("v")


def run_build(api, workspace, *, trust: bool) -> dict:
    if not trust:
        raise ValueError(
            "Builds execute project code on this computer. Review the source, then pass --trust to allow dependency installation and build scripts."
        )
    state = workspace.require()
    if workspace.dirty() or not state.get("revision_id"):
        raise ValueError("Save your current changes before building.")
    config = state["build_config"]
    manager = config["package_manager"]
    node_version, manager_version = tool_version("node"), tool_version(manager)
    if (
        node_version != config["node_version"]
        or manager_version != config["package_manager_version"]
    ):
        raise ValueError(
            "Node or package-manager version differs from the saved build configuration. Use the recorded versions, or save updated build configuration first."
        )
    prefix = f'/v1/sites/{state["site_id"]}'
    manifest = api.get(f'{prefix}/revisions/{state["revision_id"]}')
    attempt = api.post(
        prefix + "/builds",
        {
            "revision_id": state["revision_id"],
            "node_version": node_version,
            "package_manager_version": manager_version,
        },
        key=str(uuid4()),
    )
    build_path = f'{prefix}/builds/{attempt["id"]}'
    stop = threading.Event()

    def heartbeat():
        while not stop.wait(30):
            try:
                api.post(build_path + "/heartbeat", {})
            except Exception:
                # Completion is still checked against the authoritative lease.
                pass

    worker = threading.Thread(target=heartbeat, daemon=True)
    worker.start()
    try:
        with tempfile.TemporaryDirectory(prefix="codev-build-") as temporary:
            root = Path(temporary) / "project"
            materialize(api, manifest, root)
            environment = {
                name: os.environ[name]
                for name in ("PATH", "SYSTEMROOT", "TMPDIR", "TEMP", "LANG")
                if name in os.environ
            }
            environment.update(config["environment"])
            environment.update({"CI": "true", "NODE_ENV": "production"})
            # Build tools are commonly devDependencies, so install them too.
            install = {
                "npm": ["npm", "ci", "--include=dev", "--ignore-scripts"],
                "pnpm": [
                    "pnpm",
                    "install",
                    "--frozen-lockfile",
                    "--prod=false",
                    "--ignore-scripts",
                ],
                "yarn": (
                    [
                        "yarn",
                        "install",
                        "--frozen-lockfile",
                        "--production=false",
                        "--ignore-scripts",
                    ]
                    if manager_version.startswith("1.")
                    else ["yarn", "install", "--immutable", "--mode=skip-builds"]
                ),
            }[manager]
            subprocess.run(
                install,
                cwd=root,
                env=environment,
                check=True,
                timeout=900,
                stdout=sys.stderr,
            )
            subprocess.run(
                [manager, "run", config["build_script"]],
                cwd=root,
                env=environment,
                check=True,
                timeout=900,
                stdout=sys.stderr,
            )
            output = root / config["output_directory"]
            if output.is_symlink() or not output.resolve().is_relative_to(
                root.resolve()
            ):
                raise ValueError(
                    "Build output must stay inside the isolated workspace."
                )
            files = output_files(output)
            prepared = api.post(
                prefix + "/versions",
                {
                    "source_revision_id": state["revision_id"],
                    "build_attempt_id": attempt["id"],
                    "files": files,
                    "spa_mode": True,
                },
                key=str(uuid4()),
            )
            upload_verified(prepared["uploads"], files, output)
            api.post(
                f'/v1/versions/{prepared["version_id"]}/finalize', {"activate": False}
            )
            result = api.post(
                build_path + "/complete",
                {
                    "status": "succeeded",
                    "output_version_id": prepared["version_id"],
                },
                key=str(uuid4()),
            )
            state["build"] = {
                "revision_id": state["revision_id"],
                "version_id": prepared["version_id"],
                "preview_url": prepared["preview_url"],
            }
            workspace.write()
            return {**result, "preview_url": prepared["preview_url"]}
    except Exception as exc:
        try:
            # Do not upload logs, file paths, environment, or error text that
            # could contain secrets. The detailed error remains local.
            api.post(
                build_path + "/complete",
                {
                    "status": "failed",
                    "diagnostic": f"Local build failed ({type(exc).__name__}). See the local terminal for details.",
                },
                key=str(uuid4()),
            )
        except Exception:
            pass
        raise
    finally:
        stop.set()
        worker.join(timeout=1)
