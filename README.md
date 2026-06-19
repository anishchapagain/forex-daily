# Forex-Daily

A lightweight Python tool that scrapes live USD exchange rates for 14 Asian and international currency pairs from multiple public web sources. Runs on demand or on a configurable schedule, writing timestamped CSV snapshots to disk.

---

## Table of Contents

- [Overview](#overview)
- [Supported Currency Pairs](#supported-currency-pairs)
- [Data Sources](#data-sources)
- [Project Structure](#project-structure)
- [Prerequisites](#prerequisites)
- [Installation](#installation)
- [Usage](#usage)
  - [One-shot run](#one-shot-run)
  - [Scheduled runs](#scheduled-runs)
- [Configuration](#configuration)
- [Output](#output)
- [Windows Task Scheduler setup](#windows-task-scheduler-setup)
- [Extending the tool](#extending-the-tool)
- [Known limitations](#known-limitations)

---

## Overview

`main.py` collects USD rates by fetching HTML from each source and extracting rate + timestamp via BeautifulSoup + regex. A result is only accepted if the source timestamp matches today's date. If the primary source fails or returns stale data, the next source in the fallback list is tried automatically.

`scheduler.py` wraps `main.py` as a long-running process using APScheduler's cron trigger, firing at configurable hours every day and writing a uniquely named CSV for each run.

---

## Supported Currency Pairs

| Currency | Code | Sources |
|---|---|---|
| Australian Dollar | AUD | XE + Wise + Netdania |
| Chinese Yuan | CNY | XE + Wise |
| Indonesian Rupiah | IDR | XE + Wise |
| Indian Rupee | INR | XE + Wise |
| Japanese Yen | JPY | XE + Wise |
| Hong Kong Dollar | HKD | XE + Wise |
| Malaysian Ringgit | MYR | XE + Wise |
| New Zealand Dollar | NZD | XE + Wise + Netdania |
| Philippine Peso | PHP | XE + Wise |
| Singapore Dollar | SGD | XE + Wise + Netdania |
| Sri Lankan Rupee | LKR | XE + Wise |
| Thai Baht | THB | XE + Wise |
| Vietnamese Dong | VND | XE + Wise |
| Nepalese Rupee | NPR | XE + Wise + NRB |

---

## Data Sources

| Key | Site | Notes |
|---|---|---|
| `xe` | xe.com | Primary source for all 14 pairs; validates today's UTC timestamp |
| `wise` | wise.com | Mid-market rate; secondary source for all 14 pairs; plain HTML |
| `netdania` | netdania.com | AUD, NZD, SGD; time-only (no date in response) |
| `nrb` | nrb.org.np | Nepal Rastra Bank official daily rate for NPR; validates page date |

---

## Project Structure

```
Forex-Daily/
├── main.py            # Core collector — CORRIDORS config, parsers, collect_all(), write_csv()
├── scheduler.py       # APScheduler wrapper — reads config.ini, fires on cron schedule
├── config.ini         # All tunable settings (hours, timezone, output dir, log level)
├── requirements.txt   # Pinned Python dependencies
├── run_scheduler.bat  # Double-click launcher for Windows
└── setup_task.bat     # Registers scheduler as a Windows Task Scheduler job (run as Admin)
```

---

## Prerequisites

- Python 3.11 or newer (required for `zoneinfo` standard library)
- pip
- Internet access to reach the scraping sources

---

## Installation

```bash
# 1. Clone or download the project
git clone <repo-url> Forex-Daily
cd Forex-Daily

# 2. (Recommended) Create a virtual environment
python -m venv .venv

# Windows
.venv\Scripts\activate

# macOS / Linux
source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt
```

---

## Usage

### One-shot run

Print a rate table to stdout only:

```bash
python main.py
```

Print and save to a specific CSV file:

```bash
python main.py --csv usd_rates.csv
```

### Scheduled runs

Start the scheduler (runs until you press Ctrl+C):

```bash
python scheduler.py
```

Use an alternative config file:

```bash
python scheduler.py --config path/to/custom.ini
```

On Windows you can also double-click `run_scheduler.bat`.

The scheduler fires at the hours and minute defined in `config.ini`. Each run writes a CSV named `report_<YYYYMMDDHHmm>.csv` (e.g. `report_202606171530.csv`) to the configured output directory.

---

## Configuration

All settings live in `config.ini`:

```ini
[schedule]
hours    = 9,11,13,15,17,19,21,23   # 24h hours when the job fires
minute   = 30                        # minute past the hour (e.g. :30 → 09:30, 11:30 …)
timezone = Asia/Colombo              # any IANA timezone name

[output]
dir    = output        # directory for CSV files (auto-created)
prefix = report_       # filename prefix before the timestamp

[logging]
file  = scheduler.log  # log file path (relative to project root)
level = INFO           # DEBUG | INFO | WARNING | ERROR
```

To change the interval, edit the `hours` list. For every-2-hours starting at 09:30:

```ini
hours  = 9,11,13,15,17,19,21,23
minute = 30
```

For every-3-hours starting at 08:00:

```ini
hours  = 8,11,14,17,20,23
minute = 0
```

---

## Output

Each run produces a CSV file in the `output/` directory:

```
output/
├── report_202606170930.csv
├── report_202606171130.csv
└── report_202606171330.csv
```

CSV columns:

| Column | Description |
|---|---|
| `date` | UTC date of collection (YYYY-MM-DD) |
| `base_currency` | Always `USD` |
| `quote_currency` | e.g. `INR` |
| `rate` | Numeric rate string, e.g. `83.471` |
| `source_used` | Full URL that returned the accepted rate |
| `source_timestamp` | Timestamp string extracted from the source page |
| `status` | `OK` or `NO_DATA` |

Log output is written to both stdout and `scheduler.log`:

```
2026-06-17 09:30:00 [INFO] Scheduler started. Timezone: Asia/Colombo | Fires at minute :30 of hours: 9,11,13,15,17,19,21,23
2026-06-17 09:30:00 [INFO] Collection started → output/report_202606170930.csv
2026-06-17 09:30:18 [INFO] Done — 12/14 currencies collected. File: output/report_202606170930.csv
```

---

## Windows Task Scheduler setup

To have the scheduler start automatically at Windows logon:

1. Open a command prompt **as Administrator**
2. Run:
   ```bat
   setup_task.bat
   ```
3. To start it immediately without rebooting:
   ```bat
   schtasks /run /tn ForexDailyScheduler
   ```
4. To remove the task later:
   ```bat
   schtasks /delete /tn ForexDailyScheduler /f
   ```

The task runs in the background in a hidden window. All activity is captured in `scheduler.log`.

---

## Extending the tool

**Add a new currency pair** — add an entry to `CORRIDORS` in `main.py`:

```python
"KRW": [
    {"type": "xe", "url": "https://www.xe.com/currencyconverter/convert/?Amount=1&From=USD&To=KRW"},
],
```

**Add a new source site** — write a parser function that returns `(rate_str, timestamp_str)` or raises on failure, then register the new `type` key in `collect_one()`:

```python
def parse_mysite(html: str) -> Optional[tuple[str, str]]:
    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    match = re.search(r"USD.*?([\d.]+)", text)
    if not match:
        return None
    return match.group(1), NONE
```

Timestamp validation (must be today) is handled automatically in `collect_one()` — if the timestamp is `NONE` or unrecognisable, only sources that explicitly pass today's date are accepted via xe-style parsing.

---

## Known limitations

- `netdania` responses include only a time-of-day, not a date — these rates are accepted without date validation.
- `wise` does not expose a machine-readable date in the rate element — rates are accepted at face value with timestamp `NONE`.
- `nrb` publishes one official rate per business day; the rate does not update intraday.
- All scraping depends on source sites' HTML structure remaining stable. Regex patterns may need updating if a site redesigns its layout.
