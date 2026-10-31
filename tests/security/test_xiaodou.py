"""Xiaodou protocol and QR checkout journeys. Synthetic RSA keys, no network/mail."""
import base64
import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi.testclient import TestClient

from hadan.commerce import Commerce
from hadan.db import uid
from hadan.service import Problem
from hadan.terms import TERMS_VERSION
from hadan.xiaodou import Xiaodou, PLATFORM_KID, canonical_request, encoded

END = datetime.fromisoformat('2030-01-01T00:00:00+08:00').timestamp()
PLAN = {'id':'sample-plan','title':'Quant 订阅','valid_until':'2030-01-01T00:00:00+08:00','amount_minor':1000,'currency':'CNY'}


@pytest.fixture
def xd(harness, tmp_path, monkeypatch):
    app, service, login = harness
    cfg=service.settings
    cfg.base_url='https://testserver'
    cfg.xiaodou_enabled=True
    cfg.xiaodou_app_id='xdop_test000000000000'
    cfg.xiaodou_base_url='https://provider.example.test'
    cfg.xiaodou_cashier_origins=('https://cashier.example.test',)
    cfg.xiaodou_methods=('wechat','alipay')
    cfg.xiaodou_trade_scope='CARD_VIRTUAL'
    cfg.payment_plans=[dict(PLAN)]
    key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    path=tmp_path/'merchant.pem'
    path.write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
    path.chmod(0o600);cfg.xiaodou_private_key_file=str(path)
    platform=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    def platform_key(self,kid):
        if kid!=PLATFORM_KID:raise Problem('Unknown platform key',503)
        return platform.public_key()
    monkeypatch.setattr(Xiaodou,'platform_key',platform_key)
    service.db.execute("UPDATE products SET paid=1 WHERE id='quantgt'")
    original=httpx.Client
    calls=[]; snapshots={}; mode={'status':'CREATED','unsigned':False,'tamper':None,'timeout':False}
    def provider(request):
        raw=request.content; h=request.headers
        canonical=canonical_request(request.method,request.url.path,h['x-xd-timestamp'],h['x-xd-nonce'],cfg.xiaodou_app_id,h['x-xd-key-id'],h['x-xd-request-id'],raw)
        key.public_key().verify(base64.b64decode(h['x-xd-sign']),canonical,padding.PKCS1v15(),hashes.SHA256())
        calls.append({'body':raw,'method':request.method,'headers':dict(h),'path':request.url.path})
        if mode['timeout']:raise httpx.ReadTimeout('synthetic timeout',request=request)
        if request.method=='POST':
            body=json.loads(raw); oid=body['outTradeNo'];assert body['amountFen']==1000 and body['feeBearer']=='SELLER'
            assert body['payerClientIp']=='8.8.8.8';assert 'notifyUrl' not in body
            assert body['returnUrl']=='https://testserver/app?order='+oid
            snap={'openOrderId':'op_'+oid,'outTradeNo':oid,'status':mode['status'],'channel':body['channel'],'tradeScope':'CARD_VIRTUAL','amountFen':1000,'currency':'CNY','feeBearer':'SELLER','expireAt':datetime.fromtimestamp(time.time()+600).astimezone().isoformat(),'payUrl':'https://cashier.example.test/cashier/material-'+oid,'qrCode':'https://qr.alipay.com/material-'+oid if body['channel']=='ALIPAY_H5' else None}
            snapshots[oid]=snap
        else:
            oid=request.url.path.rsplit('/',1)[-1];snap={**snapshots[oid],'status':mode['status'],'payUrl':None,'qrCode':None}
        if mode['tamper']:snap.update(mode['tamper'])
        data=encoded(snap).encode();stamp=str(int(time.time()));nonce=uid();rid=h['x-xd-request-id']
        canonical='\n'.join(('XD-Response-v1','200','application/json',rid,stamp,nonce,PLATFORM_KID,hashlib.sha256(data).hexdigest())).encode()
        headers={'content-type':'application/json; charset=utf-8','x-xd-request-id':rid}
        if not mode['unsigned']:headers.update({'x-xd-response-sign':base64.b64encode(platform.sign(canonical,padding.PKCS1v15(),hashes.SHA256())).decode(),'x-xd-response-key-id':PLATFORM_KID,'x-xd-response-timestamp':stamp,'x-xd-response-nonce':nonce})
        return httpx.Response(200,content=data,headers=headers)
    monkeypatch.setattr(httpx,'Client',lambda *a,**kw:original(*a,**{**kw,'transport':httpx.MockTransport(provider)}))
    alice,user=login('alice@example.test')
    # The reverse proxy supplies an authenticated peer; arbitrary XFF is never read by app code.
    alice._transport.client=('8.8.8.8',54321)
    yield app,service,alice,user,platform,calls,mode,snapshots


