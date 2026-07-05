// Vercel serverless function — proxies NASA APOD API so the key stays server-side.
// Supports both GET and POST.

export default async function handler(req, res) {
  const origin = req.headers.origin || '*';
  res.setHeader('Access-Control-Allow-Origin', origin);
  res.setHeader('Access-Control-Allow-Methods', 'GET, POST, OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type');

  if (req.method === 'OPTIONS') {
    return res.status(204).end();
  }

  const apiKey = process.env.NASA_API_KEY;
  if (!apiKey) {
    return res.status(500).json({ error: 'NASA_API_KEY not configured on server.' });
  }

  // Parse params from either POST body or GET query
  const allowed = ['date', 'count', 'thumbs', 'start_date', 'end_date'];
  const params = new URLSearchParams();
  params.set('api_key', apiKey);

  let sourceParams;
  if (req.method === 'POST') {
    if (!req.body || typeof req.body !== 'object') {
      return res.status(400).json({ error: 'Request body must be JSON.' });
    }
    sourceParams = req.body;
  } else {
    sourceParams = req.query;
  }

  for (const key of allowed) {
    const val = sourceParams[key];
    if (val !== undefined) params.set(key, val);
  }

  // Validate date if present
  if (params.has('date')) {
    const dateStr = params.get('date');
    if (!/^\d{4}-\d{2}-\d{2}$/.test(dateStr)) {
      return res.status(400).json({ error: `Invalid date format: "${dateStr}". Use YYYY-MM-DD.` });
    }
    const parsed = new Date(dateStr);
    if (isNaN(parsed.getTime())) {
      return res.status(400).json({ error: `Invalid date: "${dateStr}".` });
    }
    const today = new Date();
    today.setHours(23, 59, 59, 999);
    if (parsed > today) {
      return res.status(400).json({ error: `Date "${dateStr}" is in the future. No data available.` });
    }
    const apodStart = new Date('1995-06-16');
    if (parsed < apodStart) {
      return res.status(400).json({ error: `Date "${dateStr}" is before the APOD project started (1995-06-16).` });
    }
  }

  // Validate count
  if (params.has('count') && params.has('date')) {
    return res.status(400).json({ error: 'Cannot request both "count" (random) and "date".' });
  }

  const nasaUrl = `https://api.nasa.gov/planetary/apod?${params.toString()}`;

  try {
    const resp = await fetch(nasaUrl, { signal: AbortSignal.timeout(15_000) });
    const body = await resp.json();

    // If NASA returns a list (count response), return single item for client
    if (Array.isArray(body) && body.length === 1) {
      return res.status(resp.status).json(body[0]);
    }

    return res.status(resp.status).json(body);
  } catch (err) {
    if (err.name === 'TimeoutError') {
      return res.status(504).json({ error: 'NASA API timed out. Try again.' });
    }
    return res.status(502).json({ error: 'Upstream request failed.' });
  }
}
