import os
import json
import time
import hashlib
import re
import urllib.request
import urllib.error
import urllib.parse
from collections import OrderedDict
from threading import Lock

from flask import Flask, request, Response, render_template, send_from_directory
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

MONTHS = ['JAN','FEB','MAR','APR','MAY','JUN','JUL','AUG','SEP','OCT','NOV','DEC']

@app.template_filter('month_name')
def month_name_filter(month_str):
    try:
        return MONTHS[int(month_str) - 1]
    except (ValueError, IndexError):
        return month_str

NASA_API_KEY = os.environ.get('NASA_API_KEY')
NASA_APOD_URL = 'https://api.nasa.gov/planetary/apod'

# ---------------------------------------------------------------------------
#  Supabase (optional) — REST API client
# ---------------------------------------------------------------------------
SUPABASE_URL = os.environ.get('SUPABASE_URL', '').rstrip('/')
SUPABASE_ANON_KEY = os.environ.get('SUPABASE_ANON_KEY', '')
SUPABASE_ENABLED = bool(SUPABASE_URL and SUPABASE_ANON_KEY)

_SUPA_HEADERS = {
    'apikey': SUPABASE_ANON_KEY,
    'Authorization': 'Bearer ' + SUPABASE_ANON_KEY,
    'Content-Type': 'application/json',
}

def _supa_req(method, path, body=None):
    if not SUPABASE_ENABLED:
        raise RuntimeError('Supabase not configured')
    url = SUPABASE_URL + '/rest/v1/' + path
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method, headers=_SUPA_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            raw = resp.read().decode()
            return (resp.status, json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        return (e.code, json.loads(raw) if raw else {})
    except (urllib.error.URLError, OSError):
        raise RuntimeError('Supabase unreachable')

# ---------------------------------------------------------------------------
#  Visitor count
# ---------------------------------------------------------------------------
_VISITOR_CACHE = {}
_VISITOR_LOCK = Lock()
VISITOR_DEBOUNCE = 3600

def _record_visit(ip):
    if not SUPABASE_ENABLED:
        return None
    ip_hash = hashlib.sha256(ip.encode()).hexdigest()
    now_ts = time.time()
    with _VISITOR_LOCK:
        last = _VISITOR_CACHE.get(ip_hash)
        if last and (now_ts - last) < VISITOR_DEBOUNCE:
            pass
        _VISITOR_CACHE[ip_hash] = now_ts
    try:
        cutoff = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now_ts - 86400))
        status, rows = _supa_req('GET', f'visits?ip_hash=eq.{ip_hash}&visited_at=gt.{cutoff}&select=id&limit=1')
        if status == 200 and len(rows) == 0:
            _supa_req('POST', 'visits', {'ip_hash': ip_hash, 'visited_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())})
            st_r, rows_r = _supa_req('GET', 'visit_count?id=eq.1&select=count')
            if st_r == 200 and rows_r:
                new_count = rows_r[0].get('count', 0) + 1
                _supa_req('PATCH', 'visit_count?id=eq.1', {'count': new_count})
        st2, rows2 = _supa_req('GET', 'visit_count?id=eq.1&select=count')
        if st2 == 200 and rows2:
            return rows2[0].get('count', 0)
    except RuntimeError:
        pass
    except Exception:
        pass
    return None

# ---------------------------------------------------------------------------
#  Cache — Supabase-backed with in-memory fallback
# ---------------------------------------------------------------------------
CACHE_TTL = int(os.environ.get('CACHE_TTL', '3600'))
_fallback_cache = OrderedDict()
_fallback_lock = Lock()
FALLBACK_MAX = 200

def _cache_get(key):
    if SUPABASE_ENABLED:
        try:
            status, rows = _supa_req('GET', f'cache?key=eq.{urllib.parse.quote(key, safe="")}&select=value,expires_at&limit=1')
            if status == 200 and rows:
                expires = rows[0].get('expires_at', '')
                if expires and expires < time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()):
                    try:
                        _supa_req('DELETE', f'cache?key=eq.{urllib.parse.quote(key, safe="")}')
                    except RuntimeError:
                        pass
                else:
                    return rows[0]['value'], {}
        except RuntimeError:
            pass
        except Exception:
            pass
    with _fallback_lock:
        if key not in _fallback_cache:
            return None
        val, ts = _fallback_cache[key]
        if time.time() - ts > CACHE_TTL:
            del _fallback_cache[key]
            return None
        _fallback_cache.move_to_end(key)
        return val, {}

def _cache_put(key, value, nasa_headers=None):
    if SUPABASE_ENABLED:
        expires = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(time.time() + CACHE_TTL))
        body = {'key': key, 'value': value, 'expires_at': expires}
        try:
            _supa_req('POST', 'cache', body)
        except RuntimeError:
            pass
        except Exception:
            pass
    with _fallback_lock:
        if key in _fallback_cache:
            _fallback_cache.move_to_end(key)
        _fallback_cache[key] = (value, time.time())
        while len(_fallback_cache) > FALLBACK_MAX:
            _fallback_cache.popitem(last=False)

