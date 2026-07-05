// Vercel serverless function — proxies NASA APOD API so the key stays server-side.
// Deploys automatically when placed in api/ — https://vercel.com/docs/functions

export default async function handler(req, res) {
  // CORS — allow any origin (the frontend is a separate static deploy or file)
  const origin = req.headers.origin || '*';
  res.setHeader('Access-Control-Allow-Origin', origin);
  res.setHeader('Access-Control-Allow-Methods', 'GET, OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type');

  if (req.method === 'OPTIONS') {
    return res.status(200).end();
  }

  const apiKey = process.env.NASA_API_KEY;
  if (!apiKey) {
    return res.status(500).json({ error: 'NASA_API_KEY not configured on server.' });
  }

  // Whitelist of params forwarded to NASA
  const allowed = ['date', 'count', 'thumbs', 'start_date', 'end_date'];
  const params = new URLSearchParams();
  params.set('api_key', apiKey);
  for (const key of allowed) {
    const val = req.query[key];
    if (val !== undefined) params.set(key, val);
  }

  const nasaUrl = `https://api.nasa.gov/planetary/apod?${params.toString()}`;

  try {
    const resp = await fetch(nasaUrl, { signal: AbortSignal.timeout(15_000) });
    const body = await resp.json();
    return res.status(resp.status).json(body);
  } catch (err) {
    if (err.name === 'TimeoutError') {
      return res.status(504).json({ error: 'NASA API timed out. Try again.' });
    }
    return res.status(502).json({ error: 'Upstream request failed.' });
  }
}
