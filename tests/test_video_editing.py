"""Timeline authority: media pinning, atomic edits, history and concurrency."""
import copy
import uuid

import pytest
from PIL import Image
from app.core.config import get_settings
from app.schemas.video import VideoDocument, VideoOperation
from app.services.video_editing import apply_operations


def make_project(client):
    run = client.post("/runs", json={"workspace_name": "video-tests", "project_name": "video",
        "product_name": "Video", "product_code": "video-test", "industry_code": "apparel",
        "campaign_name": "video", "creative_preset": "meta_square_5s", "pipeline_mode": "copy_image_only"})
    assert run.status_code == 200, run.text
    response = client.post(f"/runs/{run.json()['id']}/video-projects", json={"title": "Video", "request_id": "create-1"})
    assert response.status_code == 200, response.text
    return response.json()


def upload_image(client, project, tmp_path):
    file = tmp_path / "source.png"
    Image.new("RGB", (160, 90), "red").save(file)
    response = client.post(f"/video-projects/{project['id']}/sources", files={"file": ("source.png", file.read_bytes(), "image/png")},
        data={"expected_revision": project["revision"], "request_id": "import-1"})
    assert response.status_code == 200, response.text
    return response.json(), file


def clip(source_id, **changes):
    return {"id": "clip-1", "kind": "image", "source_id": source_id,
            "duration_us": 4_000_000, **changes}


def save(client, project, document, request="save-1"):
    return client.put(f"/video-projects/{project['id']}", json={"document": document,
        "expected_revision": project["revision"], "request_id": request, "summary": "Edit"})


def test_pinning_cas_idempotency_and_history(client, tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "assets_dir", tmp_path / "assets")
    project, original = upload_image(client, make_project(client), tmp_path)
    pinned = project["sources"][0]
    original.write_bytes(b"changed outside editor")
    assert __import__('pathlib').Path(pinned["uri"]).read_bytes() != original.read_bytes()
    doc = project["document"]
    doc["tracks"] = [{"id": "track-1", "name": "Picture", "clips": [clip(pinned["id"])]}]
    accepted = save(client, project, doc)
    assert accepted.status_code == 200
    assert save(client, project, doc).status_code == 200  # same request is replayed
    stale = save(client, project, doc, "different-request")
    assert stale.status_code == 409
    changed = copy.deepcopy(doc)
    changed["width"] = 640
    assert save(client, project, changed).status_code == 409  # same key, changed payload
    current = accepted.json()
    restored = client.post(f"/video-projects/{project['id']}/restore", json={"expected_revision": current["revision"],
        "request_id": "restore-1", "revision": 0})
    assert restored.status_code == 200
    assert restored.json()["document"]["tracks"] == []
    assert restored.json()["revision"] > current["revision"]
    assert len(client.get(f"/video-projects/{project['id']}/history").json()) == 4


def test_split_preserves_source_inpoint_and_atomic_validation():
    doc = VideoDocument.model_validate({"tracks": [{"id": "v", "clips": [clip("s", kind="video", source_in_us=2_000_000)]}]})
    result = apply_operations(doc, [VideoOperation(op="split_clip", clip_id="clip-1", new_id="right", at_us=1_000_000)])
    left, right = result.tracks[0].clips
    assert left.duration_us == 1_000_000
    assert (right.start_us, right.source_in_us, right.duration_us) == (1_000_000, 3_000_000, 3_000_000)
    assert doc.tracks[0].clips[0].duration_us == 4_000_000
    with pytest.raises(ValueError):
        apply_operations(doc, [VideoOperation(op="split_clip", clip_id="clip-1", new_id="bad", at_us=0)])


def test_unknown_sources_and_overlap_are_rejected(client):
    project = make_project(client)
    doc = project["document"]
    doc["tracks"] = [{"id": "a", "clips": [clip("unknown")]}]
    assert save(client, project, doc).status_code == 422
    doc["tracks"][0]["clips"].append(clip("unknown", id="two"))
    assert save(client, project, doc).status_code == 422
    assert client.get(f"/video-projects/{project['id']}").json()["revision"] == 0


def test_create_is_idempotent_and_run_source_is_scoped(client, tmp_path):
    project = make_project(client)
    again = client.post(f"/runs/{project['run_id']}/video-projects", json={"title": "Video", "request_id": "create-1"})
    assert again.json()["id"] == project["id"]
    denied = client.post(f"/video-projects/{project['id']}/sources/from-run", json={"expected_revision": 0,
        "request_id": str(uuid.uuid4()), "uri": str(tmp_path / "unregistered.mp4")})
    assert denied.status_code == 422
