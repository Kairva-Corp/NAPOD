import re
from server import app

with app.test_client() as c:
    r = c.get('/')
    html = r.data.decode()
    print(f'Page: {r.status_code} ({len(html)} bytes)')

    # Check initial data
    m = re.search(r'<script id="initial-data" type="application/json">(.+?)</script>', html, re.DOTALL)
    if m:
        data = m.group(1).strip()
        print(f'Initial data: {data[:80]}...')
    else:
        print('No initial data found')

    # Check date param
    r2 = c.get('/?date=2026-01-01')
    html2 = r2.data.decode()
    m2 = re.search(r'<script id="initial-data" type="application/json">(.+?)</script>', html2, re.DOTALL)
    if m2:
        data2 = m2.group(1).strip()
        print(f'Date param data: {data2[:80]}...')
    else:
        print('No initial data for date param')

    # Check error page (future date)
    r3 = c.get('/?date=2099-01-01')
    html3 = r3.data.decode()
    print(f'Future date: {r3.status_code} ({"error" in html3.lower()})')

    # Check initial data when error
    m3 = re.search(r'<script id="initial-data" type="application/json">(.+?)</script>', html3, re.DOTALL)
    if m3:
        d3 = m3.group(1).strip()
        print(f'Error page initial data: "{d3}"')
    else:
        print('No initial data on error page')

    print('\nAll OK')
