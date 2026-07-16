import hashlib
import os
import sqlite3
import sys
import time
import requests
import re
from datetime import datetime, timezone

# --- source repo (owner/name/branch) whose files we're polling ---
SOURCE_REPO   = "pageth/Vol2VolData"
SOURCE_BRANCH = "main"
FILES = {
    "intraday": "IntradayData.txt",
    "oi":       "OIData.txt",
}

def api_url(path: str) -> str:
    return f"https://api.github.com/repos/{SOURCE_REPO}/contents/{path}?ref={SOURCE_BRANCH}"

URLS = {source: api_url(path) for source, path in FILES.items()}

# GitHub Actions injects this automatically as secrets.GITHUB_TOKEN.
# It's plenty for reading a public repo's contents via the API.
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")

HEADERS_BASE = {
    "Accept": "application/vnd.github.raw+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "itd-oi-db-fetcher (+https://github.com/0xtrvkc/itd-oi-db)",
}
if GITHUB_TOKEN:
    HEADERS_BASE["Authorization"] = f"Bearer {GITHUB_TOKEN}"

DB_PATH = "data/vol2vol.db"
MAX_RETRIES = 3

# Both IntradayData.txt and OIData.txt share the exact same layout:
#   line 0: "<symbol> (<dte> DTE) vs <price> (<chg>) - <label>"
#   line 1: "Put: N  Call: N  Vol: N  Vol Chg: N  Future Chg: N"
#   line 2: "Strike,Call,Put,Vol Settle"
#   line 3+: "<strike>,<call>,<put>,<vol_settle>"
# so both sources are parsed with the same function and stored in
# same-shaped tables ("intraday" and "oi").
SNAPSHOT_TABLE_SCHEMA = """
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    fetched_at   DATETIME NOT NULL,
    symbol       TEXT,
    dte          REAL,
    future_price REAL,
    future_chg   REAL,
    put_oi       INTEGER,
    call_oi      INTEGER,
    vol          REAL,
    vol_chg      REAL,
    strike       INTEGER,
    call         INTEGER,
    put          INTEGER,
    vol_settle   REAL
"""


def init_db(conn):
    conn.executescript(f"""
        CREATE TABLE IF NOT EXISTS intraday ({SNAPSHOT_TABLE_SCHEMA});
        CREATE TABLE IF NOT EXISTS oi ({SNAPSHOT_TABLE_SCHEMA});
        CREATE INDEX IF NOT EXISTS idx_intraday_fetched ON intraday(fetched_at);
        CREATE INDEX IF NOT EXISTS idx_oi_fetched ON oi(fetched_at);

        CREATE TABLE IF NOT EXISTS fetch_log (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            source       TEXT,
            content_hash TEXT,
            fetched_at   DATETIME,
            was_new      INTEGER
        );

        CREATE TABLE IF NOT EXISTS etag_cache (
            source TEXT PRIMARY KEY,
            etag   TEXT
        );
    """)
    conn.commit()


def get_cached_etag(conn, source: str):
    row = conn.execute(
        "SELECT etag FROM etag_cache WHERE source=?", (source,)
    ).fetchone()
    return row[0] if row else None


def set_cached_etag(conn, source: str, etag: str):
    conn.execute(
        "INSERT INTO etag_cache (source, etag) VALUES (?, ?) "
        "ON CONFLICT(source) DO UPDATE SET etag=excluded.etag",
        (source, etag),
    )
    conn.commit()


def get_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def get_last_hash(conn, source: str):
    row = conn.execute(
        "SELECT content_hash FROM fetch_log WHERE source=? ORDER BY fetched_at DESC LIMIT 1",
        (source,)
    ).fetchone()
    return row[0] if row else None


def parse_snapshot(text: str) -> dict:
    """Parses IntradayData.txt / OIData.txt — identical format for both."""
    lines = text.strip().splitlines()
    header = lines[0]
    meta   = lines[1]

    dte_match   = re.search(r'\(([\d.]+) DTE\)', header)
    price_match = re.search(r'vs ([\d.]+)', header)
    chg_match   = re.search(r'\(([-\d.]+)\)', header.split('vs')[1]) if 'vs' in header else None
    put_oi      = int(re.search(r'Put:\s*([\d,]+)', meta).group(1).replace(',', ''))
    call_oi     = int(re.search(r'Call:\s*([\d,]+)', meta).group(1).replace(',', ''))
    vol         = float(re.search(r'Vol:\s*([\d.]+)', meta).group(1))
    vol_chg     = float(re.search(r'Vol Chg:\s*([-\d.]+)', meta).group(1))
    future_chg  = float(chg_match.group(1)) if chg_match else 0.0

    rows = []
    for line in lines[3:]:
        parts = line.split(',')
        if len(parts) == 4:
            try:
                rows.append({
                    "strike":     int(parts[0]),
                    "call":       int(parts[1]),
                    "put":        int(parts[2]),
                    "vol_settle": float(parts[3]) if parts[3].strip() else None,
                })
            except ValueError:
                continue

    return {
        "symbol":       header.split('(')[0].strip(),
        "dte":          float(dte_match.group(1)) if dte_match else None,
        "future_price": float(price_match.group(1)) if price_match else None,
        "future_chg":   future_chg,
        "put_oi":       put_oi,
        "call_oi":      call_oi,
        "vol":          vol,
        "vol_chg":      vol_chg,
        "rows":         rows,
    }


class NotModified(Exception):
    """Raised when the server confirms nothing changed (HTTP 304)."""


def fetch_fresh(url: str, etag: str | None) -> tuple[str, str | None]:
    """
    Fetches a file via the authenticated GitHub Contents API using a
    conditional request (If-None-Match). This replaces the old approach of
    appending a random cache-busting query param to defeat CDN caching.

    Why: a unique URL on every request, fired every few minutes from CI
    runner IPs, is exactly the pattern GitHub's abuse-detection flags as
    scraping -> 429. A conditional request is the opposite signal: it's a
    well-behaved client that mostly gets back a cheap "304 Not Modified"
    instead of re-downloading the file, and it runs against the API's much
    higher authenticated rate limit rather than the raw CDN.

    Returns (text, new_etag). Raises NotModified if the content is unchanged.
    """
    headers = dict(HEADERS_BASE)
    if etag:
        headers["If-None-Match"] = etag

    last_exc = None
    for attempt in range(1, MAX_RETRIES + 1):
        resp = requests.get(url, headers=headers, timeout=10)

        if resp.status_code == 304:
            raise NotModified()

        if resp.status_code == 200:
            return resp.text, resp.headers.get("ETag")

        if resp.status_code == 403 or resp.status_code == 429:
            # Rate limited (or momentarily blocked). Respect whatever the
            # server tells us to wait, falling back to simple backoff.
            retry_after = resp.headers.get("Retry-After")
            reset = resp.headers.get("X-RateLimit-Reset")
            if retry_after:
                wait = float(retry_after)
            elif reset:
                wait = max(0.0, float(reset) - time.time())
            else:
                wait = 2 ** attempt  # 2s, 4s, 8s
            print(f"  rate limited (attempt {attempt}/{MAX_RETRIES}), "
                  f"waiting {wait:.0f}s", file=sys.stderr)
            time.sleep(min(wait, 60))
            last_exc = requests.HTTPError(f"{resp.status_code} on {url}")
            continue

        # Any other error (5xx etc.) - short backoff and retry.
        if 500 <= resp.status_code < 600:
            wait = 2 ** attempt
            print(f"  server error {resp.status_code} (attempt "
                  f"{attempt}/{MAX_RETRIES}), waiting {wait}s", file=sys.stderr)
            time.sleep(wait)
            last_exc = requests.HTTPError(f"{resp.status_code} on {url}")
            continue

        resp.raise_for_status()  # anything unexpected -> raise immediately

    raise last_exc or RuntimeError(f"failed to fetch {url}")


def store_snapshot(conn, table: str, now: str, parsed: dict):
    for row in parsed["rows"]:
        conn.execute(f"""
            INSERT INTO {table}
            (fetched_at, symbol, dte, future_price, future_chg,
             put_oi, call_oi, vol, vol_chg, strike, call, put, vol_settle)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            now, parsed["symbol"], parsed["dte"], parsed["future_price"],
            parsed["future_chg"], parsed["put_oi"], parsed["call_oi"],
            parsed["vol"], parsed["vol_chg"],
            row["strike"], row["call"], row["put"], row["vol_settle"]
        ))


def run():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    init_db(conn)

    now = datetime.now(timezone.utc).isoformat()

    # source -> destination table (both parsed identically)
    TABLES = {"intraday": "intraday", "oi": "oi"}

    for source, url in URLS.items():
        cached_etag = get_cached_etag(conn, source)
        try:
            text, new_etag = fetch_fresh(url, cached_etag)
        except NotModified:
            print(f"[{source}] 304 NOT MODIFIED — skipped (no download needed)")
            continue
        except Exception as e:
            print(f"[{source}] FETCH ERROR: {e}")
            continue

        if new_etag:
            set_cached_etag(conn, source, new_etag)

        h    = get_hash(text)
        last = get_last_hash(conn, source)
        is_new = h != last

        conn.execute(
            "INSERT INTO fetch_log(source, content_hash, fetched_at, was_new) VALUES (?,?,?,?)",
            (source, h, now, int(is_new))
        )

        if not is_new:
            print(f"[{source}] SAME — skipped")
            conn.commit()
            continue

        try:
            parsed = parse_snapshot(text)
        except Exception as e:
            print(f"[{source}] PARSE ERROR: {e}")
            conn.commit()
            continue

        table = TABLES[source]
        store_snapshot(conn, table, now, parsed)
        print(f"[{source}] NEW — inserted {len(parsed['rows'])} rows into {table}")

        conn.commit()

    # sql.js-httpvfs in the browser only fetches this one file — never the
    # companion .db-wal file — so make sure everything is flushed into it
    # and the DB isn't left in WAL mode before we publish it.
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.commit()

    conn.close()


if __name__ == "__main__":
    run()
