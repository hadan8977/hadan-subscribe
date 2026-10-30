"""Build public samples with the production report renderers and fictional data.

Run with PYTHONPATH=src. Never read application settings, credentials or member data.
"""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import os
import math
import re

from openpyxl import load_workbook
from playwright.sync_api import sync_playwright
from hadan.compat import picks_email, picks_excel
from hadan.compat.diff import compare
from hadan.compat.picks_format import NY, fetched_at_utc, format_fetched

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'src/hadan/static'


def public_text(value, *, original_time, display_time):
    """Adjust public branding and time without changing the report structure."""
    return value.replace('Quant GT', 'Quant').replace(original_time, display_time)


def check_public_text(value):
    assert not re.search(r'[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]', value)
    for forbidden in ('beijing', 'fictional', 'format sample'):
        assert forbidden not in value.lower()


def stock(symbol, company, price, entry, score, sector='Technology', **extra):
    return dict(symbol=symbol, company=company, sector=sector,
                current_price=f'${price:.2f}', buy_or_entry_price=f'${entry:.2f}',
                return_=f'{(price / entry - 1) * 100:+.2f}%', gt_score=str(score),
                held_since='2026-10-01', analyst_signal='Buy +0.75',
                next_earnings='2026-11-12', market_cap='$12.4B', revenue_ttm='$2.8B',
                revenue_growth_yoy='18.5%', pe_ttm='24.6', **extra)


def build():
    a = stock('SMPA', 'Sample Systems', 52.4, 50, 8.42)
    b = stock('SMPB', 'Sample Health', 38.6, 40, 7.65, sector='Healthcare')
    c = stock('SMPC', 'Sample Industrial', 66.2, 64, 8.16, sector='Industrials',
              signal_date='2026-10-26', signal_price='$64.00',
              watch_reason='Earnings revision and improving operating margin')
    for row in (a, b, c):
        row['return'] = row.pop('return_')
    data = dict(fetched_at='2026-10-28T14:30:00+00:00',
                monthly=dict(pick_date='2026-10-28', kind='monthly', rows=[a,b]),
                weekly=dict(pick_date='2026-10-26', kind='watchlist', rows=[c]))
    previous = deepcopy(data)
    previous['monthly']['rows'] = [deepcopy(a), stock('SMPD','Sample Networks', 28,30,7.20)]
    previous['monthly']['rows'][0]['gt_score'] = '8.10'
    previous['monthly']['rows'][1]['return'] = previous['monthly']['rows'][1].pop('return_')
    change = compare(previous, data)
    checked_at = fetched_at_utc(data).astimezone(NY)
    original_time = format_fetched(data)
    display_time = f'{checked_at:%b} {checked_at.day}, {checked_at:%Y} · {checked_at:%H:%M} ET'
    # The exact same renderers are called by sources.jobs.accept_picks in production.
    xlsx = OUT / 'quant-sample.xlsx'
    picks_excel.write_report(xlsx, data, diff=change, previous=previous)
    workbook = load_workbook(xlsx)
    for sheet in workbook:
        for row in sheet:
            for cell in row:
                if isinstance(cell.value, str):
                    cell.value = public_text(cell.value, original_time=original_time, display_time=display_time)
                    check_public_text(cell.value)
    workbook.properties.creator = 'Quant'
    workbook.properties.title = 'Quant report sample'
    workbook.save(xlsx)

    document = picks_email.build_html(data, diff=change, previous=previous)
    # Preserve the production fields, layout and report-specific subtitles.
    document = document.replace('Quant GT Picks Alert', 'QUANT / 28 OCT 2026')
    document = public_text(document, original_time=original_time, display_time=display_time)
    check_public_text(document)
    # The sample carries no source branding, live prices, member data or remote assets.
    for forbidden in ('quantgt', 'private.example', 'owner-email', '<script', 'src="http'):
        assert forbidden not in document.lower()
    with TemporaryDirectory(prefix='quant-public-sample-') as temporary:
        page_path = Path(temporary) / 'sample.html'
        page_path.write_text(document)
        with sync_playwright() as p:
            executable = os.environ.get('HADAN_TEST_CHROMIUM', '/opt/quant-subscribe/browsers/chromium-1223/chrome-linux64/chrome')
            browser = p.chromium.launch(executable_path=executable, headless=True, chromium_sandbox=os.geteuid()!=0)
            try:
                page = browser.new_page(viewport={'width':640,'height':900}, device_scale_factor=2)
                page.route('**/*', lambda route: route.continue_() if route.request.url.startswith('file:') else route.abort())
                page.goto(page_path.as_uri())
                visible_text = page.locator('body').inner_text()
                check_public_text(visible_text)
                assert display_time in visible_text
                page.screenshot(path=str(OUT/'quant-sample.png'), full_page=True)
                # Front-page crop is still a screenshot of the genuine template.
                for width, name in ((640, 'quant-sample-preview.png'), (350, 'quant-sample-mobile.png')):
                    page.set_viewport_size({'width':width,'height':1000})
                    changes = page.get_by_text('What changed', exact=True).locator('..').bounding_box()
                    crop_height = math.ceil(changes['y'] + changes['height'] + 2)
                    page.screenshot(path=str(OUT/name), clip={'x':8,'y':14,'width':width-16,'height':crop_height})
            finally:
                browser.close()
    for name in ('quant-sample.xlsx','quant-sample.png','quant-sample-preview.png','quant-sample-mobile.png'):
        print(name, (OUT/name).stat().st_size)


if __name__ == '__main__':
    build()
