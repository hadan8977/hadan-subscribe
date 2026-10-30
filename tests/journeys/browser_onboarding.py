"""Real local OTP, subscription, opt-out and deep-link journeys. No external sends."""
from __future__ import annotations
import csv,json,os,re,socket,tempfile,threading,time
from email import policy
from email.parser import BytesParser
from pathlib import Path
from urllib.parse import urlencode,urlsplit
import uvicorn
import httpx
from playwright.sync_api import sync_playwright,expect
from browser_smoke import select_chromium
from hadan.config import Settings
from hadan.commerce import Commerce
from hadan.db import uid
from hadan.web import create_app
from hadan.worker import Worker


def run():
    report={'checks':[],'page_errors':[]}
    with tempfile.TemporaryDirectory(prefix='quant-onboarding-') as temp:
        sock=socket.socket();sock.bind(('127.0.0.1',0))
        while sock.getsockname()[1]<10240:
            sock.close();sock=socket.socket();sock.bind(('127.0.0.1',0))
        origin=f'http://127.0.0.1:{sock.getsockname()[1]}'
        cfg=Settings(data_dir=Path(temp)/'data',base_url=origin,production=False,secret='temporary-onboarding-test-'*4,
            admin_emails=(),allowed_emails=(),mail_backend='file',mail_from='Quant <notify@example.test>',smtp_host='',smtp_user='',smtp_password='',stripe_key='',stripe_webhook_secret='',xorpay_enabled=False,xorpay_aid='',xorpay_secret='',xianyu_url='',payment_plans=[],deepseek_key='',ops_ai=False)
        app=create_app(cfg);service=app.state.service;service.seed()
        old_id=uid();service.db.execute('INSERT INTO users VALUES(?,?,?)',(old_id,'returning@example.test',time.time()))
        old_sub=service.subscribe(old_id,'quantgt');service.preferences(old_id,old_sub['id'],{'email_enabled':False,'agent_enabled':False})
        item=service.publish('quantgt','onboarding-report','Private report','Original private version',baseline=True)
        service.publish('quantgt','onboarding-report','Private report','Revised private version',changes='Synthetic revision',baseline=True)
        code_path=Path(temp)/'codes.csv';Commerce(service).issue_codes(days=30,count=3,output=code_path)
        with code_path.open() as stream:codes=[r['code'] for r in csv.DictReader(stream)]
        server=uvicorn.Server(uvicorn.Config(app,log_level='critical',access_log=False,lifespan='off'))
        thread=threading.Thread(target=server.run,kwargs={'sockets':[sock]},daemon=True);thread.start()
        def sub(email):return service.db.one('SELECT s.* FROM subscriptions s JOIN users u ON u.id=s.user_id WHERE u.email=?',(email,))
        def login(page,email,path='/app?flow=subscribe'):
            page.goto(origin+path)
            page.locator('#email').fill(email);page.locator('#request-code').click()
            expect(page.locator('#code-fields')).to_be_visible()
            for _ in range(40):
                if not Worker(service).run_once():break
            found=[]
            for path in (cfg.data_dir/'mail').glob('*.eml'):
                msg=BytesParser(policy=policy.default).parsebytes(path.read_bytes())
                if email not in str(msg.get('To','')):continue
                body=msg.get_body(preferencelist=('plain',));match=re.search(r'验证码是 (\d{6})',body.get_content() if body else '')
                if match:found.append((path.stat().st_mtime_ns,match.group(1)))
            page.locator('#code').fill(max(found)[1]);page.locator('#login-form button[type=submit]').click()
        def redeem(page,code):
            page.locator('#redeem-code').fill(code);page.locator('#redeem-form button[type=submit]').click()
            expect(page.locator('#onboarding-ready')).to_be_visible()
        try:
            deadline=time.monotonic()+10
            while not server.started and time.monotonic()<deadline:time.sleep(.02)
            assert server.started
            with sync_playwright() as p:
                chrome,_=select_chromium(p);browser=p.chromium.launch(executable_path=str(chrome),headless=True,chromium_sandbox=not(hasattr(os,'geteuid') and os.geteuid()==0))
                def context():
                    c=browser.new_context(viewport={'width':1280,'height':900},locale='zh-CN')
                    c.route('**/*',lambda r:r.continue_() if r.request.url.startswith(origin+'/') else r.abort())
                    c.on('page',lambda page:page.on('pageerror',lambda e:report['page_errors'].append(str(e))))
                    return c
                try:
                    c=context();page=c.new_page();requests=[];page.on('request',lambda r:requests.append((r.method,urlsplit(r.url).path)))
                    held=[];feed_requests={'/api/events':0,'/api/library':0}
                    def hold_pre_access_feed(route):
                        path=urlsplit(route.request.url).path
                        feed_requests[path]+=1
                        if feed_requests[path]==1:
                            # Keep the pre-subscription empty snapshot in flight.
                            held.append((path,route))
                        else:
                            route.continue_()
                    page.route('**/api/events?*',hold_pre_access_feed)
                    page.route('**/api/library',hold_pre_access_feed)
                    def publish_after_access(route):
                        response=route.fetch()
                        assert response.status==200
                        service.publish('quantgt','after-activation','Available after activation','Synthetic content published after the first subscription.')
                        route.fulfill(response=response)
                    page.route('**/api/subscriptions',publish_after_access)
                    login(page,'new-free@example.test')
                    expect(page.locator('#onboarding-ready')).to_be_visible(timeout=6000)
                    assert sub('new-free@example.test')['email_enabled']==1
                    assert requests.count(('POST','/api/subscriptions'))==1
                    # The second feed uses real server responses after activation:
                    # an event plus the existing private archive must be available.
                    expect(page.locator('#events-list')).to_contain_text('Available after activation')
                    expect(page.locator('#archive-list')).to_contain_text('Private report')
                    assert feed_requests=={'/api/events':2,'/api/library':2}
                    page.locator('[data-language=en]').click();expect(page.locator('#welcome-heading')).to_have_text('Choose your delivery')
                    page.locator('#onboard-agent').click();expect(page.locator('#view-connections')).to_be_visible()
                    expect(page.locator('#onboarding-progress')).to_be_visible()
                    page.locator('#onboard-connect-later').click()
                    expect(page.locator('#onboarding-progress')).to_be_hidden()
                    expect(page.locator('#events-list')).to_contain_text('Available after activation')
                    # Release the older empty responses only after newer content is
                    # visible, then drain callbacks before checking they were ignored.
                    for path,route in held:
                        route.fulfill(json={'events':[],'next_before':None} if path=='/api/events' else {'contents':[],'next_before':None,'next_before_id':None})
                    page.wait_for_load_state('networkidle')
                    expect(page.locator('#events-list')).to_contain_text('Available after activation')
                    expect(page.locator('#archive-list')).to_contain_text('Private report')
                    expect(page.locator('#updates-list .error')).to_have_count(0)
                    page.unroute('**/api/events?*');page.unroute('**/api/library');page.unroute('**/api/subscriptions')
                    assert requests.count(('POST','/api/subscriptions'))==1
                    report['checks'].append('Explicit free subscription creates access once after real OTP; delayed library does not block; Agent follows completion')
                    report['checks'].append('Older pre-access empty events and archive responses arrive last without replacing the real post-activation reports')
                    c.close()
                    # Human-first: Agent setup stays in step three and updates after a real external test.
                    with httpx.Client(base_url=origin,trust_env=False,timeout=15) as device:
                        pair=device.post('/api/agent/pair',json={'name':'Inline delivery agent','mode':'poll'}).json()
                        c=context();page=c.new_page();login(page,'inline-agent@example.test')
                        expect(page.locator('#onboarding-ready')).to_be_visible()
                        page.locator('#onboard-agent').click()
                        expect(page.locator('#onboarding-progress')).to_be_visible()
                        expect(page.locator('#account .tabs')).to_be_hidden()
                        page.goto(pair['verification_uri'])
                        expect(page.locator('#pair-agent-name')).to_have_text('Inline delivery agent')
                        page.locator('#approve-pair').click();expect(page.locator('#onboard-connect-finish')).to_be_disabled()
                        device.headers['Authorization']='Bearer '+pair['device_secret']
                        assert device.get('/api/agent/test').status_code==200
                        assert device.get('/api/agent/test-file').status_code==200
                        assert device.post('/api/agent/test/complete',json={}).status_code==200
                        expect(page.locator('#onboard-connect-finish')).to_be_enabled(timeout=7000)
                        expect(page.locator('#onboard-connect-status')).to_contain_text('已连接')
                        page.locator('#onboard-connect-finish').click();expect(page.locator('#view-updates')).to_be_visible()
                        expect(page.locator('#onboarding-progress')).to_be_hidden()
                        assert sub('inline-agent@example.test')['email_enabled']==1
                        c.close()
                    report['checks'].append('Human email signup keeps Agent setup in step three and bounded polling reflects real external test completion without reopening settings')
                    # Agent entry starts with human email/access, then a named explicit approval.
                    with httpx.Client(base_url=origin,trust_env=False,timeout=15) as device:
                        pair=device.post('/api/agent/pair',json={'name':'Personal research agent','mode':'poll'}).json()
                        c=context();page=c.new_page()
                        login(page,'agent-new@example.test',urlsplit(pair['verification_uri']).path+'?'+urlsplit(pair['verification_uri']).query)
                        expect(page.locator('#view-subscriptions')).to_be_visible()
                        assert sub('agent-new@example.test') is None
                        assert device.post('/api/agent/pair/status',json={'pair_id':pair['pair_id'],'device_secret':pair['device_secret']}).json()['state']=='pending'
                        page.locator('[data-subscribe]').click()
                        expect(page.locator('#view-connections')).to_be_visible()
                        expect(page.locator('#pair-agent-name')).to_have_text('Personal research agent')
                        expect(page.locator('#approve-pair')).to_be_enabled()
                        assert device.post('/api/agent/pair/status',json={'pair_id':pair['pair_id'],'device_secret':pair['device_secret']}).json()['state']=='pending'
                        page.locator('#approve-pair').click();expect(page.locator('#pair-code')).to_have_value('')
                        device.headers['Authorization']='Bearer '+pair['device_secret']
                        status=device.get('/api/agent/status').json();assert status['entitled'] and not status['ready']
                        assert device.get('/api/agent/test').status_code==200
                        test_file=device.get('/api/agent/test-file');assert test_file.content
                        assert device.post('/api/agent/test/complete',json={}).status_code==200
                        assert device.get('/api/agent/status').json()['ready']
                        expect(page.locator('#onboard-connect-finish')).to_be_enabled(timeout=7000)
                        expect(page.locator('#onboard-connect-status')).to_contain_text('可以返回 Agent')
                        expect(page.locator('#pair-form')).to_be_hidden()
                        assert sub('agent-new@example.test')['email_enabled']==1
                        c.close()
                    report['checks'].append('Agent-first visitor verifies email, explicitly starts free access, sees the real agent name, approves once, and becomes ready only after body/file testing')
                    service.db.execute("UPDATE products SET paid=1 WHERE id='quantgt'")
                    c=context();page=c.new_page();requests=[];page.on('request',lambda r:requests.append((r.method,urlsplit(r.url).path)))
                    login(page,'returning@example.test')
                    expect(page.locator('#onboarding-ready')).to_be_visible();expect(page.locator('#welcome-delivery')).to_contain_text('保持关闭')
                    page.locator('[data-language=en]').click();expect(page.locator('#welcome-delivery')).to_contain_text('stays off')
                    assert sub('returning@example.test')['email_enabled']==0
                    assert ('POST','/api/subscriptions') not in requests and not any(m=='PATCH' for m,_ in requests)
                    page.locator('#onboard-agent').click();expect(page.locator('#view-connections')).to_be_visible()
                    assert sub('returning@example.test')['agent_enabled']==0
                    report['checks'].append('Paid lifetime access bypasses purchase; login, language change and Agent entry preserve both opt-outs')
                    c.close()
                    c=context();page=c.new_page();login(page,'new-paid@example.test')
                    expect(page.locator('#onboarding-access')).to_be_visible();expect(page.locator('#onboarding-ready')).to_be_hidden()
                    assert sub('new-paid@example.test') is None
                    redeem(page,codes[0]);assert sub('new-paid@example.test')['expires_at']>time.time()
                    report['checks'].append('Paid visitor sees no false completion; real single-use redemption advances to optional Agent')
                    c.close()
                    c=context();page=c.new_page()
                    private='/read/'+item['id']+'?version=1'
                    login(page,'private-new@example.test','/app?'+urlencode({'next':private,'lang':'en'}))
                    expect(page.locator('#onboarding-access')).to_be_visible();assert urlsplit(page.url).path=='/app'
                    redeem(page,codes[1]);page.locator('#onboard-read').click();expect(page.locator('.prose')).to_have_text('Original private version')
                    assert 'version=1' in page.url
                    report['checks'].append('Unentitled private deep link waits for access and returns to the exact requested version')
                    c.close()
                    for email in ('returning@example.test','pair-new@example.test'):
                        c=context();page=c.new_page();login(page,email,'/app?'+urlencode({'pair':'ABCD1234','next':private,'flow':'subscribe'}))
                        expect(page.locator('#view-connections' if email=='returning@example.test' else '#view-subscriptions')).to_be_visible();expect(page.locator('#pair-code')).to_have_value('ABCD1234')
                        expect(page.locator('#approve-pair')).to_be_disabled()
                        expect(page.locator('#onboarding-ready')).to_be_hidden();assert urlsplit(page.url).path=='/app'
                        if email=='pair-new@example.test':assert sub(email) is None
                        c.close()
                    report['checks'].append('Pairing entry wins over conflicting next/subscribe parameters, with or without paid access')
                    service.db.execute("UPDATE products SET paid=0 WHERE id='quantgt'")
                    c=context();page=c.new_page();failed={'once':False};verifications=[]
                    def fail_first_account(r):
                        if 'hadan_session=' in r.request.headers.get('cookie','') and not failed['once']:
                            failed['once']=True;r.fulfill(status=503,json={'error':'Temporary fixture failure'})
                        else:r.continue_()
                    page.route('**/api/me',fail_first_account)
                    page.on('request',lambda r:verifications.append(r.url) if r.method=='POST' and r.url.endswith('/api/auth/verify') else None)
                    login(page,'retry-new@example.test');expect(page.locator('#retry-account')).to_be_visible()
                    page.locator('#retry-account').click();expect(page.locator('#onboarding-ready')).to_be_visible()
                    assert len(verifications)==1 and failed['once']
                    report['checks'].append('OTP success followed by account failure retries account loading without consuming a second code')
                    c.close()
                    assert not report['page_errors'],report['page_errors']
                finally:browser.close()
        finally:
            server.should_exit=True;thread.join(timeout=10);sock.close();report['owned_server_stopped']=not thread.is_alive()
            path=Path('test-results/ux/onboarding-report.json');path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(report,ensure_ascii=False,indent=2))
            print(json.dumps(report,ensure_ascii=False))
            assert not thread.is_alive()
if __name__=='__main__':run()
