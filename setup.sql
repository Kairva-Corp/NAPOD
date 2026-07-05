-- Run this in your Supabase SQL editor to create required tables.
-- Then set SUPABASE_URL and SUPABASE_ANON_KEY in .env.

-- Visitor count (single row)
CREATE TABLE IF NOT EXISTS visit_count (
  id INTEGER PRIMARY KEY DEFAULT 1,
  count INTEGER NOT NULL DEFAULT 0
);
INSERT INTO visit_count (id, count) VALUES (1, 0) ON CONFLICT (id) DO NOTHING;

-- Unique visitor log (by IP hash)
CREATE TABLE IF NOT EXISTS visits (
  id SERIAL PRIMARY KEY,
  ip_hash TEXT NOT NULL,
  visited_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_visits_ip_hash ON visits (ip_hash);
CREATE INDEX IF NOT EXISTS idx_visits_visited_at ON visits (visited_at);

-- Cache store
CREATE TABLE IF NOT EXISTS cache (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  expires_at TIMESTAMPTZ NOT NULL
);

-- IMPORTANT: Enable Row Level Security and grant anon access
ALTER TABLE visit_count ENABLE ROW LEVEL SECURITY;
ALTER TABLE visits ENABLE ROW LEVEL SECURITY;
ALTER TABLE cache ENABLE ROW LEVEL SECURITY;

-- Allow anon key to read/write these tables
CREATE POLICY "anon_all_visit_count" ON visit_count FOR ALL TO anon USING (true) WITH CHECK (true);
CREATE POLICY "anon_all_visits" ON visits FOR ALL TO anon USING (true) WITH CHECK (true);
CREATE POLICY "anon_all_cache" ON cache FOR ALL TO anon USING (true) WITH CHECK (true);