def buy(client,**changes):
    return client.post('/api/purchase',json={'plan_id':PLAN['id'],'method':'wechat','accept_terms':True,'terms_version':TERMS_VERSION,**changes})


def notice(service,platform,oid,*,kind='PAYMENT_SUCCEEDED',event_id=None,stamp=None,**changes):
    eid=event_id or uid();stamp=str(int(time.time()) if stamp is None else stamp)
    value={'eventId':eid,'eventType':kind,'appId':service.settings.xiaodou_app_id,'outTradeNo':oid,'eventAmountFen':1000 if kind=='PAYMENT_SUCCEEDED' else 0,'grossAmountFen':1000,'status':'SUCCEEDED' if kind=='PAYMENT_SUCCEEDED' else 'CLOSED','feeBearer':'SELLER','stateVersion':0,**changes}
    raw=encoded(value).encode();nonce=uid()
    canonical='\n'.join(('XD-Webhook-v1',eid,kind,stamp,nonce,PLATFORM_KID,hashlib.sha256(raw).hexdigest())).encode()
    signature=base64.b64encode(platform.sign(canonical,padding.PKCS1v15(),hashes.SHA256())).decode()
    headers={'content-type':'application/json','x-xd-webhook-event-id':eid,'x-xd-webhook-event-type':kind,'x-xd-webhook-timestamp':stamp,'x-xd-webhook-nonce':nonce,'x-xd-webhook-key-id':PLATFORM_KID,'x-xd-webhook-signature':signature}
    return raw,headers


def post_notice(client,packet):
    raw,headers=packet
    return client.post('/webhooks/xiaodou',content=raw,headers=headers)


def test_qr_png_preserves_the_qr_module_grid():
    import struct
    import zlib
    import qrcode
    from hadan.commerce import qr_png

    value = "https://example.test/pay?order=abc123&token=" + "x" * 80
    code = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=1, border=0)
    code.add_data(value); code.make(fit=True)
    matrix = code.get_matrix(); modules = len(matrix)
    raw = qr_png(value, box_size=10, border=4)
    pos = 8; idat = b""; width = None
    while pos < len(raw):
        length = struct.unpack(">I", raw[pos:pos+4])[0]
        kind = raw[pos+4:pos+8]; payload = raw[pos+8:pos+8+length]; pos += 12 + length
        if kind == b"IHDR": width = struct.unpack(">IIBBBBB", payload)[0]
        if kind == b"IDAT": idat += payload
    pixels = zlib.decompress(idat); stride = 1 + width * 3
    for row in range(modules):
        line = pixels[(4 + row) * 10 * stride + 1:(4 + row + 1) * 10 * stride]
        for column in range(modules):
            black = line[(4 + column) * 10 * 3:(4 + column) * 10 * 3 + 3] == b"\x00\x00\x00"
            assert black is bool(matrix[row][column]), (row, column)


@pytest.mark.parametrize('method',['wechat','alipay'])
def test_qr_purchase_fixed_end_date_and_private_qr(xd,method):
    app,s,client,user,key,calls,mode,snapshots=xd
    r=buy(client,method=method,amount_minor=1,valid_until='2099-01-01')
    assert r.status_code==200,r.text
    order=r.json();oid=order['id']
    assert order['access_until']==END and order['amount_minor']==1000
    qr=client.get(order['qr_url']);assert qr.status_code==200 and qr.content.startswith(b'\x89PNG')
    assert qr.content[25]==2  # RGB, no alpha channel: opaque white background
    assert qr.headers['content-type'].startswith('image/png')
    assert qr.headers['cache-control']=='no-store' and qr.headers['referrer-policy']=='no-referrer'
    assert not s.entitled(user['id'],'quantgt')
    with TestClient(app) as other:assert other.get(order['qr_url']).status_code==401
    response=post_notice(client,notice(s,key,oid));assert response.status_code==200 and response.content==b'success'
    assert s.entitled(user['id'],'quantgt')['expires_at']==END
    assert client.get(order['qr_url']).status_code==410
    assert client.get('/api/orders/'+oid).json()['paid']
    assert buy(client).status_code==409


