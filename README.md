# vol2vol ✿ snapshot log

live: https://0xtrvkc.github.io/itd-oi-db/

Fetches `IntradayData.txt` and `OIData.txt` from [pageth/Vol2VolData](https://github.com/pageth/Vol2VolData) every 5 minutes and stores new data in a SQLite database.

options flow snapshot viewer. reads a SQLite db, shows intraday OI / vol / pc-ratio drift in a table.

## how it works

script pushes `data/vol2vol.db` → this repo. `index.html` auto-fetches the raw file from GitHub on load, parses it in-browser via [sql.js](https://github.com/sql-js/sql.js), renders the table. no backend, no build step.

## repo structure

```
data/vol2vol.db   ← sqlite, updated by external script
index.html        ← the whole app, single file
```

## db schema

expects a table called `intraday` with these columns:

| column | type |
|---|---|
| fetched_at | ISO timestamp (UTC) |
| symbol | text |
| dte | float |
| future_price | float |
| future_chg | float |
| put_oi | int |
| call_oi | int |
| vol | float |
| vol_chg | float |

## local use

just open `index.html` in a browser. drag-and-drop a `.db` onto the page if you want to load a local file instead.

## update flow

```
your script → git push data/vol2vol.db → refresh page → done
```

no cache headers set on raw.githubusercontent.com so hard-refresh (`Ctrl+Shift+R`) if you're not seeing latest data.
