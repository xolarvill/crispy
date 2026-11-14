from __future__ import annotations


def test_dashboard_defaults_to_english(client):
    response = client.get("/dashboard")
    assert response.status_code == 200
    assert '<html lang="en">' in response.text
    assert "Product creative operations" in response.text
    assert "产品创意运营" not in response.text


def test_dashboard_renders_chinese_from_cookie(client):
    client.cookies.set("crispy.locale", "zh")
    response = client.get("/dashboard")
    assert response.status_code == 200
    assert '<html lang="zh-CN">' in response.text
    assert "产品创意运营" in response.text
    assert "Product creative operations" not in response.text


def test_query_language_overrides_cookie(client):
    client.cookies.set("crispy.locale", "en")
    response = client.get("/dashboard?lang=zh")
    assert response.status_code == 200
    assert "产品创意运营" in response.text


def test_i18n_dictionary_is_served(client):
    response = client.get("/dashboard/i18n.js")
    assert response.status_code == 200
    assert "application/javascript" in response.headers["content-type"]
    assert "window.CRISPY_I18N" in response.text
    assert "产品创意运营" in response.text


def test_translate_html_skips_scripts_and_code():
    from app.i18n import translate_html

    html = (
        "<script>const label = 'Dashboard';</script>"
        "<pre>Dashboard</pre>"
        "<button title='Dashboard'>Dashboard</button>"
    )
    translated = translate_html(html, "zh")
    assert "<script>const label = 'Dashboard';</script>" in translated
    assert "<pre>Dashboard</pre>" in translated
    assert "title='工作台'" in translated
    assert ">工作台<" in translated