@pytest.mark.parametrize('change',[{'grossAmountFen':999},{'eventAmountFen':1001},{'eventAmountFen':1000.0},{'status':'CLOSED'},{'appId':'xdop_other00000000000'},{'feeBearer':'BUYER'}])
def test_signed_wrong_payment_facts_never_grant(xd,change):
    _,s,client,user,key,*_=xd;oid=buy(client).json()['id']
    assert post_notice(client,notice(s,key,oid,**change)).status_code==400
    assert not s.entitled(user['id'],'quantgt') and not s.db.all('SELECT * FROM payments')


def test_raw_body_signature_headers_and_replay(xd):
    _,s,client,user,key,*_=xd;oid=buy(client).json()['id'];raw,headers=notice(s,key,oid)
    assert client.post('/webhooks/xiaodou',content=raw+b' ',headers=headers).status_code==400
    assert post_notice(client,notice(s,key,oid,stamp=int(time.time())-600)).status_code==400
    changed={**headers,'x-xd-webhook-event-type':'REFUND_SUCCEEDED'}
    assert client.post('/webhooks/xiaodou',content=raw,headers=changed).status_code==400
    duplicated=list(headers.items())+[('x-xd-webhook-event-id','different')]
    assert client.post('/webhooks/xiaodou',content=raw,headers=duplicated).status_code==400
    assert not s.db.all('SELECT * FROM purchase_events')


def test_concurrent_callbacks_and_query_grant_once_preserve_optouts(xd):
    _,s,client,user,key,calls,mode,_=xd
    sid=uid();s.db.execute("INSERT INTO subscriptions(id,user_id,product_id,status,expires_at,email_enabled,agent_enabled,created_at) VALUES(?,?,'quantgt','active',?,0,0,?)",(sid,user['id'],time.time()-1,time.time()))
    s.db.execute("INSERT INTO delivery_preferences VALUES(?,'never')",(sid,))
    oid=buy(client).json()['id'];raw,headers=notice(s,key,oid)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results=list(pool.map(lambda _:Xiaodou(s).notify(raw,headers),range(4)))
    assert all(v['received'] for v in results)
    assert len(s.db.all('SELECT * FROM payments'))==1
    assert len(s.db.all('SELECT * FROM purchase_events'))==1
    sub=s.entitled(user['id'],'quantgt');assert sub['expires_at']==END and not sub['email_enabled'] and not sub['agent_enabled']
    # Different genuine events for the same order also cannot grant twice.
    assert post_notice(client,notice(s,key,oid)).status_code==200
    assert len(s.db.all('SELECT * FROM payments'))==1


def test_missed_callback_recovers_from_signed_query_and_accepts_late_callback(xd):
    _,s,client,user,key,calls,mode,_=xd;oid=buy(client).json()['id'];mode['status']='SUCCEEDED'
    result=Commerce(s).reconcile(oid);assert result['status']=='SUCCEEDED'
    assert s.entitled(user['id'],'quantgt')['expires_at']==END
    assert post_notice(client,notice(s,key,oid)).status_code==200
    assert len(s.db.all('SELECT * FROM payments'))==1


def test_closed_or_expired_orders_still_accept_late_payment(xd):
    _,s,client,user,key,*_=xd;oid=buy(client).json()['id']
    assert post_notice(client,notice(s,key,oid,kind='PAYMENT_CLOSED')).status_code==200
    assert client.get('/api/orders/'+oid).json()['status']=='expired'
    assert post_notice(client,notice(s,key,oid)).status_code==200
    assert s.entitled(user['id'],'quantgt')['expires_at']==END
    assert post_notice(client,notice(s,key,oid,kind='PAYMENT_CLOSED')).status_code==200
    assert client.get('/api/orders/'+oid).json()['paid']


