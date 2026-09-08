#!/usr/bin/env python3
"""Standalone Codev records v1 validator and PostgreSQL importer.

Validation uses only the Python standard library.
Import additionally requires asyncpg and a destination DSN in an environment variable.
No Codev service, account, platform key, or provider credential is required.
"""

import argparse
import asyncio
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
import zipfile
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

FORMAT = "codev.records"
FILES = {
    "manifest.json",
    "schemas.json",
    "backend.json",
    "owners.jsonl",
    "records.jsonl",
    "history.jsonl",
}
NAME = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
LINE_LIMIT = 131072


class ArchiveError(ValueError):
    """Safe diagnostic: never contains record contents or credentials."""


def require(condition, message="The archive is invalid."):
    if not condition:
        raise ArchiveError(message)


def unique_object(pairs):
    result = {}
    for name, value in pairs:
        require(name not in result, "The archive contains duplicate JSON keys.")
        result[name] = value
    return result


def read_json(raw):
    try:
        return json.loads(
            raw,
            parse_float=Decimal,
            parse_constant=lambda _: require(False),
            object_pairs_hook=unique_object,
        )
    except (ValueError, UnicodeError, RecursionError):
        raise ArchiveError("The archive contains invalid JSON.") from None


def timestamp(value, *, nullable=False):
    if value is None and nullable:
        return None
    require(
        isinstance(value, str) and value.endswith("Z"),
        "Archive timestamps must use UTC.",
    )
    try:
        result = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
        require(result.utcoffset().total_seconds() == 0)
        return result
    except (ValueError, TypeError):
        raise ArchiveError("The archive contains an invalid timestamp.") from None


def identifier(value):
    try:
        require(isinstance(value, str) and str(UUID(value)) == value)
        return value
    except ValueError:
        raise ArchiveError("The archive contains an invalid identifier.") from None


def positive(value):
    require(
        type(value) is int and 1 <= value <= 9223372036854775807,
        "The archive contains an invalid revision.",
    )


def validate_data(value, depth=0):
    require(depth <= 8, "A record exceeds the supported nesting depth.")
    if isinstance(value, dict):
        require(
            len(value) <= 100
            and all(isinstance(key, str) and "\0" not in key for key in value)
        )
        for item in value.values():
            validate_data(item, depth + 1)
    elif isinstance(value, list):
        require(len(value) <= 1000)
        for item in value:
            validate_data(item, depth + 1)
    elif isinstance(value, str):
        require(
            "\0" not in value
            and not any(0xD800 <= ord(char) <= 0xDFFF for char in value)
        )
    elif isinstance(value, Decimal):
        require(value.is_finite())
    else:
        require(value is None or type(value) in {int, bool})


def lines(archive, name):
    with archive.open(name) as source:
        while raw := source.readline(LINE_LIMIT + 1):
            require(
                len(raw) <= LINE_LIMIT and raw.endswith(b"\n"),
                "The archive contains an oversized or incomplete record.",
            )
            value = read_json(raw)
            require(isinstance(value, dict))
            yield value, raw.decode("utf-8")


def validate_record(item, schemas, index):
    require(
        set(item)
        == {
            "id",
            "collection",
            "owner_id",
            "data",
            "revision",
            "schema_revision",
            "created_at",
            "updated_at",
            "deleted_at",
        }
    )
    identifier(item["id"])
    require(item["collection"] in schemas, "A record references a missing collection.")
    if item["owner_id"] is not None:
        identifier(item["owner_id"])
        require(
            index.execute(
                "SELECT 1 FROM owners WHERE id=?", (item["owner_id"],)
            ).fetchone(),
            "A record references a missing owner.",
        )
    positive(item["revision"])
    positive(item["schema_revision"])
    require(isinstance(item["data"], dict))
    validate_data(item["data"])
    created, updated = timestamp(item["created_at"]), timestamp(item["updated_at"])
    require(created <= updated, "A record's timestamps are inconsistent.")
    timestamp(item["deleted_at"], nullable=True)