# ---------------------------------------------------------------------------
#  Tiered rate limiter — per-IP burst, per-IP sustained, global
# ---------------------------------------------------------------------------
BURST_LIMIT = 10          # max requests in burst window
BURST_WINDOW = 10         # seconds
SUSTAINED_LIMIT = 120     # max requests in sustained window
SUSTAINED_WINDOW = 60     # seconds
GLOBAL_LIMIT = 500        # total across all IPs
GLOBAL_WINDOW = 60        # seconds
NASA_BUDGET = 900         # max NASA API calls per hour (safety margin below 1000)
NASA_BUDGET_WINDOW = 3600

_rate_store = {}        # ip -> [timestamps]
_nasa_budget_store = []  # [timestamps]
_global_store = []       # [timestamps]
_rate_lock = Lock()

def _rate_limited(ip):
    """Check all three tiers. Returns (blocked: bool, retry_after: float, is_nasa_budget_exhausted: bool)."""
    now = time.time()
    retry_after = 0
    nasa_exhausted = False

    with _rate_lock:
        # --- Global tier ---
        cutoff = now - GLOBAL_WINDOW
        _global_store[:] = [t for t in _global_store if t > cutoff]
        if len(_global_store) >= GLOBAL_LIMIT:
            retry_after = max(retry_after, _global_store[0] + GLOBAL_WINDOW - now)
        else:
            _global_store.append(now)

        # --- Per-IP sustained tier ---
        ts_list = _rate_store.get(ip, [])
        cutoff_s = now - SUSTAINED_WINDOW
        ts_list = [t for t in ts_list if t > cutoff_s]
        if len(ts_list) >= SUSTAINED_LIMIT:
            retry_after = max(retry_after, ts_list[0] + SUSTAINED_WINDOW - now)
        else:
            # --- Per-IP burst tier (checked within sustained) ---
            cutoff_b = now - BURST_WINDOW
            burst_count = sum(1 for t in ts_list if t > cutoff_b)
            if burst_count >= BURST_LIMIT:
                retry_after = max(retry_after, BURST_WINDOW)
            else:
                ts_list.append(now)
                _rate_store[ip] = ts_list

        # --- NASA budget tier ---
        cutoff_n = now - NASA_BUDGET_WINDOW
        _nasa_budget_store[:] = [t for t in _nasa_budget_store if t > cutoff_n]
        if len(_nasa_budget_store) >= NASA_BUDGET:
            nasa_exhausted = True
            retry_after = max(retry_after, _nasa_budget_store[0] + NASA_BUDGET_WINDOW - now)

    blocked = retry_after > 0
    return (blocked, retry_after, nasa_exhausted)

def _track_nasa_call():
    """Record a NASA API call in the budget tracker."""
    with _rate_lock:
        _nasa_budget_store.append(time.time())

