#!/usr/bin/env python3
"""Regenerate the Frigate compatibility fixtures under tests/fixtures/.

Marcellus reads Frigate's SQLite file directly and calls Frigate's HTTP API,
so both are contracts it does not own. For each Frigate release tag this
writes two generated files:

* `tests/fixtures/frigate_schema/<tag>.sql` -- the database schema, built the
  way Frigate itself builds it on first boot: the upstream peewee migrations
  of that tag run against an empty SQLite file, then `sqlite_master` dumped.
* `tests/fixtures/frigate_api/<tag>.json` -- every FastAPI route under
  `frigate/api/` (method, path, handler, auth dependency) and the fields of
  every request-body / query-parameter model under `frigate/api/defs/`,
  read with `ast` (the API sources are parsed, never imported or run).

tests/test_frigate_compat.py loads both and runs Marcellus's real read paths
and HTTP calls against every version, so a release that renames a column or
moves an endpoint fails CI. See docs/frigate-compatibility.md.

Usage (needs network access to github.com and the peewee versions Frigate
pins in docker/main/requirements-wheels.txt -- install them in a scratch venv,
not the project's):

    python3 -m venv /tmp/frigate-schema-venv
    /tmp/frigate-schema-venv/bin/pip install 'peewee==3.17.*' 'peewee_migrate==1.14.*'
    /tmp/frigate-schema-venv/bin/python tools/frigate_schema_fixture.py v0.17.2 v0.18.2

The upstream migration files are downloaded into a fresh temporary directory
and executed there (they are plain peewee_migrate scripts that only touch the
empty database they are given). Nothing outside the two fixture directories is
written. After a new Frigate release, run it for the new tag and add the tag
to `FRIGATE_VERSIONS` in tests/test_frigate_compat.py.
"""

from __future__ import annotations

import argparse
import ast
import json
import sqlite3
import sys
import tempfile
import urllib.request
from pathlib import Path
from typing import Any

REPO = "blakeblackshear/frigate"
API = f"https://api.github.com/repos/{REPO}"
RAW = f"https://raw.githubusercontent.com/{REPO}"
FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
HTTP_METHODS = ("get", "post", "put", "patch", "delete")


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "marcellus-schema-fixture"})
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 -- fixed https host
        return bytes(resp.read())


def _commit_sha(tag: str) -> str:
    data = json.loads(_get(f"{API}/commits/{tag}"))
    return str(data["sha"])


def _list_py(tag: str, directory: str) -> list[str]:
    listing = json.loads(_get(f"{API}/contents/{directory}?ref={tag}"))
    return sorted(e["name"] for e in listing if e["type"] == "file" and e["name"].endswith(".py"))


# -- database schema ---------------------------------------------------------


def _fetch_db_sources(tag: str, root: Path) -> list[str]:
    """Download frigate/models.py and migrations/*.py at `tag` into `root`."""
    (root / "frigate").mkdir()
    (root / "frigate" / "__init__.py").write_text("")
    (root / "frigate" / "models.py").write_bytes(_get(f"{RAW}/{tag}/frigate/models.py"))
    names = _list_py(tag, "migrations")
    (root / "migrations").mkdir()
    for name in names:
        (root / "migrations" / name).write_bytes(_get(f"{RAW}/{tag}/migrations/{name}"))
    return names


def _run_migrations(root: Path, db_path: Path) -> None:
    try:
        from peewee_migrate import Router  # type: ignore[import-not-found]
        from playhouse.sqlite_ext import SqliteExtDatabase  # type: ignore[import-not-found]
    except ImportError:
        sys.exit("needs peewee + peewee_migrate (see the module docstring)")
    sys.path.insert(0, str(root))
    try:
        db = SqliteExtDatabase(str(db_path))
        Router(db, migrate_dir=str(root / "migrations")).run()
        db.close()
    finally:
        sys.path.remove(str(root))
        for mod in [m for m in sys.modules if m == "frigate" or m.startswith("frigate.")]:
            del sys.modules[mod]


