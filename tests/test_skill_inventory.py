"""Provenance refresh must not silently adopt files or hide missing materials."""
import json
from pathlib import Path

import pytest

from scripts.skill_inventory import run


@pytest.fixture
def package(tmp_path: Path) -> Path:
    (tmp_path / "skills" / "example").mkdir(parents=True)
    (tmp_path / "docs").mkdir()
    (tmp_path / "skills" / "example" / "SKILL.md").write_bytes(b"method\n")
    data = {"sources": {"workbench_native": {}}, "files": [
        {"path": "skills/example/SKILL.md", "source_id": "workbench_native", "upstream": None}
    ]}
    (tmp_path / "docs" / "local-skills-inventory.json").write_text(json.dumps(data))
    assert run("refresh", tmp_path) == []
    return tmp_path


def test_changes_detected_and_line_endings_portable(package: Path):
    skill = package / "skills" / "example" / "SKILL.md"
    skill.write_bytes(b"method\r\n")
    assert run("check", package) == []
    skill.write_bytes(b"different method\n")
    assert run("check", package) == ["Changed file: skills/example/SKILL.md"]
    assert run("refresh", package) == []
    assert run("check", package) == []


@pytest.mark.parametrize("change, message", [
    ("new", "Unregistered file:"),
    ("missing", "Missing file:"),
    ("identity", "Unknown source:"),
    ("upstream", "Incomplete upstream provenance:"),
])
def test_refresh_refuses_unreviewed_inventory_changes(package: Path, change: str, message: str):
    inventory = package / "docs" / "local-skills-inventory.json"
    if change == "new":
        (package / "skills" / "unexpected.md").write_text("new")
    elif change == "missing":
        (package / "skills" / "example" / "SKILL.md").unlink()
    else:
        data = json.loads(inventory.read_text())
        data["files"][0]["source_id"] = "vendor"
        if change == "upstream":
            data["sources"]["vendor"] = {}
        inventory.write_text(json.dumps(data))
    before = inventory.read_bytes()
    assert any(error.startswith(message) for error in run("refresh", package))
    assert inventory.read_bytes() == before
