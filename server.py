import os
import json
import time
import hashlib
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
ALLOWED_PARAMS = frozenset({'date', 'count', 'thumbs', 'start_date', 'end_date'})

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
    """Low-level Supabase REST call. Returns (status, body_dict) or raises."""
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
_VISITOR_CACHE = {}       # local cache: ip_hash -> timestamp (debounce writes)
_VISITOR_LOCK = Lock()
VISITOR_DEBOUNCE = 3600   # only write to Supabase once per hour per IP

def _record_visit(ip):
    """Record visit if new IP (24h window). Returns current total count or None."""
    if not SUPABASE_ENABLED:
        return None

    ip_hash = hashlib.sha256(ip.encode()).hexdigest()
    now_ts = time.time()

    # Debounce: skip if we've seen this IP recently
    with _VISITOR_LOCK:
        last = _VISITOR_CACHE.get(ip_hash)
        if last and (now_ts - last) < VISITOR_DEBOUNCE:
            # Still return count from cache if we can
            pass
        _VISITOR_CACHE[ip_hash] = now_ts

    try:
        # Check if IP visited in last 24h
        cutoff = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now_ts - 86400))
        status, rows = _supa_req('GET', f'visits?ip_hash=eq.{ip_hash}&visited_at=gt.{cutoff}&select=id&limit=1')
        if status == 200 and len(rows) == 0:
            # New visitor — insert and bump count
            _supa_req('POST', 'visits', {'ip_hash': ip_hash, 'visited_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())})
            # Read current count, increment, write
            st_r, rows_r = _supa_req('GET', 'visit_count?id=eq.1&select=count')
            if st_r == 200 and rows_r:
                new_count = rows_r[0].get('count', 0) + 1
                _supa_req('PATCH', 'visit_count?id=eq.1', {'count': new_count})
        # Return current count
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

# In-memory fallback when Supabase is unavailable
_fallback_cache = OrderedDict()
_fallback_lock = Lock()
FALLBACK_MAX = 200

def _cache_get(key):
    """Try Supabase first, then fallback memory."""
    if SUPABASE_ENABLED:
        try:
            status, rows = _supa_req('GET', f'cache?key=eq.{urllib.parse.quote(key, safe="")}&select=value,expires_at&limit=1')
            if status == 200 and rows:
                expires = rows[0].get('expires_at', '')
                if expires and expires < time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()):
                    # Expired — delete and fall through
                    try:
                        _supa_req('DELETE', f'cache?key=eq.{urllib.parse.quote(key, safe="")}')
                    except RuntimeError:
                        pass
                else:
                    return rows[0]['value'], {}
            # Not found or expired
        except RuntimeError:
            pass  # fall through to memory cache
        except Exception:
            pass

    # In-memory fallback
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
    """Store in Supabase (and memory fallback)."""
    if SUPABASE_ENABLED:
        expires = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(time.time() + CACHE_TTL))
        body = {'key': key, 'value': value, 'expires_at': expires}
        try:
            _supa_req('POST', 'cache', body)
        except RuntimeError:
            pass  # fall through to memory
        except Exception:
            pass

    # Always write to memory fallback too
    with _fallback_lock:
        if key in _fallback_cache:
            _fallback_cache.move_to_end(key)
        _fallback_cache[key] = (value, time.time())
        while len(_fallback_cache) > FALLBACK_MAX:
            _fallback_cache.popitem(last=False)

# ---------------------------------------------------------------------------
#  Rate limiter — sliding-window per IP
# ---------------------------------------------------------------------------
RATE_LIMIT = int(os.environ.get('RATE_LIMIT', '60'))
RATE_WINDOW = int(os.environ.get('RATE_WINDOW', '60'))

_rate_store = {}
_rate_lock = Lock()

def _rate_limited(ip):
    now = time.time()
    with _rate_lock:
        ts_list = _rate_store.get(ip, [])
        cutoff = now - RATE_WINDOW
        ts_list = [t for t in ts_list if t > cutoff]
        if len(ts_list) >= RATE_LIMIT:
            _rate_store[ip] = ts_list
            return True
        ts_list.append(now)
        _rate_store[ip] = ts_list
        return False