def _rate_info(ip):
    """Return dict of rate limit status for headers."""
    now = time.time()
    info = {'burst_limit': BURST_LIMIT, 'sustained_limit': SUSTAINED_LIMIT, 'global_limit': GLOBAL_LIMIT}
    with _rate_lock:
        ts_list = _rate_store.get(ip, [])
        cutoff_s = now - SUSTAINED_WINDOW
        ts_list = [t for t in ts_list if t > cutoff_s]
        info['sustained_remaining'] = max(0, SUSTAINED_LIMIT - len(ts_list))

        cutoff_b = now - BURST_WINDOW
        burst_count = sum(1 for t in ts_list if t > cutoff_b)
        info['burst_remaining'] = max(0, BURST_LIMIT - burst_count)

        cutoff_g = now - GLOBAL_WINDOW
        _global_store[:] = [t for t in _global_store if t > cutoff_g]
        info['global_remaining'] = max(0, GLOBAL_LIMIT - len(_global_store))

        cutoff_n = now - NASA_BUDGET_WINDOW
        _nasa_budget_store[:] = [t for t in _nasa_budget_store if t > cutoff_n]
        info['nasa_budget_remaining'] = max(0, NASA_BUDGET - len(_nasa_budget_store))
    return info

# ---------------------------------------------------------------------------
#  Input validation
# ---------------------------------------------------------------------------
DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')

def _validate_date(date_str):
    """Validate date format and range. Returns (date_str or None, error_msg or None)."""
    if not date_str:
        return (None, 'Date parameter is required.')
    if not DATE_RE.match(date_str):
        return (None, f'Invalid date format: "{date_str}". Use YYYY-MM-DD.')
    try:
        parsed = time.strptime(date_str, '%Y-%m-%d')
    except ValueError:
        return (None, f'Invalid date: "{date_str}".')
    today = time.strptime(time.strftime('%Y-%m-%d'), '%Y-%m-%d')
    if parsed > today:
        return (None, f'Date "{date_str}" is in the future. No data available.')
    if parsed < time.strptime('1995-06-16', '%Y-%m-%d'):
        return (None, f'Date "{date_str}" is before the APOD project started (1995-06-16).')
    return (date_str, None)

def _validate_api_params(body):
    """Validate incoming API request body. Returns (params_dict, error_response_or_None)."""
    if not body or not isinstance(body, dict):
        return (None, (400, {'error': 'Request body must be JSON.'}))

    if body.get('count') and body.get('date'):
        return (None, (400, {'error': 'Cannot request both "count" (random) and "date".'}))

    params = {}

    # Date param
    date_str = body.get('date')
    if date_str is not None:
        validated_date, err = _validate_date(date_str)
        if err:
            return (None, (400, {'error': err}))
        params['date'] = validated_date

    # Count param (random)
    count = body.get('count')
    if count is not None:
        try:
            c = int(count)
            if c < 1 or c > 10:
                return (None, (400, {'error': 'Count must be between 1 and 10.'}))
            params['count'] = str(c)
        except (ValueError, TypeError):
            return (None, (400, {'error': 'Count must be an integer.'}))

    params['thumbs'] = 'true'

    return (params, None)

# ---------------------------------------------------------------------------
#  Shared NASA fetch
# ---------------------------------------------------------------------------
def _fetch_apod(params, ip):
    """Returns (status, body_dict, headers_dict, cache_hit_bool)."""
    cache_key = hashlib.sha256(
        urllib.parse.urlencode(sorted(params.items())).encode()
    ).hexdigest()

    if 'count' not in params:
        date_param = params.get('date')
        if date_param:
            cached = _cache_get('date:' + date_param)
            if cached is not None:
                body_str, nasa_headers = cached
                return (200, json.loads(body_str), nasa_headers, True)

        cached = _cache_get(cache_key)
        if cached is not None:
            body_str, nasa_headers = cached
            return (200, json.loads(body_str), nasa_headers, True)

    if not NASA_API_KEY:
        return (500, {'error': 'NASA_API_KEY not set in .env'}, {}, False)

    # Check NASA budget before calling
    _, _, nasa_exhausted = _rate_limited(ip)
    if nasa_exhausted:
        remaining_secs = _rate_info(ip)  # approximate
        return (429, {
            'error': 'NASA API budget exhausted for this hour. Try again later.',
            'nasa_budget_reset_after': 3600
        }, {}, False)

    fetch_params = dict(params)
    fetch_params['api_key'] = NASA_API_KEY
    url = NASA_APOD_URL + '?' + urllib.parse.urlencode(fetch_params)

    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=15) as nasa_resp:
            body_str = nasa_resp.read().decode()
            status = nasa_resp.status

            _track_nasa_call()

            nasa_headers = {}
            for h in ('X-RateLimit-Limit', 'X-RateLimit-Remaining', 'X-RateLimit-Reset'):
                v = nasa_resp.headers.get(h)
                if v:
                    nasa_headers[h] = v

            if status == 200:
                if 'count' not in params:
                    _cache_put(cache_key, body_str, nasa_headers)
                try:
                    data = json.loads(body_str)
                    entries = data if isinstance(data, list) else [data]
                    for entry in entries:
                        if entry.get('date'):
                            _cache_put('date:' + entry['date'], json.dumps(entry), nasa_headers)
                except json.JSONDecodeError:
                    pass

            body_data = json.loads(body_str) if status == 200 else {'error': 'NASA request failed'}
            return (status, body_data, nasa_headers, False)

    except urllib.error.HTTPError as e:
        body_str = e.read().decode()
        try:
            return (e.code, json.loads(body_str), {}, False)
        except json.JSONDecodeError:
            return (e.code, {'error': body_str}, {}, False)
    except urllib.error.URLError:
        return (504, {'error': 'NASA API timed out. Try again.'}, {}, False)

