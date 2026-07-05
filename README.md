# NAPOD — NASA APOD Viewer

Vanilla HTML/CSS/JS viewer for the [NASA Astronomy Picture of the Day](https://apod.nasa.gov) API.

## Setup

1. **Get an API key** — sign up free at https://api.nasa.gov
2. Copy `.env.example` to `.env` and add your key:

   ```
   NASA_API_KEY=your_key_here
   ```

## Run locally (Flask)

```bash
pip install -r requirements.txt
python server.py
```

Open `http://localhost:5000`.

> Opening `index.html` directly via `file://` will **not** work — the frontend needs the Flask server for API proxying.

## Deploy to Vercel (alternative)

```bash
npm i -g vercel
vercel --prod
```

Set `NASA_API_KEY` as a Vercel Environment Variable.

## Configuration

All env vars with their defaults:

| Variable | Default | Description |
|----------|---------|-------------|
| `NASA_API_KEY` | — | Your NASA API key (required) |
| `CACHE_TTL` | `3600` | Server-side cache duration (seconds) |
| `CACHE_MAX` | `200` | Max cached entries (LRU eviction) |
| `RATE_LIMIT` | `60` | Max requests per IP per window |
| `RATE_WINDOW` | `60` | Rate-limit window in seconds |

## Architecture

```
index.html       — single-file frontend (vanilla JS)
server.py        — Flask server (serves static files + API proxy with cache + rate limiting)
api/apod.js      — Vercel serverless function (alternative deployment)
.env.example     — env template
requirements.txt — Python deps
```

## Features

- Date carousel with keyboard navigation (← →)
- Swipe gesture support on mobile
- Full-resolution HD image link
- Direct link to official NASA APOD page
- Video fallback link for non-embeddable content
- Share/copy link to any day
- **Server-side LRU cache** with configurable TTL — repeated requests skip NASA
- **Per-IP rate limiter** — sliding window, configurable limit
- **Rate limit display** — shows NASA + proxy remaining in the header
- **Cache indicator** — shows when response was served from cache
- localStorage caching (24h TTL) for offline resilience
- 10s request timeout with friendly error messages
- Rate-limit and invalid-date error mapping
- Starfield animated canvas background