def _dump(db_path: Path) -> list[str]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' "
            "AND tbl_name != 'migratehistory' "
            "ORDER BY CASE type WHEN 'table' THEN 0 ELSE 1 END, tbl_name, name"
        ).fetchall()
    finally:
        conn.close()
    return [f"{sql};" for _type, _name, sql in rows]


def build_schema(tag: str, sha: str, out_dir: Path) -> Path:
    from importlib.metadata import version

    with tempfile.TemporaryDirectory(prefix="frigate-schema-") as tmp:
        root = Path(tmp)
        names = _fetch_db_sources(tag, root)
        db_path = root / "frigate.db"
        _run_migrations(root, db_path)
        ddl = _dump(db_path)

    header = [
        f"-- Frigate {tag} database schema (generated -- do not hand-edit).",
        f"-- Source: github.com/{REPO} @ {tag} ({sha})",
        f"--   frigate/models.py + migrations/{names[0]} .. {names[-1]} ({len(names)} files)",
        f"-- Built by tools/frigate_schema_fixture.py: upstream migrations run with "
        f"peewee {version('peewee')} / peewee_migrate {version('peewee_migrate')}",
        "-- against an empty SQLite file, then sqlite_master dumped (migratehistory omitted).",
    ]
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{tag}.sql"
    out.write_text("\n".join(header) + "\n\n" + "\n\n".join(ddl) + "\n")
    return out


# -- HTTP API manifest -------------------------------------------------------


def _routes(filename: str, source: str) -> list[dict[str, str]]:
    out = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if not (
                isinstance(dec, ast.Call)
                and isinstance(dec.func, ast.Attribute)
                and dec.func.attr in HTTP_METHODS
                and dec.args
                and isinstance(dec.args[0], ast.Constant)
                and isinstance(dec.args[0].value, str)
            ):
                continue
            deps = [ast.unparse(k.value) for k in dec.keywords if k.arg == "dependencies"]
            out.append(
                {
                    "method": dec.func.attr.upper(),
                    "path": "/api" + dec.args[0].value,
                    "handler": f"{filename}:{node.name}",
                    "auth": deps[0] if deps else "",
                }
            )
    return out


def _models(source: str) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for node in ast.parse(source).body:
        if not isinstance(node, ast.ClassDef):
            continue
        fields = {}
        for stmt in node.body:
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                fields[stmt.target.id] = ast.unparse(stmt.annotation)
        if fields:
            out[node.name] = fields
    return out


def build_api(tag: str, sha: str, out_dir: Path) -> Path:
    routes: list[dict[str, str]] = []
    for name in _list_py(tag, "frigate/api"):
        routes += _routes(name, _get(f"{RAW}/{tag}/frigate/api/{name}").decode())
    models: dict[str, dict[str, str]] = {}
    for sub in ("frigate/api/defs/request", "frigate/api/defs/query"):
        for name in _list_py(tag, sub):
            models.update(_models(_get(f"{RAW}/{tag}/{sub}/{name}").decode()))
    manifest: dict[str, Any] = {
        "_generated": (
            "by tools/frigate_schema_fixture.py from frigate/api/*.py and "
            "frigate/api/defs/{request,query}/*.py (ast, not executed) -- do not hand-edit"
        ),
        "tag": tag,
        "source": f"github.com/{REPO} @ {tag} ({sha})",
        "routes": sorted(routes, key=lambda r: (r["path"], r["method"])),
        "models": dict(sorted(models.items())),
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{tag}.json"
    out.write_text(json.dumps(manifest, indent=1) + "\n")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("tags", nargs="+", help="Frigate release tags, e.g. v0.17.2 v0.18.2")
    ap.add_argument("--fixtures", type=Path, default=FIXTURES)
    args = ap.parse_args()
    for tag in args.tags:
        sha = _commit_sha(tag)
        print(build_schema(tag, sha, args.fixtures / "frigate_schema"))
        print(build_api(tag, sha, args.fixtures / "frigate_api"))


if __name__ == "__main__":
    main()
