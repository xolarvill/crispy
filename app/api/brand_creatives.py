import base64
import io
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.data.models import BrandCreative, PipelineRun, VariantAsset, Workspace
from app.data.session import get_db
from app.services.run_media import run_media_summaries

router = APIRouter()

# Snapshots are composited in the browser and stored with the template. Keep the
# contract below the browser's practical canvas export size and reject anything
# that is not a real PNG/JPEG so a template can always be reopened.
MAX_SNAPSHOT_EDGE = 4096


class Region(BaseModel):
    x: float = Field(ge=0, lt=1)
    y: float = Field(ge=0, lt=1)
    width: float = Field(gt=0, le=1)
    height: float = Field(gt=0, le=1)
    note: str = Field(min_length=1, max_length=500)


class Layout(BaseModel):
    ratio: Literal["1:1", "3:4", "16:9"] = "3:4"
    title: str = Field(default="", max_length=120)
    titleX: float = Field(default=.08, ge=0, le=1)
    titleY: float = Field(default=.12, ge=0, le=1)
    color: Literal["#ffffff", "#171717"] = "#ffffff"
    cropX: float = Field(default=.5, ge=0, le=1)
    cropY: float = Field(default=.5, ge=0, le=1)
    logo: str = Field(default="", max_length=400000)
    logoX: float = Field(default=.78, ge=0, le=1)
    logoY: float = Field(default=.06, ge=0, le=1)


class CreativeDraft(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=160)
    uri: str = Field(default="", max_length=4096)
    reason: str = Field(default="", max_length=2000)
    prompt: str = Field(default="", max_length=32000)
    image: str = Field(max_length=3000000)
    base_image: str = Field(default="", max_length=3000000)
    layout: Layout
    regions: list[Region] = Field(default_factory=list, max_length=30)
    parent_id: str | None = None


def decode_snapshot(value: str) -> Image.Image:
    try:
        header, data = value.split(",", 1)
        if header not in ("data:image/jpeg;base64", "data:image/png;base64"):
            raise ValueError()
        raw = base64.b64decode(data, validate=True)
        image = Image.open(io.BytesIO(raw))
        image.load()
        if image.format not in ("PNG", "JPEG") or max(image.size) > MAX_SNAPSHOT_EDGE:
            raise ValueError()
        return image
    except (ValueError, OSError, Image.DecompressionBombError) as exc:
        raise HTTPException(422, "图片格式或尺寸无效") from exc


def check_snapshot(value: str) -> None:
    decode_snapshot(value)


def thumbnail(value: str) -> str:
    image = decode_snapshot(value).convert("RGB")
    image.thumbnail((360, 360))
    out = io.BytesIO()
    image.save(out, "JPEG", quality=72, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode("ascii")


def workspace_named(db: Session, name: str) -> Workspace:
    workspace = db.scalar(select(Workspace).where(Workspace.name == name))
    if not workspace:
        raise HTTPException(404, "找不到此店铺")
    return workspace


def source_info(db: Session, run_id: str, uri: str):
    run = db.get(PipelineRun, run_id)
    if not run:
        raise HTTPException(404, "找不到此任务")
    items = run_media_summaries(db, [run_id], limit=None).get(run_id, {}).get("media_previews", [])
    if not any(m["uri"] == uri and m["kind"] == "image" for m in items):
        raise HTTPException(404, "找不到此任务的图片")
    asset = db.scalar(select(VariantAsset).where(
        VariantAsset.run_id == run_id, VariantAsset.uri == uri,
        VariantAsset.asset_type.in_(("image", "storyboard_frame")),
    ).order_by(VariantAsset.created_at.desc(), VariantAsset.id.desc()))
    return run, asset


@router.get("/runs/{run_id}/creative-source")
def creative_source(run_id: str, uri: str, db: Session = Depends(get_db)):
    run, asset = source_info(db, run_id, uri)
    return {
        "prompt": ((asset.payload or {}).get("prompt") or asset.prompt_summary or "") if asset else "",
        "workspace": db.get(Workspace, run.workspace_id).name,
        "variant": asset.variant.variant_id if asset else None,
        "stage": asset.stage_name if asset else None,
    }


@router.post("/runs/{run_id}/brand-creatives", status_code=201)
def save_creative(run_id: str, draft: CreativeDraft, db: Session = Depends(get_db)):
    run = db.get(PipelineRun, run_id)
    if not run:
        raise HTTPException(404, "找不到此任务")

    parent = None
    if draft.parent_id:
        parent = db.get(BrandCreative, draft.parent_id)
        if not parent:
            raise HTTPException(404, "找不到母版")
        if parent.workspace_id != run.workspace_id:
            raise HTTPException(422, "模板与店铺不匹配")

    if draft.uri:
        _, asset = source_info(db, run_id, draft.uri)
    elif parent:
        asset = None
    else:
        raise HTTPException(422, "请选择要保存的图片")

    source_run_id = parent.run_id if parent and not draft.uri else run.id
    uri = draft.uri or (parent.document.get("uri") if parent else "")
    prompt = draft.prompt or (parent.document.get("prompt") if parent else "")
    if asset is not None and not prompt:
        prompt = (asset.payload or {}).get("prompt") or asset.prompt_summary or ""

    if draft.base_image:
        check_snapshot(draft.base_image)
    for region in draft.regions:
        if region.x + region.width > 1.000001 or region.y + region.height > 1.000001:
            raise HTTPException(422, "批注超出图片范围")

    document = draft.model_dump(exclude={"parent_id", "name", "uri"})
    document.update({
        "uri": uri,
        "prompt": prompt,
        "asset_id": asset.id if asset else (parent.document.get("asset_id") if parent else None),
        "thumbnail": thumbnail(draft.image),
    })
    row = BrandCreative(workspace_id=run.workspace_id, run_id=source_run_id, name=draft.name,
                        parent_id=draft.parent_id, document=document)
    db.add(row)
    db.commit()
    return {"id": row.id}


@router.get("/brand-creatives")
def list_creatives(workspace: str, db: Session = Depends(get_db)):
    workspace_row = workspace_named(db, workspace)
    rows = db.scalars(select(BrandCreative).where(
        BrandCreative.workspace_id == workspace_row.id,
    ).order_by(BrandCreative.created_at.desc(), BrandCreative.id.desc()).limit(100)).all()
    return [
        {
            "id": row.id,
            "name": row.name,
            "parent_id": row.parent_id,
            "run_id": row.run_id,
            "reason": (row.document or {}).get("reason", ""),
            "ratio": ((row.document or {}).get("layout") or {}).get("ratio", "3:4"),
            "thumbnail": (row.document or {}).get("thumbnail", ""),
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }
        for row in rows
    ]


@router.get("/brand-creatives/{creative_id}")
def read_creative(creative_id: str, workspace: str, db: Session = Depends(get_db)):
    workspace_row = workspace_named(db, workspace)
    row = db.scalar(select(BrandCreative).where(
        BrandCreative.id == creative_id, BrandCreative.workspace_id == workspace_row.id,
    ))
    if not row:
        raise HTTPException(404, "此店铺没有该模板")
    return {
        **(row.document or {}),
        "id": row.id,
        "name": row.name,
        "parent_id": row.parent_id,
        "run_id": row.run_id,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }
