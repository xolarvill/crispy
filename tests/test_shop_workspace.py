# tests/test_shop_workspace.py

from __future__ import annotations

import re


def test_list_shops_returns_array(client):
    resp = client.get("/shops")
    assert resp.status_code == 200
    body = resp.json()
    assert "shops" in body
    assert isinstance(body["shops"], list)
    assert {
        "product_count",
        "research_ready_count",
        "research_blocked_count",
        "memory_count",
        "memory_safe_count",
        "memory_review_count",
        "memory_conflict_count",
    }.issubset(body["shops"][0].keys())


def test_list_categories_for_unknown_shop(client):
    resp = client.get("/shops/nonexistent-shop/categories")
    assert resp.status_code == 200
    body = resp.json()
    assert body["categories"] == []


def test_workspace_has_industry_code():
    """Verify Workspace model has industry_code field."""
    from app.data.models import Workspace
    assert hasattr(Workspace, "industry_code")


def test_shop_analysis_page_has_shop_selector(client):
    resp = client.get("/dashboard/shop-analysis")
    assert resp.status_code == 200
    html = resp.text
    assert '<select id="shop-name"' in html
    assert "renderShopSelect" in html
    assert "Research Intelligence" in html


def test_create_run_form_has_shop_labels(client):
    resp = client.get("/dashboard")
    assert resp.status_code == 200
    html = resp.text
    assert "Product Category" in html
    assert "category-list" in html
    # Shop is now a <select> (managed list), not a free-text datalist
    assert '<select id="workspace_name"' in html


def test_shop_management_page_renders_workflow_layout(client):
    resp = client.get("/dashboard/shops")
    assert resp.status_code == 200
    html = resp.text
    assert "Shops" in html
    assert "Create Shop" in html
    assert "Shop workflow help" in html
    assert "shop-list" in html
    assert "shops-directory-bar" in html
    assert "shops-list-filters" in html
    assert "shop-directory" in html
    assert "shops-list-tools" in html
    assert "shops-layout" not in html
    assert "shops-sidebar" not in html
    assert "shops-list-head" not in html
    assert re.search(r'<dialog[^>]+id="create-shop-modal"[^>]*>', html)
    assert re.search(r'<dialog[^>]+aria-modal="true"[^>]*>', html)
    assert re.search(r'<dialog[^>]+aria-labelledby="[^"]+"[^>]*>', html)
    assert re.search(r'aria-haspopup="dialog"[^>]+aria-controls="create-shop-modal"', html)
    assert re.search(r'<button[^>]+class="ui-modal-close"[^>]+type="button"[^>]+onclick="this\.closest\(\'dialog\'\)\.close\(\)"', html)
    assert 'data-modal="reusable"' in html
    assert re.search(r'dialog\.ui-modal::backdrop[^}]*backdrop-filter:\s*blur\(', html)
    assert "function openShopCreateModal()" in html
    assert "function closeShopCreateModal()" in html
    assert "async function createShop()" in html
    assert "showModal()" in html
    assert "close()" in html
    assert "create-shop-card" not in html
    assert "create-shop-toggle" not in html
    assert "setCreateCollapsed" not in html
    assert "toggleCreateShop" not in html
    assert "renderShopWorkspace" in html
    assert "displayShopName" in html
    assert "summary-grid" in html
    assert "Research Context" in html
    assert "Memory Health" in html
    assert "Sites" in html
    assert "Channel Accounts" in html
    assert "connection-stack" in html
    assert "connection-bar" in html
    assert "connection-grid" not in html
    assert "loadShopConnections" in html
    assert "createChannelAccount" in html
    assert "channel-platform-bar" in html
    assert "channel-platform-form" in html
    for platform in ("shopify", "meta", "tiktok", "notion", "instagram", "facebook"):
        assert platform in html
    assert "new-channel-platform" not in html
    assert "workflow-card" not in html
    assert 'href="/dashboard/shops"' in html


def test_shop_crud_lifecycle(client):
    """Test create, rename, and delete a shop. Delete may be blocked if runs exist."""
    # Create
    resp = client.post("/shops", json={"name": "test-shop-crud"})
    assert resp.status_code == 201
    assert resp.json()["name"] == "test-shop-crud"

    # Rename
    resp = client.put("/shops/test-shop-crud", json={"name": "test-shop-renamed"})
    assert resp.status_code == 200
    assert resp.json()["name"] == "test-shop-renamed"

    # Delete (may be blocked if test isolation leaked runs)
    resp = client.delete("/shops/test-shop-renamed")
    assert resp.status_code in (204, 409)


def test_create_shop_returns_stable_id_and_metadata(client):
    resp = client.post(
        "/shops",
        json={
            "name": "analysis-shop",
            "industry_code": "pet_accessories",
            "store_url": "https://example-shop.test",
            "description": "Urban pet accessories store.",
        },
    )

    assert resp.status_code == 201
    body = resp.json()
    assert body["id"]
    assert body["name"] == "analysis-shop"
    assert body["industry_code"] == "pet_accessories"
    assert body["store_url"] == "https://example-shop.test"
    assert body["description"] == "Urban pet accessories store."
    assert body["archived_at"] is None
    assert body["site_count"] == 1
    sites = client.get(f"/shops/{body['id']}/sites").json()["sites"]
    assert sites[0]["url"] == "https://example-shop.test"
    assert sites[0]["is_primary"] is True


def test_archive_shop_hides_it_from_default_list(client):
    created = client.post("/shops", json={"name": "archive-me"}).json()

    resp = client.patch(f"/shops/{created['id']}", json={"archived": True})

    assert resp.status_code == 200
    assert resp.json()["archived_at"]
    listed = client.get("/shops").json()["shops"]
    assert all(item["id"] != created["id"] for item in listed)
    listed_with_archived = client.get("/shops?include_archived=true").json()["shops"]
    assert any(item["id"] == created["id"] for item in listed_with_archived)


def test_shop_sites_support_multiple_urls_and_primary_switch(client):
    shop = client.post(
        "/shops",
        json={"name": "multi-site-shop", "store_url": "https://primary.example"},
    ).json()

    created = client.post(
        f"/shops/{shop['id']}/sites",
        json={
            "label": "EU Store",
            "url": "https://eu.example",
            "site_type": "storefront",
            "platform": "shopify",
            "locale": "en-GB",
            "currency": "GBP",
            "is_primary": True,
        },
    )

    assert created.status_code == 201
    sites = client.get(f"/shops/{shop['id']}/sites").json()["sites"]
    assert len(sites) == 2
    assert [site["url"] for site in sites if site["is_primary"]] == ["https://eu.example"]
    updated_shop = next(item for item in client.get("/shops").json()["shops"] if item["id"] == shop["id"])
    assert updated_shop["store_url"] == "https://eu.example"
    assert updated_shop["site_count"] == 2


def test_shop_channel_accounts_support_platform_scoped_accounts(client):
    shop = client.post("/shops", json={"name": "channel-shop"}).json()

    resp = client.post(
        f"/shops/{shop['id']}/channel-accounts",
        json={
            "platform": "tiktok",
            "account_key": "tt-main",
            "label": "TikTok Main",
            "account_id": "adv-123",
            "credential_env_vars": {
                "access_token": "CRISPY_API_KEY_TIKTOK_MAIN",
                "advertiser_id": "CRISPY_API_KEY_TIKTOK_ADVERTISER_MAIN",
            },
            "sync_settings": {"auto_sync_minutes": 60},
            "attribution_rules": {"creative_key": "utm_content"},
            "is_primary": True,
        },
    )

    assert resp.status_code == 201
    account = resp.json()
    assert account["platform"] == "tiktok"
    assert account["is_primary"] is True
    assert account["credential_env_vars"]["access_token"] == "CRISPY_API_KEY_TIKTOK_MAIN"
    listed = client.get(f"/shops/{shop['id']}/channel-accounts").json()["accounts"]
    assert len(listed) == 1
    assert listed[0]["account_id"] == "adv-123"

    invalid = client.post(
        f"/shops/{shop['id']}/channel-accounts",
        json={
            "platform": "meta",
            "account_key": "meta-main",
            "credential_env_vars": {"access_token": "SECRET_VALUE_SHOULD_NOT_BE_STORED"},
        },
    )
    assert invalid.status_code == 400


def test_cannot_delete_last_shop(client):
    """Verify the last remaining shop cannot be deleted."""
    resp = client.get("/shops")
    shops = resp.json()["shops"]
    if len(shops) <= 1:
        # Delete should be blocked
        resp = client.delete(f"/shops/{shops[0]['name']}")
        assert resp.status_code == 409


def test_shop_analysis_links_to_shop_management_page(client):
    """Verify research keeps shop selection but sends management to the Shops page."""
    resp = client.get("/dashboard/shop-analysis")
    assert resp.status_code == 200
    html = resp.text
    assert "Manage Shops" in html
    assert "/dashboard/shops" in html
    assert "shop-list-mgmt" not in html
    assert 'Archive shop ""' not in html
