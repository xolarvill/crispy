from sqlalchemy import event

from app.data.models import Artifact, RunVariant, VariantAsset
from app.data.session import SessionLocal, engine


def create_run(client, code):
    response = client.post('/runs', json={
        'workspace_name': 'media-shop', 'project_name': 'media-project',
        'product_name': code, 'product_code': code, 'industry_code': 'apparel',
        'campaign_name': 'media-campaign', 'creative_preset': 'meta_square_5s',
        'pipeline_mode': 'copy_image_only',
    })
    assert response.status_code == 200
    return response.json()['id']


def test_run_list_previews_are_batched_deduplicated_and_exclude_failed_media(client):
    run_id = create_run(client, 'MEDIA-1')
    other_id = create_run(client, 'MEDIA-2')
    with SessionLocal() as db:
        variant = RunVariant(run_id=run_id, variant_id='v1')
        db.add(variant)
        db.flush()
        for key, kind, uri, failure in [
            ('image', 'image', 'artifacts/generated.jpg', None),
            ('duplicate', 'image', 'artifacts/generated.jpg', None),
            ('video', 'video', 'artifacts/output.mp4', None),
            ('failed', 'image', 'artifacts/broken.png', 'provider_error'),
            ('error_file', 'image', 'artifacts/image_generation_error.png', None),
            ('pending', 'video', None, None),
        ]:
            db.add(VariantAsset(run_id=run_id, run_variant_id=variant.id, stage_name='copy_image_generation', asset_type=kind, uri=uri, failure_category=failure, idempotency_key=key))
        for kind, uri in [
            ('input_file', 'artifacts/reference.JPG'),
            ('input_file', 'artifacts/reference.webm'),
            ('input_file', 'artifacts/products.csv'),
            ('input_video_frame', 'artifacts/auxiliary-frame.jpg'),
            ('generated_image', 'artifacts/generated.jpg'),
            ('generated_image', 'https://example.test/legacy-image?v=1'),
        ]:
            db.add(Artifact(run_id=run_id, stage_name='intake', artifact_type=kind, uri=uri))
        db.add(Artifact(run_id=other_id, stage_name='intake', artifact_type='input_file', uri='artifacts/other.png'))
        db.commit()

    statements = []
    def count_selects(_conn, _cursor, statement, _params, _context, _many):
        if statement.lstrip().upper().startswith('SELECT'):
            statements.append(statement)
    event.listen(engine, 'before_cursor_execute', count_selects)
    try:
        response = client.get('/runs')
    finally:
        event.remove(engine, 'before_cursor_execute', count_selects)
    assert response.status_code == 200
    rows = {row['id']: row for row in response.json()}
    assert rows[run_id]['media_count'] == 5
    assert len(rows[run_id]['media_previews']) == 3
    assert rows[run_id]['media_previews'][0]['source'] == 'generated'
    assert rows[run_id]['media_previews'][1]['source'] == 'generated'
    assert rows[other_id]['media_count'] == 1
    assert len(statements) == 2  # One run query plus one batched metadata query, regardless of run count.

    gallery = client.get(f'/runs/{run_id}/media').json()
    assert gallery['run_id'] == run_id
    assert gallery['media_count'] == 5
    assert {item['uri'] for item in gallery['media_previews']} == {
        'artifacts/generated.jpg', 'artifacts/output.mp4', 'artifacts/reference.JPG', 'artifacts/reference.webm',
        'https://example.test/legacy-image?v=1',
    }
    assert {item['kind'] for item in gallery['media_previews']} == {'image', 'video'}


def test_empty_run_and_missing_run_media(client):
    run_id = create_run(client, 'EMPTY-MEDIA')
    assert client.get('/runs').json()[0]['media_previews'] == []
    assert client.get('/runs').json()[0]['media_count'] == 0
    assert client.get(f'/runs/{run_id}/media').json() == {'run_id': run_id, 'media_count': 0, 'media_previews': []}
    assert client.get('/runs/missing/media').status_code == 404
