import json

import pytest

from hadan.billing import Billing
from hadan.commerce import Commerce
from hadan.terms import TERMS_VERSION


@pytest.mark.parametrize("endpoint,provider,payload", [
    ("/api/purchase", "commerce", {"plan_id": "month", "method": "wechat"}),
    ("/api/checkout", "stripe", {"product_id": "quantgt"}),
])
def test_payment_needs_current_explicit_agreement_before_provider_call(harness, monkeypatch, endpoint, provider, payload):
    _, service, login = harness
    client, user = login("terms@example.com")
    calls = []

    def purchase(*args, **kwargs):
        calls.append("commerce")
        return {"id": "test-order", "status": "pending"}

    def checkout(*args, **kwargs):
        calls.append("stripe")
        return {"url": "https://checkout.example.test/session"}

    monkeypatch.setattr(Commerce, "create_order", purchase)
    monkeypatch.setattr(Billing, "checkout", checkout)
    for consent in ({}, {"accept_terms": False, "terms_version": TERMS_VERSION},
                    {"accept_terms": "true", "terms_version": TERMS_VERSION},
                    {"accept_terms": True, "terms_version": "old-version"}):
        assert client.post(endpoint, json={**payload, **consent}).status_code == 400
        assert not calls
        assert not service.db.all("SELECT * FROM audit WHERE action='terms_accepted'")
    response = client.post(endpoint, json={**payload, "accept_terms": True, "terms_version": TERMS_VERSION})
    assert response.status_code == 200, response.text
    assert calls == [provider]
    row = service.db.one("SELECT * FROM audit WHERE action='terms_accepted'")
    assert row["actor"] == user["id"]
    details = json.loads(row["details"])
    assert details["version"] == TERMS_VERSION
    assert "法定退款权利" in details["refund_policy"]["zh-CN"]
    assert "required by law" in details["refund_policy"]["en"]
    assert row["created_at"] > 0
    assert row["object_id"] != "quantgt"  # A specific consent identity, never only a product.


def test_terms_are_public_and_payment_checkbox_starts_unchecked(harness):
    app, _, login = harness
    client, _ = login("terms-page@example.com")
    assert client.get("/api/purchase-options").json()["terms_version"] == TERMS_VERSION
    for lang, expected in (("zh-CN", "法定退款权利"), ("en", "required by law")):
        response = client.get("/terms?lang=" + lang)
        assert response.status_code == 200
        assert expected in response.text
        assert 'id="refunds"' in response.text
    html = client.get("/app").text
    checkbox = html.split('id="accept-terms"', 1)[1].split('>', 1)[0]
    assert "required" in checkbox and "checked" not in checkbox


def test_failed_checkout_consent_is_bounded_and_rate_limited(harness):
    _, service, login = harness
    client, _ = login('bounded-consent@example.com')
    accepted={'accept_terms':True,'terms_version':TERMS_VERSION,'method':'alipay'}
    for invalid in ('x'*131072, {}, [], None):
        response=client.post('/api/purchase',json={**accepted,'plan_id':invalid})
        assert response.status_code==400
    assert not service.db.all("SELECT * FROM audit WHERE action='terms_accepted'")
    # Extraneous client data never becomes permanent contract metadata.
    for _ in range(16):
        response=client.post('/api/purchase',json={**accepted,'plan_id':'month','product_id':'x'*131072})
        assert response.status_code==503
    records=service.db.all("SELECT details FROM audit WHERE action='terms_accepted'")
    assert len(records)==16
    assert all(len(row['details'])<5000 for row in records)
    assert all(json.loads(row['details'])['product_id']=='quantgt' for row in records)
    assert client.post('/api/purchase',json={**accepted,'plan_id':'month'}).status_code==429
    assert len(service.db.all("SELECT id FROM audit WHERE action='terms_accepted'"))==16