def test_callback_works_after_disabling_new_checkout_or_removing_signing_key(xd):
    _,s,client,user,key,*_=xd;oid=buy(client).json()['id']
    s.settings.xiaodou_enabled=False;s.settings.xiaodou_methods=();s.settings.xiaodou_private_key_file=''
    assert post_notice(client,notice(s,key,oid)).status_code==200
    assert s.entitled(user['id'],'quantgt')['expires_at']==END


@pytest.mark.parametrize('mode_change',[{'unsigned':True},{'tamper':{'amountFen':1}},{'tamper':{'currency':'USD'}},{'tamper':{'channel':'ALIPAY_H5'}},{'tamper':{'payUrl':'https://evil.example.test/cashier/x'}}])
def test_untrusted_provider_response_never_exposes_qr_or_grants(xd,mode_change):
    _,s,client,user,key,calls,mode,_=xd;mode.update(mode_change)
    assert buy(client).status_code in (400,502)
    assert not s.entitled(user['id'],'quantgt')
    assert s.db.one('SELECT qr_data FROM purchase_orders')['qr_data'] is None


def test_timeout_reuses_request_id_body_and_order_but_refreshes_nonce(xd):
    _,s,client,user,key,calls,mode,_=xd;mode['timeout']=True
    assert buy(client).status_code==503;first=calls[0]
    mode['timeout']=False;r=buy(client);assert r.status_code==200,r.text
    second=calls[1]
    assert first['body']==second['body']
    assert first['headers']['x-xd-request-id']==second['headers']['x-xd-request-id']
    assert first['headers']['x-xd-nonce']!=second['headers']['x-xd-nonce']
    assert len(s.db.all('SELECT * FROM purchase_orders'))==1


def test_frozen_service_end_survives_later_plan_change(xd):
    _,s,client,user,key,*_=xd;oid=buy(client).json()['id']
    s.settings.payment_plans=[{**PLAN,'valid_until':'2030-08-01T00:00:00+08:00','amount_minor':9999}]
    assert post_notice(client,notice(s,key,oid)).status_code==200
    assert s.entitled(user['id'],'quantgt')['expires_at']==END


def test_unknown_payment_does_not_issue_new_order_or_qr(xd):
    _,s,client,user,key,calls,mode,_=xd;mode['status']='UNKNOWN'
    order=buy(client).json();assert order['status']=='verifying' and order['qr_url'] is None
    assert buy(client,method='alipay').status_code==409 and len(calls)==1
    assert not s.entitled(user['id'],'quantgt')


def test_expired_fixed_offer_stops_selling_and_rejects_ambiguous_periods(xd,monkeypatch):
    _,s,client,user,key,calls,*_=xd
    s.settings.payment_plans=[{**PLAN,'valid_until':'2026-01-01T00:00:00+08:00'}]
    assert Commerce(s).plans()==[] and buy(client).status_code==404 and not calls
    for bad in ({'valid_until':'2030-01-01'},{'days':30},{'valid_until':None}):
        s.settings.payment_plans=[{**PLAN,**bad}]
        with pytest.raises(Problem):Commerce(s).plans()


def pairing(client):
    r=client.post('/api/agent/pair',json={'name':'Private reader','mode':'poll'});assert r.status_code==200
    p=r.json();r=client.post('/api/pair/approve',json={'code':p['user_code']});assert r.status_code==200
    return r.json()['binding_id'],{'authorization':'Bearer '+p['device_secret']}


def authorize(client,bid,**changes):
    return client.post('/api/bindings/'+bid+'/checkout-permission',json={'plan_id':PLAN['id'],'method':'wechat','accept_terms':True,'terms_version':TERMS_VERSION,**changes})


def test_owner_session_can_prepare_authorized_checkout_and_get_qr(xd):
    app,s,client,user,key,calls,mode,_=xd;bid,auth=pairing(client)
    permission=authorize(client,bid);assert permission.status_code==200,permission.text
    permission_id=permission.json()['permission_id']
    assert permission.json()['order_id']==permission_id
    assert not calls
    order=client.post('/api/checkout-permissions/'+permission_id+'/prepare',json={})
    assert order.status_code==200,order.text
    order=order.json()
    assert order['id']==permission_id and order['status']=='pending'
    assert order['qr_url'] and order['pay_url']
    assert len(calls)==1
    again=client.post('/api/checkout-permissions/'+permission_id+'/prepare',json={})
    assert again.status_code==200 and again.json()['id']==permission_id
    assert len(calls)==1


