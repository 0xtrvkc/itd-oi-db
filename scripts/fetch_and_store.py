import hashlib
import sqlite3
import requests
import re
from datetime import datetime, timezone

URLS = {
    "intraday": "https://raw.githubusercontent.com/pageth/Vol2VolData/main/IntradayData.txt",
    "oi":       "https://raw.githubusercontent.com/pageth/Vol2VolData/main/OIData.txt",
}
DB_PATH = "data/vol2vol.db"


def init_db(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS intraday (
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
        );

        CREATE TABLE IF NOT EXISTS oi_data (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            fetched_at   DATETIME NOT NULL,
            raw_line     TEXT
        );

        CREATE TABLE IF NOT EXISTS fetch_log (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            source       TEXT,
            content_hash TEXT,
            fetched_at   DATETIME,
            was_new      INTEGER
        );
    """)
    conn.commit()


def get_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def get_last_hash(conn, source: str):
    row = conn.execute(
        "SELECT content_hash FROM fetch_log WHERE source=? ORDER BY fetched_at DESC LIMIT 1",
        (source,)
    ).fetchone()
    return row[0] if row else None


def parse_intraday(text: str) -> dict:
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


def fetch_fresh(url: str) -> str:
    """
    GitHub's raw.githubusercontent.com is fronted by a CDN (Fastly) that can
    serve a cached copy of the file for a while after the origin content has
    changed. A plain requests.get() can therefore return stale bytes, which
    makes our content-hash dedup falsely think "nothing changed" even when
    the upstream source has moved on.

    We defeat this by:
      1. Appending a unique cache-busting query param, so the CDN treats
         each request as a distinct URL (cache miss).
      2. Sending no-cache headers as a secondary precaution.
    """
    resp = requests.get(
        url,
        params={"t": int(datetime.now(timezone.utc).timestamp() * 1000)},
        headers={"Cache-Control": "no-cache", "Pragma": "no-cache"},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.text


def run():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    init_db(conn)

    now = datetime.now(timezone.utc).isoformat()

    for source, url in URLS.items():
        try:
            text = fetch_fresh(url)
        except Exception as e:
            print(f"[{source}] FETCH ERROR: {e}")
            continue

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

        if source == "intraday":
            parsed = parse_intraday(text)
            for row in parsed["rows"]:
                conn.execute("""
                    INSERT INTO intraday
                    (fetched_at, symbol, dte, future_price, future_chg,
                     put_oi, call_oi, vol, vol_chg, strike, call, put, vol_settle)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    now, parsed["symbol"], parsed["dte"], parsed["future_price"],
                    parsed["future_chg"], parsed["put_oi"], parsed["call_oi"],
                    parsed["vol"], parsed["vol_chg"],
                    row["strike"], row["call"], row["put"], row["vol_settle"]
                ))
            print(f"[{source}] NEW — inserted {len(parsed['rows'])} rows")

        elif source == "oi":
            # Store raw lines until OIData structure is confirmed
            for line in text.strip().splitlines():
                conn.execute(
                    "INSERT INTO oi_data(fetched_at, raw_line) VALUES (?,?)",
                    (now, line)
                )
            print(f"[{source}] NEW — stored raw OI data")

        conn.commit()

    conn.close()


if __name__ == "__main__":
    run()