def validate_archive(path, max_bytes=1073741824):
    """Verify checksums, bounds, identities, references and exact JSON types."""
    try:
        with (
            zipfile.ZipFile(path) as archive,
            tempfile.TemporaryDirectory(prefix="codev-import-index-") as directory,
        ):
            infos = archive.infolist()
            names = [item.filename for item in infos]
            require(
                len(names) == len(set(names))
                and not set(names) - FILES
                and FILES - {"history.jsonl"} <= set(names)
            )
            require(
                sum(item.file_size for item in infos) <= max_bytes,
                "The archive exceeds the configured size limit.",
            )
            require(
                not any(item.flag_bits & 1 for item in infos),
                "Download the portable, decrypted export first.",
            )
            for name, limit in (
                ("manifest.json", 65536),
                ("schemas.json", 1048576),
                ("backend.json", 65536),
            ):
                require(archive.getinfo(name).file_size <= limit)
            manifest = read_json(archive.read("manifest.json"))
            require(
                manifest["format"] == FORMAT
                and type(manifest["version"]) is int
                and manifest["version"] == 1,
                "Unsupported archive format version.",
            )
            require(set(manifest["files"]) == set(names) - {"manifest.json"})
            require(
                type(manifest["includes_history"]) is bool
                and manifest["includes_history"] == ("history.jsonl" in names)
            )
            identifier(manifest["site_id"])
            if manifest["app_version_id"]:
                identifier(manifest["app_version_id"])
            require(
                manifest["environment"] in {"production", "preview"}
                or re.fullmatch(r"recovery_[a-f0-9]{32}", manifest["environment"])
            )
            require(
                type(manifest["data_revision"]) is int
                and 0 <= manifest["data_revision"] <= 9223372036854775807
            )
            timestamp(manifest["captured_at"])
            for name, expected in manifest["files"].items():
                checksum, size, count = hashlib.sha256(), 0, 0
                with archive.open(name) as source:
                    while chunk := source.read(1048576):
                        checksum.update(chunk)
                        size += len(chunk)
                        count += chunk.count(b"\n")
                require(
                    size == expected["bytes"]
                    and checksum.hexdigest() == expected["sha256"],
                    "An archive checksum does not match.",
                )
                if name.endswith(".jsonl"):
                    require(
                        count == expected["records"],
                        "An archive record count does not match.",
                    )
            schemas = read_json(archive.read("schemas.json"))
            require(isinstance(schemas, list) and len(schemas) <= 20)
            registry = {}
            for schema in schemas:
                require(
                    set(schema)
                    == {
                        "name",
                        "definition",
                        "schema_revision",
                        "retired_fields",
                        "retired_at",
                    }
                )
                name = schema["name"]
                require(
                    isinstance(name, str)
                    and NAME.fullmatch(name)
                    and name not in registry
                )
                positive(schema["schema_revision"])
                timestamp(schema["retired_at"], nullable=True)
                definition = schema["definition"]
                require(
                    definition["access"]
                    in {"private", "shared", "owner", "public_read", "public_submit"}
                )
                require(
                    isinstance(definition["fields"], dict)
                    and len(definition["fields"]) <= 50
                )
                require(all(NAME.fullmatch(field) for field in definition["fields"]))
                require(
                    isinstance(schema["retired_fields"], list)
                    and set(schema["retired_fields"]) <= set(definition["fields"])
                )
                registry[name] = schema
            backend = read_json(archive.read("backend.json"))
            require(
                backend.get("version") == 1
                and set(backend)
                <= {"version", "collections", "routes", "public_variables"}
            )
            index = sqlite3.connect(Path(directory) / "identities.sqlite")
            try:
                index.executescript(
                    "CREATE TABLE owners(id TEXT PRIMARY KEY); CREATE TABLE records(id TEXT PRIMARY KEY, collection TEXT, owner_id TEXT, revision INTEGER); CREATE TABLE history(id TEXT, revision INTEGER, PRIMARY KEY(id, revision));"
                )
                for owner, _ in lines(archive, "owners.jsonl"):
                    require(set(owner) == {"id", "created_at"})
                    identifier(owner["id"])
                    timestamp(owner["created_at"])
                    index.execute("INSERT INTO owners VALUES (?)", (owner["id"],))
                for item, _ in lines(archive, "records.jsonl"):
                    validate_record(item, registry, index)
                    index.execute(
                        "INSERT INTO records VALUES (?, ?, ?, ?)",
                        (
                            item["id"],
                            item["collection"],
                            item["owner_id"],
                            item["revision"],
                        ),
                    )
                if manifest["includes_history"]:
                    for history, _ in lines(archive, "history.jsonl"):
                        require(
                            set(history)
                            == {"record_id", "snapshot", "operation", "expires_at"}
                        )
                        item = history["snapshot"]
                        validate_record(item, registry, index)
                        require(
                            history["record_id"] == item["id"]
                            and history["operation"]
                            in {
                                "create",
                                "update",
                                "delete",
                                "restore",
                                "copy",
                                "bulk_restore",
                            }
                        )
                        current = index.execute(
                            "SELECT collection, owner_id, revision FROM records WHERE id=?",
                            (item["id"],),
                        ).fetchone()
                        require(
                            current
                            and current[:2] == (item["collection"], item["owner_id"])
                            and current[2] >= item["revision"],
                            "A historical record does not match its current identity.",
                        )
                        timestamp(history["expires_at"])
                        index.execute(
                            "INSERT INTO history VALUES (?, ?)",
                            (item["id"], item["revision"]),
                        )
            finally:
                index.close()
            return manifest
    except ArchiveError:
        raise
    except (
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        zipfile.BadZipFile,
        sqlite3.Error,
        OSError,
        RuntimeError,
    ):
        raise ArchiveError(
            "The archive failed format or integrity verification."
        ) from None