def test_expired_pending_order_does_not_block_new_purchase(xd):
    app,s,client,user,key,calls,mode,_=xd
    old=buy(client).json();assert old['id']
    s.db.execute('UPDATE purchase_orders SET expires_at=? WHERE id=?',(time.time()-1,old['id']))
    fresh=buy(client)
    assert fresh.status_code==200,fresh.text
    assert fresh.json()['id']!=old['id']
    assert len(calls)==2
    assert client.get('/api/orders/'+old['id']).json()['status']=='expired'


def test_expired_pending_order_does_not_block_agent_permission(xd):
    app,s,client,user,key,calls,mode,_=xd
    old=buy(client).json();bid,auth=pairing(client)
    s.db.execute('UPDATE purchase_orders SET expires_at=? WHERE id=?',(time.time()-1,old['id']))
    permission=authorize(client,bid)
    assert permission.status_code==200,permission.text
    prepared=client.post('/api/checkout-permissions/'+permission.json()['permission_id']+'/prepare',json={})
    assert prepared.status_code==200,prepared.text
    assert prepared.json()['id']==permission.json()['permission_id']
    assert prepared.json()['qr_url']
    assert len(calls)==2


def test_agent_status_before_prepare_is_authorized_not_404(xd):
    app,s,client,user,key,calls,mode,_=xd;bid,auth=pairing(client)
    permission=authorize(client,bid);assert permission.status_code==200
    with TestClient(app) as agent:
        agent.headers.update(auth)
        status=agent.get('/api/agent/checkout')
        assert status.status_code==200,status.text
        assert status.json()['status']=='authorized'
        assert status.json()['permission_id']==permission.json()['permission_id']
        assert status.json()['qr_available'] is False and status.json()['checkout_url'] is None
    assert not calls


def test_creating_order_past_local_expiry_resumes_same_order(xd):
    app,s,client,user,key,calls,mode,_=xd;bid,auth=pairing(client)
    permission=authorize(client,bid);assert permission.status_code==200
    pid=permission.json()['permission_id']
    with TestClient(app) as agent:
        agent.headers.update(auth)
        mode['timeout']=True
        assert agent.post('/api/agent/checkout',json={}).status_code==503
        s.db.execute('UPDATE purchase_orders SET expires_at=? WHERE id=?',(time.time()-1,pid))
        mode['timeout']=False
        order=agent.post('/api/agent/checkout',json={})
        assert order.status_code==200,order.text
        assert order.json()['id']==pid
    assert len(calls)==2
    assert len(s.db.all('SELECT * FROM purchase_orders'))==1
    assert len(s.db.all('SELECT * FROM purchase_details'))==1


def test_agent_prepares_owner_approved_one_time_checkout_without_spending(xd):
    app,s,client,user,key,calls,mode,_=xd;bid,auth=pairing(client)
    with TestClient(app) as agent:
        agent.headers.update(auth)
        assert agent.post('/api/agent/checkout',json={}).status_code==403
        permission=authorize(client,bid);assert permission.status_code==200,permission.text
        assert not calls and not s.entitled(user['id'],'quantgt')
        order=agent.post('/api/agent/checkout',json={});assert order.status_code==200,order.text
        order=order.json();assert order['id']==permission.json()['order_id'] and order['payment_by']=='owner'
        assert order['amount_minor']==1000 and order['access_until']==END
        assert 'pay_url' not in order and 'qr_url' not in order
        assert order['qr_available'] is True and order['qr_image'].startswith('data:image/png;base64,')
        assert order['qr_download_url']=='https://testserver/api/agent/checkout/qr'
        assert order['checkout_url'] is None
        qr=agent.get('/api/agent/checkout/qr')
        assert qr.status_code==200 and qr.content.startswith(b'\x89PNG')
        assert qr.headers['content-type'].startswith('image/png')
        assert agent.get('/api/orders/'+order['id']+'/qr').status_code==401
        assert agent.post('/api/purchase',json={}).status_code==401
        assert agent.post('/api/agent/checkout',json={'amount_minor':1}).status_code==400
        status=agent.get('/api/agent/checkout').json()
        assert status['status']=='pending' and status['qr_available'] is True
        assert 'qr_image' not in status and status['checkout_url'] is None
        assert post_notice(client,notice(s,key,order['id'])).status_code==200
        assert agent.post('/api/agent/checkout',json={}).json()['paid']
        assert len(calls)==1 and len(s.db.all('SELECT * FROM payments'))==1
        assert s.entitled(user['id'],'quantgt')['expires_at']==END


