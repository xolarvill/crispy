"""Lightweight, batched media metadata for run lists and their preview gallery."""
from sqlalchemy import case, func, literal, or_, select, union_all
from sqlalchemy.orm import Session

from app.data.models import Artifact, VariantAsset


def run_media_summaries(db: Session, run_ids: list[str], limit: int | None = 3) -> dict[str, dict]:
    if not run_ids:
        return {}
    input_uri = func.lower(Artifact.uri)
    video_input = or_(*(input_uri.like(f"%.{ext}") for ext in ("mp4", "mov", "m4v", "webm")))
    image_input = or_(*(input_uri.like(f"%.{ext}") for ext in ("png", "jpg", "jpeg", "webp", "gif", "avif")))
    generated = select(
        VariantAsset.run_id.label("run_id"), VariantAsset.uri.label("uri"),
        case((VariantAsset.asset_type == "video", "video"), else_="image").label("kind"),
        literal(0).label("source_rank"), VariantAsset.created_at.label("created_at"),
    ).where(
        VariantAsset.run_id.in_(run_ids), VariantAsset.asset_type.in_(["image", "storyboard_frame", "video"]),
        VariantAsset.uri.is_not(None), func.trim(VariantAsset.uri) != "",
        VariantAsset.failure_category.is_(None), ~VariantAsset.uri.contains("_generation_error."),
    )
    inputs = select(
        Artifact.run_id.label("run_id"), Artifact.uri.label("uri"),
        case((or_(video_input, Artifact.artifact_type == "generated_video"), "video"), else_="image").label("kind"),
        case((Artifact.artifact_type == "input_file", 1), else_=0).label("source_rank"), Artifact.created_at.label("created_at"),
    ).where(
        Artifact.run_id.in_(run_ids),
        Artifact.artifact_type.in_(["input_file", "generated_image", "generated_video", "storyboard_frame"]),
        or_(Artifact.artifact_type != "input_file", image_input, video_input),
        func.trim(Artifact.uri) != "", ~Artifact.uri.contains("_generation_error."),
    )
    candidates = union_all(generated, inputs).subquery()
    # A repeated URI is one media item even if several variants reference it.
    unique_media = select(
        candidates.c.run_id, candidates.c.uri, candidates.c.kind,
        func.min(candidates.c.source_rank).label("source_rank"),
        func.max(candidates.c.created_at).label("created_at"),
    ).group_by(candidates.c.run_id, candidates.c.uri, candidates.c.kind).subquery()
    ranked = select(
        unique_media,
        func.count().over(partition_by=unique_media.c.run_id).label("total"),
        func.row_number().over(
            partition_by=unique_media.c.run_id,
            order_by=(unique_media.c.source_rank, unique_media.c.created_at.desc(), unique_media.c.uri),
        ).label("position"),
    ).subquery()
    query = select(ranked).order_by(ranked.c.run_id, ranked.c.position)
    if limit is not None:
        query = query.where(ranked.c.position <= limit)
    result: dict[str, dict] = {}
    for row in db.execute(query).mappings():
        summary = result.setdefault(row["run_id"], {"media_count": row["total"], "media_previews": []})
        summary["media_previews"].append({"uri": row["uri"], "kind": row["kind"], "source": "generated" if row["source_rank"] == 0 else "input"})
    return result