def _build_rate_headers(rinfo):
    """Build rate-limit response headers from rate info dict."""
    return {
        'X-RateLimit-Burst-Limit': str(rinfo['burst_limit']),
        'X-RateLimit-Burst-Remaining': str(rinfo['burst_remaining']),
        'X-RateLimit-Limit': str(rinfo['sustained_limit']),
        'X-RateLimit-Remaining': str(rinfo['sustained_remaining']),
        'X-RateLimit-Global-Limit': str(rinfo['global_limit']),
        'X-RateLimit-Global-Remaining': str(rinfo['global_remaining']),
        'X-RateLimit-NASA-Budget-Remaining': str(rinfo['nasa_budget_remaining']),
    }

# ---------------------------------------------------------------------------
#  Routes
# ---------------------------------------------------------------------------

@app.route('/')
def index():
    ip = request.remote_addr or 'unknown'

    today = time.strftime('%Y-%m-%d')
    date_str = request.args.get('date', today)
    if date_str > today:
        date_str = today

    params = {'date': date_str, 'thumbs': 'true'}
    visitor_count = _record_visit(ip)

    blocked, retry_after, _ = _rate_limited(ip)
    if blocked:
        return render_template('index.html',
            apod=None,
            error=f'Rate limit exceeded. Try again in {max(1, int(retry_after))}s.',
            selected_date=date_str,
            today=today,
            visitor_count=visitor_count,
        ), 429

    status, data, nasa_headers, cache_hit = _fetch_apod(params, ip)

    if isinstance(data, list) and len(data) == 1:
        data = data[0]

    if status != 200:
        msg = data.get('msg') or data.get('error', {}).get('message') or 'No data for that date.'
        return render_template('index.html',
            apod=None,
            error=msg,
            selected_date=date_str,
            today=today,
            visitor_count=visitor_count,
        ), status

    nasa_remain = nasa_headers.get('X-RateLimit-Remaining', '?')
    nasa_limit = nasa_headers.get('X-RateLimit-Limit', '?')

    return render_template('index.html',
        apod=data,
        error=None,
        selected_date=date_str,
        today=today,
        cache_hit=cache_hit,
        nasa_remaining=nasa_remain,
        nasa_limit=nasa_limit,
        visitor_count=visitor_count,
    )


@app.after_request
def add_cors_headers(resp):
    resp.headers['Access-Control-Allow-Origin'] = '*'
    resp.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    resp.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    return resp

def _parse_apod_params():
    """Parse params from either POST body or GET query string."""
    if request.method == 'POST':
        body = request.get_json(silent=True)
        params, err = _validate_api_params(body)
        if err:
            return (None, err)
        return (params, None)

    # GET — parse query params (backward compat)
    raw = {}
    for key in ALLOWED_PARAMS:
        val = request.args.get(key)
        if val is not None:
            raw[key] = val
    raw['thumbs'] = 'true'
    # Validate date if present
    if 'date' in raw:
        validated, err_msg = _validate_date(raw['date'])
        if err_msg:
            return (None, (400, {'error': err_msg}))
        raw['date'] = validated
    if 'count' in raw and 'date' in raw:
        return (None, (400, {'error': 'Cannot request both "count" (random) and "date".'}))
    return (raw, None)