def test_mcp_prepare_checkout_returns_png_without_browser_handoff(xd):
    app,s,client,user,key,calls,mode,_=xd;bid,auth=pairing(client)
    assert authorize(client,bid).status_code==200
    with TestClient(app) as agent:
        agent.headers.update(auth)
        prepared=agent.post('/mcp',json={"jsonrpc":"2.0","id":1,"method":"tools/call",
            "params":{"name":"prepare_checkout","arguments":{}}}).json()
        result=prepared['result'];assert result.get('isError') is not True
        value=json.loads(result['content'][0]['text'])
        assert value['qr_available'] is True and value['checkout_url'] is None
        assert value['amount_minor']==1000 and value['payment_by']=='owner'
        image=result['content'][1]
        assert image['type']=='image' and image['mimeType']=='image/png'
        assert base64.b64decode(image['data']).startswith(b'\x89PNG')
        qr=agent.post('/mcp',json={"jsonrpc":"2.0","id":2,"method":"tools/call",
            "params":{"name":"get_checkout_qr","arguments":{}}}).json()['result']
        assert qr['content'][1]['mimeType']=='image/png'
        assert base64.b64decode(qr['content'][1]['data']).startswith(b'\x89PNG')
        status=agent.post('/mcp',json={"jsonrpc":"2.0","id":3,"method":"tools/call",
            "params":{"name":"get_checkout_status","arguments":{}}}).json()['result']
        state=json.loads(status['content'][0]['text'])
        assert state['status']=='pending' and state['qr_available'] is True
        assert 'qr_image' not in state and state['checkout_url'] is None
    assert len(calls)==1 and not s.entitled(user['id'],'quantgt')


def test_mcp_agent_can_start_checkout_after_chat_confirmation(xd):
    app,s,client,user,key,calls,mode,_=xd;bid,auth=pairing(client)
    with TestClient(app) as agent:
        agent._transport.client=('8.8.8.8',54321)
        agent.headers.update(auth)
        response=agent.post('/mcp',json={"jsonrpc":"2.0","id":1,"method":"tools/call",
            "params":{"name":"prepare_checkout","arguments":{
                "plan_id":PLAN['id'],"method":"wechat","accept_terms":True,"terms_version":TERMS_VERSION}}}).json()
        result=response['result'];assert result.get('isError') is not True
        value=json.loads(result['content'][0]['text'])
        assert value['qr_available'] is True and value['checkout_url'] is None
        assert value['amount_minor']==1000 and value['payment_by']=='owner'
        assert result['content'][1]['type']=='image' and result['content'][1]['mimeType']=='image/png'
        assert base64.b64decode(result['content'][1]['data']).startswith(b'\x89PNG')
        again=agent.post('/mcp',json={"jsonrpc":"2.0","id":2,"method":"tools/call",
            "params":{"name":"prepare_checkout","arguments":{
                "plan_id":PLAN['id'],"method":"wechat","accept_terms":True,"terms_version":TERMS_VERSION}}}).json()['result']
        again_value=json.loads(again['content'][0]['text'])
        assert again_value['id']==value['id'] and again_value['qr_available'] is True
        assert s.db.one("SELECT COUNT(*) count FROM checkout_permissions")['count']==1
    assert len(calls)==1 and not s.entitled(user['id'],'quantgt')


def test_http_agent_can_start_checkout_without_web_authorization(xd):
    app,s,client,user,key,calls,mode,_=xd;bid,auth=pairing(client)
    with TestClient(app) as agent:
        agent._transport.client=('8.8.8.8',54321)
        agent.headers.update(auth)
        response=agent.post('/api/agent/checkout',json={
            "plan_id":PLAN['id'],"method":"wechat","accept_terms":True,"terms_version":TERMS_VERSION})
        assert response.status_code==200,response.text
        order=response.json()
        assert order['qr_available'] is True and order['checkout_url'] is None
        assert order['qr_image'].startswith('data:image/png;base64,')
        assert agent.get('/api/agent/checkout/qr').content.startswith(b'\x89PNG')
    assert len(calls)==1 and not s.entitled(user['id'],'quantgt')


