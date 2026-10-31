"""Language, public identity and authenticated page contracts; all data is disposable."""
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit

import pytest
from starlette.requests import Request

from hadan.i18n import get_language, template_context, translate_error


class VisibleText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.skip = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip -= 1

    def handle_data(self, value):
        if not self.skip:
            self.parts.append(value)


def visible(html):
    parser = VisibleText()
    parser.feed(html)
    return " ".join(parser.parts)


def request(path="/app", query="", headers=()):
    return Request({"type": "http", "method": "GET", "scheme": "https", "server": ("quant.example.test", 443),
                    "path": path, "raw_path": path.encode(), "query_string": query.encode(), "headers": list(headers)})


@pytest.mark.parametrize("query,cookie,header,expected", [
    ("lang=en", "quant_lang=zh-CN", "zh-CN", "en"),
    ("lang=zh-CN", "quant_lang=en", "en-US,en;q=0.9", "zh-CN"),
    ("", "quant_lang=en", "zh-CN", "en"),
    ("", "", "en-US,en;q=0.9", "en"),
    ("", "", "zh-TW,zh;q=0.9", "zh-CN"),
    ("", "", "", "zh-CN"),
])
def test_language_precedence(query, cookie, header, expected):
    assert get_language(request(query=query, headers=[(b"cookie", cookie.encode()), (b"accept-language", header.encode())])) == expected


def test_language_links_preserve_private_route_version_and_navigation_state():
    req = request("/read/" + "a" * 32, "version=2&lang=zh-CN&next=%2Fapp%3Fview%3Dconnections&empty=")
    link = template_context(req)["language_url"]("en")
    parsed = urlsplit(link)
    assert parsed.path == req.url.path
    assert parse_qs(parsed.query, keep_blank_values=True) == {"version": ["2"], "lang": ["en"], "next": ["/app?view=connections"], "empty": [""]}
    assert not parsed.scheme and not parsed.netloc


@pytest.mark.parametrize("path,cn,en", [
    ("/", "送达邮箱与 Agent", "For you and your agent."),
    ("/app", "登录 Quant", "Sign in to Quant"),
    ("/agent", "让已有 Agent 接收 Quant。", "Bring Quant to your agent."),
])
def test_public_pages_are_bilingual_single_product(journey, path, cn, en):
    client = journey.client()
    for lang, expected in (("zh-CN", cn), ("en", en)):
        response = client.get(path, params={"lang": lang})
        assert response.status_code == 200
        assert response.headers["content-language"] == lang
        assert f'<html lang="{lang}" ' in response.text
        text = visible(response.text)
        assert expected in text
        if path == "/":
            assert "notify@example.test" not in text
        else:
            assert "notify@example.test" in text
        for forbidden in ("Hadan", "Quant GT", "quantgt", "newsletter", "来信"):
            assert forbidden not in text
        assert "fonts.googleapis" not in response.text and "cdn.jsdelivr" not in response.text
        assert 'data-language="en"' in response.text and 'data-language="zh-CN"' in response.text
    # Persist the selection across pages, including navigation without lang in the URL.
    after = client.get("/app")
    assert after.headers["content-language"] == "en"
    assert "Sign in to Quant" in visible(after.text)


def test_subscription_view_purchase_before_redeem_and_no_manual_connection(journey):
    html = journey.client().get("/app").text
    assert html.index('id="purchase-section"') < html.index('redeem-panel')
    assert "manual-connection" not in html
    assert 'id="pair-code" type="hidden"' in html


def test_invalid_unsubscribe_renders_localized_recoverable_html(journey):
    client = journey.client()
    for lang, message in (("en", "This unsubscribe link is invalid."), ("zh-CN", "退订链接无效")):
        response = client.get("/unsubscribe/invalid", params={"lang": lang})
        assert response.status_code == 400
        assert "text/html" in response.headers["content-type"]
        assert message in visible(response.text)
        assert 'href="/app?view=subscriptions" class="button"' in response.text
        assert f'/unsubscribe/invalid?lang={"zh-CN" if lang == "en" else "en"}' in response.text


def test_authenticated_redemption_errors_follow_selected_language(journey):
    client = journey.login()
    me = client.get("/api/me").json()
    for lang, expected in (("en", "This access code is invalid or has already been used."), ("zh-CN", "兑换码无效或已使用")):
        client.get("/app", params={"lang": lang})
        response = client.post("/api/redeem", json={"code": "invalid"}, headers={"X-CSRF-Token": me["csrf"]})
        assert response.status_code == 400
        assert response.json()["error"] == expected
    options = client.get("/api/purchase-options").json()
    assert options["plans"] == []
    assert next(method for method in options["methods"] if method["id"] == "code")["enabled"] is True
    assert next(method for method in options["methods"] if method["id"] == "usdt")["enabled"] is False


def test_report_interface_translates_without_rewriting_original_content(journey):
    client = journey.login()
    journey.subscribe(client)
    edition = journey.service.publish("quantgt", "i18n-report", "原始中文报告标题", "原始中文正文；source provenance remains truthful。", files=[{"name": "原始附件.txt", "data": b"original"}])
    response = client.get("/read/" + edition["id"], params={"lang": "en"})
    assert response.status_code == 200
    text = visible(response.text)
    assert "Back to reports" in text and "Attachments" in text
    assert "Version history" not in text
    assert "V1" not in text
    assert "原始中文报告标题" in text and "原始中文正文" in text and "原始附件.txt" in text
    assert "Quant GT" not in text and "Hadan" not in text


def test_anonymous_read_preserves_valid_version_in_sign_in_redirect(journey):
    client = journey.client()
    content_id = "a" * 32
    response = client.get("/read/" + content_id, params={"version": 2, "lang": "en"}, follow_redirects=False)
    assert response.status_code == 303
    target = urlsplit(response.headers["location"])
    assert target.path == "/app"
    assert parse_qs(target.query) == {"next": ["/read/" + content_id + "?version=2"], "lang": ["en"]}


@pytest.mark.parametrize("version", [0, -1, 1_000_000_001])
def test_invalid_content_version_has_localized_error(journey, version):
    response = journey.client().get("/read/" + "a" * 32, params={"version": version, "lang": "en"})
    assert response.status_code == 400
    assert "Choose a valid content version." in visible(response.text)


def test_safe_error_fallback_and_bidirectional_language_switch():
    assert translate_error("付款订单不存在", "en") == "This payment order is unavailable."
    assert translate_error("This payment order is unavailable.", "zh-CN") == "付款订单不存在"
    assert translate_error("未登记的新错误", "en") == template_context(request(query="lang=en"))["t"]("generic_error")

def test_delivery_test_content_is_hidden_from_human_history(journey):
    owner = journey.login("test-delivery-filter@example.test")
    journey.subscribe(owner)
    me = owner.get("/api/me").json()["user"]
    result = journey.service.publish(
        "quantgt", "test:delivery-filter", "Delivery test", "One-time delivery test.", baseline=True
    )
    cid, eid = result["id"], result["event_id"]
    with journey.service.db.transaction() as db:
        journey.service._fanout(db, eid, "quantgt", only_user=me["id"])
    human_events = owner.get("/api/events", params={"limit": 100, "newest": "true"}).json()["events"]
    human_library = owner.get("/api/library").json()["contents"]
    assert cid not in {item["content_id"] for item in human_events}
    assert cid not in {item["id"] for item in human_library}
    _, agent = journey.connect(owner)
    agent_events = agent.get("/api/events", params={"pending": "true", "limit": 100}).json()["events"]
    assert cid in {item["content_id"] for item in agent_events}