async def import_archive(
    path, dsn, schema, *, owner_mapping=None, allow_unmapped=False, max_bytes=1073741824
):
    import asyncpg

    require(
        re.fullmatch(r"[a-z][a-z0-9_]{0,47}", schema)
        and schema not in {"public", "app_runtime", "information_schema"}
        and not schema.startswith("pg_"),
        "Choose a new, dedicated destination schema name.",
    )
    owner_mapping = owner_mapping or {}
    require(
        isinstance(owner_mapping, dict)
        and all(
            isinstance(value, str) and 1 <= len(value) <= 512 and "\0" not in value
            for value in owner_mapping.values()
        ),
        "Owner mappings must map app owner IDs to destination subject strings.",
    )
    require(
        len(set(owner_mapping.values())) == len(owner_mapping),
        "Destination subjects must be unique.",
    )
    for owner in owner_mapping:
        identifier(owner)
    mapping_hash = hashlib.sha256(
        json.dumps(
            {"mapping": owner_mapping, "allow_unmapped": allow_unmapped}, sort_keys=True
        ).encode()
    ).hexdigest()
    with tempfile.TemporaryDirectory(prefix="codev-import-") as directory:
        package = Path(directory) / "package.zip"
        checksum, size = hashlib.sha256(), 0
        with open(path, "rb") as source, package.open("wb") as output:
            while chunk := source.read(1048576):
                size += len(chunk)
                require(
                    size <= max_bytes + 1048576,
                    "The input exceeds the configured archive size limit.",
                )
                checksum.update(chunk)
                output.write(chunk)
        manifest = validate_archive(package, max_bytes)
        with zipfile.ZipFile(package) as archive:
            owners = {owner["id"] for owner, _ in lines(archive, "owners.jsonl")}
            require(
                set(owner_mapping) <= owners,
                "A mapping refers to an owner outside this archive.",
            )
            require(
                allow_unmapped or owners <= set(owner_mapping),
                "Supply every owner's destination mapping, or explicitly choose --unmapped-owners deny.",
            )
            connection = await asyncpg.connect(dsn, command_timeout=60)
            try:
                async with connection.transaction():
                    await connection.execute("SET LOCAL lock_timeout='5s'")
                    await connection.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                        "codev-import:" + schema,
                    )
                    exists = await connection.fetchval(
                        "SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname=$1)",
                        schema,
                    )
                    namespace = '"' + schema + '"'
                    if exists:
                        marker = await connection.fetchrow(
                            f"SELECT archive_sha256, mapping_sha256 FROM {namespace}.import_metadata"
                        )
                        require(
                            marker
                            and tuple(marker) == (checksum.hexdigest(), mapping_hash),
                            "The destination already exists with a different import. Choose a new schema.",
                        )
                        return {
                            "status": "already_imported",
                            "schema": schema,
                            "records": manifest["files"]["records.jsonl"]["records"],
                        }
                    await connection.execute(
                        f"CREATE SCHEMA {namespace}; REVOKE ALL ON SCHEMA {namespace} FROM PUBLIC; SET LOCAL search_path TO {namespace}, pg_catalog"
                    )
                    await connection.execute("""
                        CREATE TABLE import_metadata (archive_sha256 text NOT NULL, mapping_sha256 text NOT NULL, manifest jsonb NOT NULL, backend jsonb NOT NULL, imported_at timestamptz NOT NULL DEFAULT now());
                        CREATE TABLE collections (name text PRIMARY KEY, definition jsonb NOT NULL, schema_revision bigint NOT NULL, retired_fields jsonb NOT NULL, retired_at timestamptz);
                        CREATE TABLE owners (id uuid PRIMARY KEY, created_at timestamptz NOT NULL, destination_subject text UNIQUE);
                        CREATE TABLE records (id uuid PRIMARY KEY, collection text NOT NULL REFERENCES collections(name), owner_id uuid REFERENCES owners(id), data jsonb NOT NULL, revision bigint NOT NULL, schema_revision bigint NOT NULL, created_at timestamptz NOT NULL, updated_at timestamptz NOT NULL, deleted_at timestamptz);
                        CREATE TABLE history (record_id uuid REFERENCES records(id), revision bigint NOT NULL, snapshot jsonb NOT NULL, operation text NOT NULL, expires_at timestamptz NOT NULL, PRIMARY KEY(record_id, revision));
                        CREATE INDEX records_page ON records(collection, created_at, id);
                        CREATE INDEX records_owner ON records(collection, owner_id);
                    """)
                    await connection.execute(
                        "INSERT INTO import_metadata VALUES ($1, $2, $3::jsonb, $4::jsonb, now())",
                        checksum.hexdigest(),
                        mapping_hash,
                        archive.read("manifest.json").decode(),
                        archive.read("backend.json").decode(),
                    )
                    await connection.execute(
                        "INSERT INTO collections SELECT value->>'name', value->'definition', (value->>'schema_revision')::bigint, value->'retired_fields', (value->>'retired_at')::timestamptz FROM jsonb_array_elements($1::jsonb)",
                        archive.read("schemas.json").decode(),
                    )
                    for owner, _ in lines(archive, "owners.jsonl"):
                        await connection.execute(
                            "INSERT INTO owners VALUES ($1, $2, $3)",
                            UUID(owner["id"]),
                            timestamp(owner["created_at"]),
                            owner_mapping.get(owner["id"]),
                        )
                    statements = {
                        "records.jsonl": "INSERT INTO records SELECT (j->>'id')::uuid, j->>'collection', (j->>'owner_id')::uuid, j->'data', (j->>'revision')::bigint, (j->>'schema_revision')::bigint, (j->>'created_at')::timestamptz, (j->>'updated_at')::timestamptz, (j->>'deleted_at')::timestamptz FROM (SELECT $1::jsonb AS j) input",
                        "history.jsonl": "INSERT INTO history SELECT (j->>'record_id')::uuid, (j->'snapshot'->>'revision')::bigint, j->'snapshot', j->>'operation', (j->>'expires_at')::timestamptz FROM (SELECT $1::jsonb AS j) input",
                    }
                    for name, statement in statements.items():
                        if name not in archive.namelist():
                            continue
                        batch = []
                        for _, raw in lines(archive, name):
                            batch.append((raw,))
                            if len(batch) == 200:
                                await connection.executemany(statement, batch)
                                batch = []
                        if batch:
                            await connection.executemany(statement, batch)
                    await connection.execute(
                        f"REVOKE ALL ON ALL TABLES IN SCHEMA {namespace} FROM PUBLIC"
                    )
                    for table in ("records", "history", "owners", "collections"):
                        await connection.execute(
                            f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY; ALTER TABLE {table} FORCE ROW LEVEL SECURITY"
                        )
                    return {
                        "status": "imported",
                        "schema": schema,
                        "records": manifest["files"]["records.jsonl"]["records"],
                        "authorization": "deny until destination grants and RLS policies are explicitly configured",
                    }
            finally:
                await connection.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--schema")
    parser.add_argument("--database-env", default="CODEV_IMPORT_DATABASE_URL")
    parser.add_argument("--owner-map", type=Path)
    parser.add_argument("--unmapped-owners", choices=["error", "deny"], default="error")
    parser.add_argument("--max-bytes", type=int, default=1073741824)
    args = parser.parse_args()
    try:
        require(
            1024 <= args.max_bytes <= 17179869184,
            "Choose a bounded archive size between 1 KiB and 16 GiB.",
        )
        if args.validate_only:
            result = validate_archive(args.archive, args.max_bytes)
            print(
                json.dumps(
                    {
                        "status": "valid",
                        "format": result["format"],
                        "version": result["version"],
                        "records": result["files"]["records.jsonl"]["records"],
                    }
                )
            )
        else:
            require(
                args.schema and os.environ.get(args.database_env),
                "Set the destination DSN environment variable and supply --schema.",
            )
            mapping = read_json(args.owner_map.read_bytes()) if args.owner_map else {}
            result = asyncio.run(
                import_archive(
                    args.archive,
                    os.environ[args.database_env],
                    args.schema,
                    owner_mapping=mapping,
                    allow_unmapped=args.unmapped_owners == "deny",
                    max_bytes=args.max_bytes,
                )
            )
            print(json.dumps(result))
    except ArchiveError as error:
        print(str(error), file=sys.stderr)
        return 1
    except ImportError:
        print(
            "Install asyncpg in this Python environment to import into PostgreSQL.",
            file=sys.stderr,
        )
        return 1
    except Exception:
        print(
            "Import failed; the destination transaction was rolled back. Check database access and choose a new dedicated schema. No credentials or record values are included in this diagnostic.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
