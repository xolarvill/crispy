"""Maintain explicit provenance and content fingerprints for repository-local skills."""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INVENTORY = ROOT / "docs" / "local-skills-inventory.json"


def files(root: Path) -> dict[str, str]:
    def fingerprint(path: Path) -> str:
        # Avoid false changes after Git's LF/CRLF conversion on another checkout.
        content = path.read_bytes()
        if path.suffix.lower() in {".md", ".yaml", ".yml", ".json", ".py"}:
            content = content.replace(b"\r\n", b"\n")
        return hashlib.sha256(content).hexdigest()

    return {
        path.relative_to(root).as_posix(): fingerprint(path)
        for path in sorted((root / "skills").rglob("*"))
        if path.is_file()
    }


def run(mode: str, root: Path = ROOT) -> list[str]:
    inventory = root / "docs" / INVENTORY.name
    data = json.loads(inventory.read_text(encoding="utf-8"))
    errors = []
    records = data["files"]
    paths = [record["path"] for record in records]
    if len(paths) != len(set(paths)):
        errors.append("Duplicate inventory paths")
    actual = files(root)
    for path in sorted(set(actual) - set(paths)):
        errors.append(f"Unregistered file: {path}")
    for path in sorted(set(paths) - set(actual)):
        errors.append(f"Missing file: {path}")
    for record in records:
        if record["source_id"] not in data["sources"]:
            errors.append(f"Unknown source: {record['path']}")
        if record["source_id"] == "workbench_native" and record.get("upstream") is not None:
            errors.append(f"Native file has upstream identity: {record['path']}")
        if record["source_id"] != "workbench_native":
            upstream = record.get("upstream") or {}
            if not all(upstream.get(k) for k in ("repository", "path", "commit", "license")):
                errors.append(f"Incomplete upstream provenance: {record['path']}")
        if record["path"] in actual:
            if mode == "refresh":
                record["sha256"] = actual[record["path"]]
            elif record.get("sha256") != actual[record["path"]]:
                errors.append(f"Changed file: {record['path']}")
    # Refresh never silently registers files or changes their source identity.
    if mode == "refresh" and not errors:
        data["reviewed_on"] = date.today().isoformat()
        inventory.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["check", "refresh"])
    args = parser.parse_args()
    errors = run(args.mode)
    for error in errors:
        print(error)
    if not errors:
        print(f"Local skills {args.mode}: OK")
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