def _rate_remaining(ip):
    now = time.time()
    with _rate_lock:
        ts_list = _rate_store.get(ip, [])
        cutoff = now - RATE_WINDOW
        ts_list = [t for t in ts_list if t > cutoff]
        _rate_store[ip] = ts_list
        return max(0, RATE_LIMIT - len(ts_list))

# ---------------------------------------------------------------------------
#  Shared NASA fetch
# ---------------------------------------------------------------------------
def _fetch_apod(params, ip):
    """Returns (status, body_dict, headers_dict, cache_hit_bool)."""
    cache_key = hashlib.sha256(
        urllib.parse.urlencode(sorted(params.items())).encode()
    ).hexdigest()

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

    fetch_params = dict(params)
    fetch_params['api_key'] = NASA_API_KEY
    url = NASA_APOD_URL + '?' + urllib.parse.urlencode(fetch_params)

    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=15) as nasa_resp:
            body_str = nasa_resp.read().decode()
            status = nasa_resp.status

            nasa_headers = {}
            for h in ('X-RateLimit-Limit', 'X-RateLimit-Remaining', 'X-RateLimit-Reset'):
                v = nasa_resp.headers.get(h)
                if v:
                    nasa_headers[h] = v

            if status == 200:
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

    # Record visit (non-blocking — fine if it fails)
    visitor_count = _record_visit(ip)

    if _rate_limited(ip):
        return render_template('index.html',
            apod=None,
            error='Rate limit exceeded. Try again later.',
            selected_date=date_str,
            today=today,
            rate_remaining=_rate_remaining(ip),
            rate_limit=RATE_LIMIT,
            visitor_count=visitor_count,
        )

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
            rate_remaining=_rate_remaining(ip),
            rate_limit=RATE_LIMIT,
            visitor_count=visitor_count,
        )

    nasa_remain = nasa_headers.get('X-RateLimit-Remaining', '?')
    nasa_limit = nasa_headers.get('X-RateLimit-Limit', '?')

    return render_template('index.html',
        apod=data,
        error=None,
        selected_date=date_str,
        today=today,
        cache_hit=cache_hit,
        rate_remaining=_rate_remaining(ip),
        rate_limit=RATE_LIMIT,
        nasa_remaining=nasa_remain,
        nasa_limit=nasa_limit,
        visitor_count=visitor_count,
    )


@app.route('/api/apod')
def proxy_apod():
    ip = request.remote_addr or 'unknown'

    if _rate_limited(ip):
        remaining = _rate_remaining(ip)
        resp = Response(
            json.dumps({'error': f'Rate limit exceeded. Try again in {RATE_WINDOW}s.'}),
            status=429,
            mimetype='application/json',
        )
        resp.headers['X-RateLimit-Limit'] = str(RATE_LIMIT)
        resp.headers['X-RateLimit-Remaining'] = str(remaining)
        resp.headers['X-RateLimit-Reset-After'] = str(RATE_WINDOW)
        resp.headers['Retry-After'] = str(RATE_WINDOW)
        return resp

    params = {}
    for key in ALLOWED_PARAMS:
        val = request.args.get(key)
        if val is not None:
            params[key] = val

    status, data, nasa_headers, cache_hit = _fetch_apod(params, ip)

    body_str = json.dumps(data)
    resp = Response(body_str, status=status, mimetype='application/json')
    resp.headers['X-Cache'] = 'HIT' if cache_hit else 'MISS'
    resp.headers['X-RateLimit-Local-Limit'] = str(RATE_LIMIT)
    resp.headers['X-RateLimit-Local-Remaining'] = str(_rate_remaining(ip))
    if nasa_headers.get('X-RateLimit-Limit'):
        resp.headers['X-RateLimit-Limit'] = nasa_headers['X-RateLimit-Limit']
    if nasa_headers.get('X-RateLimit-Remaining'):
        resp.headers['X-RateLimit-Remaining'] = nasa_headers['X-RateLimit-Remaining']
    if cache_hit:
        resp.headers['X-Cache-TTL'] = str(CACHE_TTL)
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
    port = int(os.environ.get('PORT', 5000))
    print(f'Server running at http://localhost:{port}')
    app.run(host='0.0.0.0', port=port, debug=True)
