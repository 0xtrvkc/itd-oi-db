# Vol2Vol Data Collector

Fetches `IntradayData.txt` and `OIData.txt` from [pageth/Vol2VolData](https://github.com/pageth/Vol2VolData) every 5 minutes and stores new data in a SQLite database.

## How it works

1. GitHub Actions runs every 5 minutes
2. Fetches both data files
3. Hashes the content — if identical to last fetch, skips
4. If new data detected, parses and inserts rows into `data/vol2vol.db`
5. Commits the updated `.db` file back to this repo

## Setup

1. Create a new GitHub repo
2. Upload all these files (keep the folder structure)
3. Go to **Settings → Actions → General** → set to "Allow all actions"
4. Go to **Actions** tab → click **Run workflow** to test manually
5. Check that `data/vol2vol.db` appears after the run

## File structure

```
.github/workflows/fetch_data.yml   # cron job
scripts/fetch_and_store.py         # all logic
data/vol2vol.db                    # auto-created on first run
requirements.txt
```

## Database tables

- `intraday` — parsed strike/call/put/vol data from IntradayData.txt
- `oi_data` — raw lines from OIData.txt
- `fetch_log` — audit log of every fetch (hash, timestamp, was_new)