def test_agent_permission_expires_is_explicit_and_cannot_survive_revocation(xd):
    app,s,client,user,key,calls,*_=xd;bid,auth=pairing(client)
    assert authorize(client,bid,accept_terms=False).status_code==400
    assert authorize(client,bid,terms_version='old').status_code==400
    result=authorize(client,bid);assert result.status_code==200
    s.db.execute('UPDATE checkout_permissions SET expires_at=?',(time.time()-1,))
    with TestClient(app) as agent:
        agent.headers.update(auth)
        assert agent.post('/api/agent/checkout',json={}).status_code==403
        assert authorize(client,bid).status_code==200
        assert client.delete('/api/bindings/'+bid).status_code==200
        assert agent.post('/api/agent/checkout',json={}).status_code==401
    assert not calls and not s.db.all('SELECT * FROM purchase_orders')


def test_agent_changed_quote_requires_new_human_agreement(xd):
    app,s,client,user,key,calls,*_=xd;bid,auth=pairing(client)
    assert authorize(client,bid).status_code==200
    s.settings.payment_plans=[{**PLAN,'valid_until':'2030-06-01T00:00:00+08:00'}]
    with TestClient(app) as agent:
        r=agent.post('/api/agent/checkout',json={},headers=auth)
        assert r.status_code==403
    assert not calls


def test_spoofed_forwarded_ip_does_not_authorize_payment(xd):
    _,s,client,user,key,calls,*_=xd
    client._transport.client=('127.0.0.1',54321)
    client.headers['x-forwarded-for']='8.8.8.8'
    assert buy(client).status_code==400 and not calls


def test_refund_before_payment_never_grants_access_and_records_fact(xd):
    _,s,client,user,key,*_=xd;oid=buy(client).json()['id']
    refund=notice(s,key,oid,kind='REFUND_SUCCEEDED',status='SUCCEEDED',eventAmountFen=1000,outRefundNo='refund_'+uid())
    assert post_notice(client,refund).status_code==200
    assert post_notice(client,notice(s,key,oid)).status_code==200
    assert not s.entitled(user['id'],'quantgt')
    assert s.db.one('SELECT status FROM purchase_orders')['status']=='review'
    assert s.db.one('SELECT amount_minor,status FROM purchase_refunds')=={'amount_minor':1000,'status':'succeeded'}
    assert len(s.db.all('SELECT * FROM payments'))==1
    assert s.db.one("SELECT kind FROM incidents")['kind']=='payment_review'
    assert post_notice(client,refund).status_code==200
    assert len(s.db.all('SELECT * FROM purchase_refunds'))==1


@pytest.mark.parametrize('initial',['UNKNOWN','CLOSED'])
def test_stale_create_response_cannot_restore_uncertain_or_closed_qr(xd,initial):
    _,s,client,user,key,calls,mode,snapshots=xd
    oid=buy(client).json()['id'];original=dict(snapshots[oid])
    mode['status']=initial;Commerce(s).reconcile(oid)
    expected='verifying' if initial=='UNKNOWN' else 'expired'
    assert s.db.one('SELECT status FROM purchase_orders')['status']==expected
    Xiaodou(s).apply_snapshot(oid,original,materials=True)
    row=s.db.one('SELECT status,qr_data FROM purchase_orders')
    assert row['status']==expected and row['qr_data'] is None


def test_existing_order_reconciles_when_new_checkout_disabled(xd):
    from hadan.schedule import tick
    _,s,client,user,key,calls,mode,_=xd;oid=buy(client).json()['id']
    s.settings.xiaodou_enabled=False;s.settings.xorpay_enabled=False
    s.db.execute('UPDATE purchase_orders SET created_at=? WHERE id=?',(time.time()-300,oid))
    tick(s)
    assert s.db.one("SELECT id FROM jobs WHERE kind='purchase_reconcile'")
    mode['status']='SUCCEEDED'
    assert Commerce(s).reconcile(oid)['status']=='SUCCEEDED'
    assert s.entitled(user['id'],'quantgt')['expires_at']==END
