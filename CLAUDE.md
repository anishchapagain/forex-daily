# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Setup

```bash
pip install requests beautifulsoup4 python-dateutil tabulate
```

## Running

```bash
python main.py --csv usd_rates.csv
```

Omit `--csv` to print results to stdout only. For scheduled daily execution:

```bash
5 9 * * * /usr/bin/python3 /opt/fx/main.py --csv /opt/fx/output/usd_rates.csv
```

## Architecture

Single-file application (`main.py`) that scrapes daily USD exchange rates for 14 currency pairs.

**Data flow:**

1. `CORRIDORS` dict defines each currency pair with an ordered list of `(parser_type, url)` sources
2. `collect_all()` iterates currencies; for each, tries sources in order until one succeeds
3. `collect_one(quote, source)` fetches the URL and dispatches to the appropriate parser
4. Each parser (`parse_xe`, `parse_netdania`, `parse_investing`, `parse_ibrlive`, `parse_sampath`) extracts a rate + timestamp using BeautifulSoup + regex
5. Results are validated — only rates with today's timestamp are accepted
6. Output goes to `tabulate` (stdout) and optionally `write_csv()`

**Result dataclass** captures: `date`, `base_currency`, `quote_currency`, `rate`, `source_used`, `source_timestamp`, `status`. Status is `"OK"` on success or `"NO_DATA"` if all sources fail.

**Sources:**
- `xe` — xe.com (default fallback for most currencies)
- `netdania` — netdania.com (AUD, NZD, SGD)
- `investing` — investing.com (PHP, VND)
- `ibrlive` — ibrlive.com (INR)
- `sampath` — sampath.lk (LKR)

`reuters` and `fedan` entries in `CORRIDORS` are placeholders — parsers not yet implemented.

## Extending

To add a new currency or source: add an entry to `CORRIDORS` and, if using a new site, write a `parse_<site>(html)` function that returns `(rate_str, timestamp_str)` or raises on failure. Timestamp validation (must be today's date) is handled in `collect_one()`.