def _make_apod_response(ip, status, data, nasa_headers, cache_hit):
    """Build a rate-limited API response with all headers."""
    rinfo = _rate_info(ip)
    rate_headers = _build_rate_headers(rinfo)
    body_str = json.dumps(data)
    resp = Response(body_str, status=status, mimetype='application/json')
    for k, v in rate_headers.items():
        resp.headers[k] = v
    resp.headers['X-Cache'] = 'HIT' if cache_hit else 'MISS'
    if cache_hit:
        resp.headers['X-Cache-TTL'] = str(CACHE_TTL)
    if nasa_headers.get('X-RateLimit-Limit'):
        resp.headers['X-RateLimit-NASA-Limit'] = nasa_headers['X-RateLimit-Limit']
    if nasa_headers.get('X-RateLimit-Remaining'):
        resp.headers['X-RateLimit-NASA-Remaining'] = nasa_headers['X-RateLimit-Remaining']
    if nasa_headers.get('X-RateLimit-Reset'):
        resp.headers['X-RateLimit-NASA-Reset'] = nasa_headers['X-RateLimit-Reset']
    return resp

@app.route('/api/apod', methods=['GET', 'POST', 'OPTIONS'])
def proxy_apod():
    ip = request.remote_addr or 'unknown'

    # Handle CORS preflight
    if request.method == 'OPTIONS':
        return Response(status=204)

    # --- Parse params ---
    params, err = _parse_apod_params()
    if err:
        status_code, err_body = err
        return Response(json.dumps(err_body), status=status_code, mimetype='application/json')

    # --- Rate limit ---
    blocked, retry_after, nasa_exhausted = _rate_limited(ip)

    if blocked:
        msg = 'Too many requests. '
        if nasa_exhausted:
            msg += 'NASA API budget exhausted for this hour. Try again later.'
        else:
            msg += f'Try again in {max(1, int(retry_after))}s.'
        resp = Response(
            json.dumps({'error': msg, 'retry_after_seconds': int(retry_after)}),
            status=429, mimetype='application/json',
        )
        rinfo = _rate_info(ip)
        for k, v in _build_rate_headers(rinfo).items():
            resp.headers[k] = v
        resp.headers['Retry-After'] = str(max(1, int(retry_after)))
        return resp

    # --- Fetch ---
    status, data, nasa_headers, cache_hit = _fetch_apod(params, ip)
    return _make_apod_response(ip, status, data, nasa_headers, cache_hit)


@app.route('/api/status', methods=['GET'])
def api_status():
    ip = request.remote_addr or 'unknown'
    rinfo = _rate_info(ip)
    resp = Response(
        json.dumps({
            'ok': True,
            'rate_limits': {
                'burst': {'limit': rinfo['burst_limit'], 'remaining': rinfo['burst_remaining']},
                'sustained': {'limit': rinfo['sustained_limit'], 'remaining': rinfo['sustained_remaining']},
                'global': {'limit': rinfo['global_limit'], 'remaining': rinfo['global_remaining']},
                'nasa_budget': {'limit': 900, 'remaining': rinfo['nasa_budget_remaining']},
            }
        }),
        status=200, mimetype='application/json',
    )
    for k, v in _build_rate_headers(rinfo).items():
        resp.headers[k] = v
    return resp


@app.route('/<path:filename>')
def static_files(filename):
    if filename.startswith('api/'):
        return ('Not found', 404)
    return send_from_directory('.', filename)


if __name__ == '__main__':
    if SUPABASE_ENABLED:
        print('Supabase: connected')
    else:
        print('Supabase: not configured (set SUPABASE_URL + SUPABASE_ANON_KEY)')
    port = int(os.environ.get('PORT', 8000))
    print(f'Server running at http://localhost:{port}')
    app.run(host='0.0.0.0', port=port, debug=True)
