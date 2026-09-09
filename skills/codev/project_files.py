"""Portable source-tree rules shared by the API and standalone client.

This module deliberately has no application imports or third-party
dependencies: the installer ships it beside project.py.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import stat
import unicodedata
from pathlib import Path

EXCLUDED_DIRECTORIES = frozenset(
    {
        ".git",
        ".codev",
        "node_modules",
        ".vite",
        ".cache",
        ".next",
        "__pycache__",
        ".claude",
        ".codex",
        ".agents",
    }
)
EXCLUDED_FILES = frozenset(
    {
        ".ds_store",
        ".npmrc",
        ".yarnrc.yml",
        ".netrc",
        ".pypirc",
        "credentials.json",
        "service-account.json",
        "id_rsa",
        "id_ed25519",
        "codev.backend.json",
    }
)
WINDOWS_RESERVED = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"{prefix}{number}" for prefix in ("com", "lpt") for number in range(1, 10)}
)


def validate_path(path: str) -> str:
    """Accept one canonical relative path that is safe on supported hosts."""
    if not path or len(path.encode("utf-8")) > 1024:
        raise ValueError("Paths must contain between 1 and 1024 UTF-8 bytes.")
    if path.startswith("/") or "\\" in path:
        raise ValueError("Paths must be relative and use forward slashes.")
    for part in path.split("/"):
        if part in ("", ".", "..") or part.endswith((".", " ")):
            raise ValueError("Paths cannot contain empty or traversal segments.")
        if re.search(r'[\x00-\x1f\x7f<>:"|?*]', part):
            raise ValueError("A path contains an unsupported character.")
        if part.split(".", 1)[0].casefold() in WINDOWS_RESERVED:
            raise ValueError("A path contains a reserved device name.")
    return path


def portable_key(path: str) -> str:
    return unicodedata.normalize("NFC", path).casefold()


def canonical_path(path: str) -> str:
    return unicodedata.normalize("NFC", path)


def local_source_path(root: Path, path: str) -> Path:
    """Resolve canonical manifest names on filesystems that decompose Unicode."""
    current = root
    for part in path.split("/"):
        direct = current / part
        if direct.exists():
            current = direct
        else:
            matches = [
                child
                for child in current.iterdir()
                if canonical_path(child.name) == part
            ]
            if len(matches) != 1:
                raise ValueError(f"Source path is missing or ambiguous: {path}")
            current = matches[0]
        if current.is_symlink():
            raise ValueError(f"Source symlinks are not supported: {path}")
    return current


def validate_tree(paths: list[str]) -> None:
    seen: dict[str, str] = {}
    directories: dict[str, str] = {}
    for path in sorted(paths):
        validate_path(path)
        key = portable_key(path)
        if key in seen or key in directories:
            raise ValueError(f"Conflicting source path: {path}")
        pieces = key.split("/")
        original_pieces = path.split("/")
        for length in range(1, len(pieces)):
            parent = "/".join(pieces[:length])
            if parent in seen:
                raise ValueError(f"A file is also a parent directory: {path}")
            original_parent = "/".join(original_pieces[:length])
            if parent in directories and directories[parent] != original_parent:
                raise ValueError(f"Conflicting directory spelling: {path}")
            directories[parent] = original_parent
        seen[key] = path


def always_excluded(path: str, output_directory: str) -> bool:
    path, output_directory = path.casefold(), output_directory.casefold()
    parts = path.split("/")
    if any(part in EXCLUDED_DIRECTORIES for part in parts):
        return True
    if path == output_directory or path.startswith(output_directory + "/"):
        return True
    return sensitive_path(path)


def sensitive_path(path: str) -> bool:
    parts = path.casefold().split("/")
    name = parts[-1]
    return (
        name in EXCLUDED_FILES
        or (name.startswith(".env") and name != ".env.example")
        or (name.startswith(".dev.vars") and name != ".dev.vars.example")
        or name.endswith((".pem", ".key", ".p12", ".pfx"))
        or any(
            part in {".git", ".codev", ".aws", ".ssh", ".claude", ".codex", ".agents"}
            for part in parts
        )
    )


def read_ignore(root: Path) -> list[str]:
    path = root / ".codevignore"
    if not path.exists():
        return []
    if path.is_symlink() or path.stat().st_size > 65536:
        raise ValueError(".codevignore must be a regular file of at most 64 KiB.")
    return [
        canonical_path(line.strip())
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def matches_ignore(path: str, patterns: list[str]) -> bool:
    """Ordered shell patterns; a leading ! re-includes nonprotected paths."""
    ignored = False
    for raw in patterns:
        negate = raw.startswith("!")
        pattern = raw[1:] if negate else raw
        pattern = pattern.lstrip("/").rstrip("/")
        candidates = [path, *path.split("/")] if "/" not in pattern else [path]
        if path.startswith(pattern + "/") or any(
            fnmatch.fnmatchcase(candidate, pattern) for candidate in candidates
        ):
            ignored = not negate
    return ignored


def collect_files(root: Path, output_directory: str = "dist") -> list[dict]:
    """Hash a deterministic tree without following symlinks or storing secrets."""
    validate_path(output_directory)
    patterns = read_ignore(root)
    result: list[dict] = []
    for current, directories, names in os.walk(root, followlinks=False):
        retained: list[str] = []
        for name in sorted(directories):
            full = Path(current) / name
            relative = full.relative_to(root).as_posix()
            if always_excluded(relative, output_directory):
                continue
            if full.is_symlink():
                raise ValueError(f"Source symlinks are not supported: {relative}")
            # Keep user-ignored directories traversable so ! rules work.
            retained.append(name)
        directories[:] = retained
        for name in sorted(names):
            full = Path(current) / name
            relative = full.relative_to(root).as_posix()
            if always_excluded(relative, output_directory) or matches_ignore(
                canonical_path(relative), patterns
            ):
                continue
            info = full.lstat()
            if not stat.S_ISREG(info.st_mode):
                raise ValueError(f"Only regular source files are supported: {relative}")
            hasher = hashlib.sha256()
            with full.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    hasher.update(chunk)
            result.append(
                {
                    "path": relative,
                    "sha256": hasher.hexdigest(),
                    "size": info.st_size,
                    "executable": bool(info.st_mode & stat.S_IXUSR),
                }
            )
    validate_tree([file["path"] for file in result])
    for file in result:
        file["path"] = canonical_path(file["path"])
    return sorted(result, key=lambda file: file["path"])


def content_digest(
    files: list[dict], build_config: dict, backend_definition: dict | None = None
) -> str:
    canonical = {
        "files": sorted(files, key=lambda file: file["path"]),
        "build_config": build_config,
    }
    if backend_definition is not None:
        canonical["backend_definition"] = backend_definition
    return hashlib.sha256(
        json.dumps(
            canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
    ).hexdigest()


def read_backend(directory: Path) -> dict | None:
    path = directory / "codev.backend.json"
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink() or not path.is_file():
        raise ValueError("Backend configuration must be a regular local file.")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise ValueError("Backend configuration exceeds 64 KiB.")
    try:
        value = json.loads(raw)
    except ValueError:
        raise ValueError("Backend configuration must contain valid JSON.") from None
    if not isinstance(value, dict) or set(value) - {
        "version",
        "collections",
        "routes",
        "public_variables",
    }:
        raise ValueError(
            "Backend configuration contains unsupported properties. Enter secret values through backend.py secret-set."
        )
    return value


def validate_backend(origin: str, definition: dict | None):
    """Use the server's exact contract before connecting or creating an app."""
    if definition is None:
        return None
    from auth import AuthError, request

    try:
        return request(origin, "/v1/backend/validate", definition)
    except AuthError as error:
        if error.detail.get("status") == 404:
            raise AuthError(
                "backend_validation_unavailable",
                "This Codev server needs the backend validation endpoint. Update it before configuring this app.",
            ) from None
        raise
