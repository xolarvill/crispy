import base64
import io

from PIL import Image

from app.data.models import RunVariant, VariantAsset
from app.data.session import SessionLocal


def png(colour=(198, 122, 84)) -> str:
    image = Image.new("RGB", (64, 64), colour)
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()


def create_run(client, workspace: str, code: str) -> str:
    response = client.post("/runs", json={
        "workspace_name": workspace, "project_name": "studio-project",
        "product_name": code, "product_code": code, "industry_code": "apparel",
        "campaign_name": "studio-campaign", "creative_preset": "meta_square_5s",
        "pipeline_mode": "copy_image_only",
    })
    assert response.status_code == 200
    return response.json()["id"]


def seed_asset(run_id: str, uri: str = "artifacts/hero.png") -> None:
    with SessionLocal() as db:
        variant = RunVariant(run_id=run_id, variant_id="v1")
        db.add(variant)
        db.flush()
        db.add(VariantAsset(
            run_id=run_id, run_variant_id=variant.id, stage_name="copy_image_generation",
            asset_type="image", uri=uri, idempotency_key="hero", prompt_summary="侧光鞋刷主图",
        ))
        db.commit()


def draft(**overrides) -> dict:
    payload = {
        "name": "鞋刷主图 · 侧光识别款",
        "uri": "artifacts/hero.png",
        "reason": "柄尾标记和刷毛接触点都清楚",
        "prompt": "3:4 竖幅，侧光让折叠接缝可见",
        "image": png(),
        "base_image": png((90, 110, 140)),
        "layout": {
            "ratio": "3:4", "title": "看清每一道接缝", "titleX": 0.1, "titleY": 0.14,
            "color": "#ffffff", "cropX": 0.5, "cropY": 0.5, "logo": "",
            "logoX": 0.78, "logoY": 0.06,
        },
        "regions": [{"x": 0.1, "y": 0.2, "width": 0.3, "height": 0.2, "note": "标题压住刷柄，移到左上"}],
        "parent_id": None,
    }
    payload.update(overrides)
    return payload


def test_save_list_read_and_derive_variant(client):
    run_id = create_run(client, "studio-shop", "STUDIO-1")
    seed_asset(run_id)
    source = client.get(f"/runs/{run_id}/creative-source", params={"uri": "artifacts/hero.png"})
    assert source.status_code == 200
    assert source.json()["workspace"] == "studio-shop"
    assert source.json()["prompt"] == "侧光鞋刷主图"

    response = client.post(f"/runs/{run_id}/brand-creatives", json=draft())
    assert response.status_code == 201
    creative_id = response.json()["id"]

    listed = client.get("/brand-creatives", params={"workspace": "studio-shop"})
    assert listed.status_code == 200
    [item] = listed.json()
    assert item["id"] == creative_id
    assert item["ratio"] == "3:4"
    assert item["reason"].startswith("柄尾标记")
    assert item["thumbnail"].startswith("data:image/jpeg;base64,")
    assert "image" not in item  # The list stays light; full snapshots load on demand.

    stored = client.get(f"/brand-creatives/{creative_id}", params={"workspace": "studio-shop"})
    assert stored.status_code == 200
    document = stored.json()
    assert document["layout"]["title"] == "看清每一道接缝"
    assert document["regions"][0]["note"] == "标题压住刷柄，移到左上"
    assert document["base_image"].startswith("data:image/png;base64,")
    assert document["run_id"] == run_id

    variant = client.post(
        f"/runs/{run_id}/brand-creatives",
        json=draft(name="鞋刷主图 · 新品版本", uri="", parent_id=creative_id,
                   base_image="", layout={**draft()["layout"], "ratio": "1:1"}),
    )
    assert variant.status_code == 201
    derived = client.get(
        f"/brand-creatives/{variant.json()['id']}", params={"workspace": "studio-shop"}
    ).json()
    assert derived["parent_id"] == creative_id
    assert derived["uri"] == "artifacts/hero.png"
    assert derived["prompt"] == "3:4 竖幅，侧光让折叠接缝可见"
    assert derived["layout"]["ratio"] == "1:1"


def test_rejects_invalid_snapshot_region_and_parent(client):
    run_id = create_run(client, "studio-shop", "STUDIO-2")
    seed_asset(run_id)
    assert client.post(
        f"/runs/{run_id}/brand-creatives", json=draft(image="data:image/png;base64,bm90")
    ).status_code == 422
    assert client.post(
        f"/runs/{run_id}/brand-creatives",
        json=draft(regions=[{"x": 0.8, "y": 0.1, "width": 0.4, "height": 0.2, "note": "越界"}]),
    ).status_code == 422
    assert client.post(
        f"/runs/{run_id}/brand-creatives", json=draft(parent_id="missing")
    ).status_code == 404
    assert client.post(
        f"/runs/{run_id}/brand-creatives", json=draft(uri="")
    ).status_code == 422
    assert client.post(
        f"/runs/{run_id}/brand-creatives", json=draft(uri="artifacts/not-there.png")
    ).status_code == 404


def test_templates_are_scoped_to_their_workspace(client):
    run_id = create_run(client, "studio-shop", "STUDIO-3")
    other_run = create_run(client, "other-shop", "STUDIO-4")
    seed_asset(run_id)
    seed_asset(other_run)
    creative_id = client.post(f"/runs/{run_id}/brand-creatives", json=draft()).json()["id"]

    assert client.get("/brand-creatives", params={"workspace": "missing-shop"}).status_code == 404
    assert client.get(
        f"/brand-creatives/{creative_id}", params={"workspace": "other-shop"}
    ).status_code == 404
    assert client.post(
        f"/runs/{other_run}/brand-creatives",
        json=draft(uri="artifacts/hero.png", parent_id=creative_id),
    ).status_code == 422
    assert client.post(
        "/runs/missing/brand-creatives", json=draft(uri="artifacts/hero.png")
    ).status_code == 404
