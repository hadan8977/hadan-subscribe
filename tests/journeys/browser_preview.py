"""Exercise the public simulation in a disposable preview server. No real API calls."""
from pathlib import Path
import json,os,socket,subprocess,time,urllib.request,urllib.error
from playwright.sync_api import sync_playwright,expect
from browser_smoke import select_chromium
ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'test-results/preview-flow';OUT.mkdir(parents=True,exist_ok=True)

def run():
    sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close()
    origin=f'http://127.0.0.1:{port}'
    child=subprocess.Popen([str(ROOT/'.venv/bin/python'),'-I',str(ROOT/'scripts/preview.py')],env={**os.environ,'PASEO_PORT':str(port)},stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
    report={'checks':[],'page_errors':[],'api_network_requests':[]}
    try:
        for _ in range(50):
            try:
                with urllib.request.urlopen(origin,timeout=1):break
            except OSError:time.sleep(.1)
        else:raise RuntimeError('Preview failed to start')
        with sync_playwright() as p:
            chrome,_=select_chromium(p);browser=p.chromium.launch(executable_path=str(chrome),headless=True,chromium_sandbox=os.geteuid()!=0)
            try:
                for lang,width in [('zh-CN',1440),('en',390)]:
                    c=browser.new_context(viewport={'width':width,'height':1000},reduced_motion='reduce',permissions=['clipboard-read','clipboard-write'])
                    page=c.new_page();page.on('pageerror',lambda e:report['page_errors'].append(str(e)))
                    page.on('request',lambda q:report['api_network_requests'].append(q.url) if '/api/' in q.url or '/mcp' in q.url else None)
                    page.goto(origin+'/?lang='+lang);page.locator('#copy-agent-prompt').click()
                    expect(page.locator('#toast')).to_be_visible()
                    copied=page.evaluate('navigator.clipboard.readText()')
                    assert copied==('帮我订阅 ' if lang=='zh-CN' else 'Subscribe me to ')+origin
                    page.locator('[data-subscribe-entry]').click()
                    expect(page.locator('#email')).to_have_value('reader@example.test')
                    page.locator('#request-code').click();page.locator('#code').fill('000000');page.locator('#login-form button[type=submit]').click()
                    expect(page.locator('#login-status')).not_to_be_empty()
                    page.locator('#code').fill('123456');page.locator('#login-form button[type=submit]').click()
                    expect(page.locator('#onboarding-access')).to_be_visible()
                    page.screenshot(path=str(OUT/f'access-{lang}.png'),full_page=True)
                    expect(page.locator('#redeem-code')).to_have_value('QUANT-DEMO')
                    page.locator('#redeem-form button[type=submit]').click();expect(page.locator('#onboarding-ready')).to_be_visible()
                    page.screenshot(path=str(OUT/f'delivery-{lang}.png'),full_page=True)
                    page.locator('#onboard-agent').click();expect(page.locator('#onboarding-agent-heading')).to_be_visible()
                    expect(page.locator('#onboarding-progress')).to_be_visible();expect(page.locator('#account .tabs')).to_be_hidden()
                    page.locator('#copy-guide').click();assert page.evaluate('navigator.clipboard.readText()')==copied
                    page.locator('#inspect-pair').click();expect(page.locator('#pair-agent-name')).to_have_text('Quant Demo Agent')
                    page.screenshot(path=str(OUT/f'authorize-{lang}.png'),full_page=True)
                    page.locator('#approve-pair').click();expect(page.locator('#onboard-connect-finish')).to_be_enabled(timeout=7000)
                    expect(page.locator('.demo-status')).to_contain_text('未实际' if lang=='zh-CN' else 'No real')
                    page.screenshot(path=str(OUT/f'connected-{lang}.png'),full_page=True)
                    page.locator('#onboard-connect-finish').click();expect(page.locator('#view-updates')).to_be_visible()
                    expect(page.locator('#onboarding-progress')).to_be_hidden()
                    assert page.evaluate('localStorage.length+sessionStorage.length')==0
                    assert 'hadan_session=' not in page.evaluate('document.cookie')
                    assert page.evaluate("fetch('/api/admin/jobs',{method:'POST',body:'{}'}).then(r=>r.status)")==503
                    assert page.evaluate("fetch('https://example.com/api').then(r=>r.status)")==503
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                    page.locator('#events-list a').first.click();expect(page.locator('h1')).to_have_text('Portfolio rebalanced')
                    expect(page.locator('.attachments a')).to_have_count(2)
                    page.goto(origin+'/app?flow=subscribe');expect(page.locator('#login')).to_be_visible()
                    c.close();report['checks'].append('Complete isolated subscription/Agent journey, concise clipboard, boundaries and reset: '+lang)
                c=browser.new_context(viewport={'width':390,'height':1000});page=c.new_page()
                page.on('pageerror',lambda e:report['page_errors'].append(str(e)))
                page.goto(origin+'/preview/agent?lang=zh-CN')
                for language in ['zh-CN','en','es']:
                    page.locator('#demo-language').select_option(language)
                    page.locator('#demo-capability').select_option('auto');page.locator('#demo-reset').click()
                    for _ in range(3):page.locator('#demo-next').click()
                    expect(page.locator('#demo-transcript li')).to_have_count(8)
                    expect(page.locator('#demo-transcript a[download]')).to_have_count(2)
                    expect(page.locator('#demo-next')).to_be_hidden()
                    expect(page.locator('#demo-reset')).to_be_visible()
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                    page.screenshot(path=str(OUT/f'conversation-{language}.png'),full_page=True)
                    page.locator('#demo-capability').select_option('browse');expect(page.locator('#demo-next')).to_be_hidden()
                    expect(page.locator('#demo-transcript li')).to_have_count(2)
                    assert 'pair=' not in page.locator('#demo-transcript a').get_attribute('href')
                    page.locator('#demo-capability').select_option('ondemand');page.locator('#demo-next').click()
                    expected={'zh-CN':'你让我查看时','en':'Ask me to check','es':'Pídeme'}[language]
                    expect(page.locator('#demo-transcript')).to_contain_text(expected)
                c.close();report['checks'].append('Agent dialogue, revision, files and capability exits in Chinese, English and Spanish')
            finally:browser.close()
        for path in ['/skill.md','/llms.txt','/agent/instructions']:
            with urllib.request.urlopen(origin+path) as r:
                text=r.read().decode();assert 'READ-ONLY PREVIEW' in text and '127.0.0.1:8097' not in text
        try:urllib.request.urlopen(urllib.request.Request(origin+'/api/auth/request',data=b'{}'))
        except urllib.error.HTTPError as e:assert e.code==503
        else:raise AssertionError('Server accepted write')
        assert not report['api_network_requests'],report
        assert not report['page_errors'],report
        report['checks'].append('No API requests on browser network; documents preserve preview boundary and direct server writes return 503')
    finally:
        child.terminate();child.wait(timeout=10);report['owned_server_stopped']=True
        (OUT/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps(report,ensure_ascii=False))

if __name__=='__main__':run()
