from __future__ import annotations

from sqlalchemy import update

from app.api.routes import _run_stream_signature
from app.data.models import PipelineRun


def test_run_stream_signature_changes_with_run_status(client, db_session):
    created = client.post(
        "/runs",
        json={
            "workspace_name": "stream-shop",
            "project_name": "stream-project",
            "product_name": "stream product",
            "product_code": "STREAM-1",
            "industry_code": "apparel",
            "campaign_name": "stream-campaign",
            "pipeline_mode": "copy_image_only",
            "creative_preset": "meta_square_5s",
        },
    )
    assert created.status_code == 200
    run_id = created.json()["id"]

    before = _run_stream_signature(db_session)
    db_session.execute(
        update(PipelineRun)
        .where(PipelineRun.id == run_id)
        .values(status="completed")
    )
    db_session.commit()
    after = _run_stream_signature(db_session)

    assert after != before
    assert any(row[0] == run_id and row[1] == "completed" for row in after[0])
