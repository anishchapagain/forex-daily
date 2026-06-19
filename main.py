"""
USD Corridor FX Collector
=========================
Scrapes live USD exchange rates for a configurable set of currency pairs
from multiple public web sources. Currency definitions and source URLs are
loaded from ``currencies.toml``; operational settings (retry policy,
schedule, output paths) are read from ``config.ini``.

Unlike a simple fallback approach, **every** configured source for every
currency is queried on each run. Each source result — success or failure —
is captured independently and written to a wide-format CSV so analysts can
compare rates across providers and audit data quality over time.

Typical usage
-------------
One-shot (print table + optionally write CSV):
    python main.py
    python main.py --csv output/report_202606171530.csv

Via scheduler (called internally by scheduler.py):
    from main import load_currencies, collect_all, write_csv

Install dependencies:
    pip install -r requirements.txt        # Python 3.11+ required

Environment
-----------
Python 3.11+ is required for the ``tomllib`` standard-library module
(TOML parsing without a third-party dependency).
"""

import argparse
import configparser
import csv
import logging
import os
import re
import sys
import time
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup
from dateutil import parser as dtparser
from tabulate import tabulate


# ---------------------------------------------------------------------------
# Module-level logger.
# Handlers are NOT attached here; they are configured by scheduler.py (or by
# the basicConfig call in main() for standalone CLI use).
# ---------------------------------------------------------------------------
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Base currency is fixed for all corridors in this tool.
BASE_CURRENCY: str = "USD"

# Sentinel value used when a rate or timestamp cannot be extracted.
NONE: str = "NONE"

# Default paths — overridden by CLI flags or scheduler config.
CORRIDORS_FILE: str = "currencies.toml"
CONFIG_FILE: str = "config.ini"

# HTTP headers sent with every request to mimic a real browser and reduce
# the chance of being blocked by anti-scraping middleware.
# NOTE: version numbers are intentionally omitted from the UA string to avoid
# becoming stale as browsers update.
HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# Human-readable display names for each parser type key.
# Used as column-name prefixes in the CSV (e.g. "XE_rate", "Wise_rate").
SOURCE_DISPLAY_NAMES: dict[str, str] = {
    "xe":        "XE",
    "wise":      "Wise",
    "netdania":  "Netdania",
    "nrb":       "NRB",
    # Legacy sources retained for column-name backward compatibility only.
    "investing": "Investing",
    "ibrlive":   "IBRLive",
    "sampath":   "Sampath",
    "reuters":   "Reuters",
    "fedan":     "Fedan",
}

# Canonical source order that controls left-to-right column ordering in the CSV.
# Active sources are listed first; legacy stubs follow so old CSV headers remain valid.
SOURCE_ORDER: list[str] = [
    "xe", "wise", "netdania", "nrb",
    "investing", "ibrlive", "sampath", "reuters", "fedan",
]


# ---------------------------------------------------------------------------
# Runtime date helper
# ---------------------------------------------------------------------------

def get_today() -> str:
    """Return the current UTC date as an ISO 8601 string (YYYY-MM-DD).

    Called at the start of each collection run — not cached at module import
    time — so that runs which straddle a UTC midnight boundary always record
    the correct date for each row.

    Returns:
        Today's UTC date, e.g. ``"2026-06-18"``.
    """
    return datetime.now(timezone.utc).date().isoformat()


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class SourceResult:
    """Outcome of a single source fetch-and-parse attempt.

    Attributes:
        src_type: The parser type key as defined in currencies.toml
            (e.g. ``"xe"``, ``"netdania"``).
        rate: Numeric rate string (e.g. ``"83.471"``), or :data:`NONE`
            when the fetch or parse step failed.
        timestamp: Timestamp string extracted from the source page, or
            :data:`NONE` when unavailable.
        status: Outcome code.

            - ``"OK"``         — rate successfully retrieved.
            - ``"TIMEOUT"``    — HTTP request timed out on all retries.
            - ``"HTTP_ERROR"`` — Server returned a 4xx/5xx status.
            - ``"CONN_ERROR"`` — TCP-level connection failure.
            - ``"PARSE_FAIL"`` — Page fetched but rate pattern not found.
            - ``"ERROR"``      — Any other unexpected exception.
            - ``"NO_DATA"``    — Parser type is a known placeholder
                                  (reuters, fedan) with no implementation.
    """

    src_type: str
    rate: str = NONE
    timestamp: str = NONE
    status: str = "NO_DATA"


@dataclass
class Result:
    """Wide-format result for one currency pair, spanning all its sources.

    One :class:`Result` is produced per configured currency per run.
    The ``sources`` list contains one :class:`SourceResult` for every
    source defined in ``currencies.toml``, in declaration order.

    Attributes:
        date: UTC collection date in ``YYYY-MM-DD`` format.
        base_currency: Always ``"USD"``.
        quote_currency: ISO 4217 quote currency code (e.g. ``"INR"``).
        sources: Ordered results, one per configured source.
        overall_status: ``"OK"`` if at least one source succeeded,
            ``"NO_DATA"`` if every source failed.
    """

    date: str
    base_currency: str
    quote_currency: str
    sources: list[SourceResult] = field(default_factory=list)
    overall_status: str = "NO_DATA"


# ---------------------------------------------------------------------------
# Configuration loading
# ---------------------------------------------------------------------------

def load_currencies(path: str = CORRIDORS_FILE) -> dict[str, list[dict[str, str]]]:
    """Load and validate the currency corridor definitions from a TOML file.

    The TOML file must contain one top-level section per quote currency.
    Each section must define a non-empty ``sources`` array of inline
    tables, where every table supplies both a ``"type"`` key (parser
    identifier) and a ``"url"`` key (full HTTP URL to fetch).

    Example TOML fragment::

        [INR]
        sources = [
            {type = "ibrlive", url = "https://ibrlive.com/"},
            {type = "xe",      url = "https://www.xe.com/..."},
        ]

    Args:
        path: Filesystem path to the TOML currencies file. Defaults to
            ``currencies.toml`` in the working directory.

    Returns:
        Ordered dict mapping each ISO quote-currency code to its list of
        validated source dicts (each guaranteed to have ``"type"`` and
        ``"url"`` keys).

    Raises:
        FileNotFoundError: If the file does not exist at ``path``.
        ValueError: If the TOML is syntactically invalid, any section is
            missing ``sources``, has an empty sources list, or a source
            entry omits ``"type"`` or ``"url"``.
    """
    resolved = Path(path)
    if not resolved.exists():
        raise FileNotFoundError(
            f"Currencies config not found: '{path}'. "
            "Ensure currencies.toml exists or pass --currencies <path>."
        )

    try:
        with resolved.open("rb") as fh:
            raw = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        # Surface a readable error so operators know exactly what is wrong.
        raise ValueError(
            f"currencies.toml has invalid TOML syntax: {exc}"
        ) from exc

    if not raw:
        raise ValueError(
            f"currencies.toml at '{path}' is empty — no currencies defined."
        )

    corridors: dict[str, list[dict[str, str]]] = {}

    for code, section in raw.items():
        sources = section.get("sources")
        if not isinstance(sources, list) or len(sources) == 0:
            raise ValueError(
                f"currencies.toml: [{code}] must contain a non-empty 'sources' array."
            )
        for idx, src in enumerate(sources):
            for required_key in ("type", "url"):
                if not src.get(required_key):
                    raise ValueError(
                        f"currencies.toml: [{code}] source[{idx}] is missing "
                        f"required key '{required_key}'."
                    )
        corridors[code] = sources

    logger.debug("Loaded %d currencies from '%s'.", len(corridors), path)
    return corridors


# ---------------------------------------------------------------------------
# HTTP fetch with retry
# ---------------------------------------------------------------------------

def fetch(url: str, max_retries: int = 3, retry_delay: float = 2.0) -> str:
    """Fetch the HTML content of a URL with exponential-backoff retry logic.

    The first attempt is made immediately. On failure the function waits
    ``retry_delay * 2^(attempt-1)`` seconds before each subsequent try,
    giving a back-off schedule of 2 s, 4 s, 8 s for the default settings.

    Total maximum attempts = ``max_retries + 1``.

    Note on timing: with a 25-second per-request timeout and
    ``max_retries=3``/``retry_delay=2``, the worst-case elapsed time per
    URL is 25 + 2 + 25 + 4 + 25 + 8 + 25 ≈ 114 seconds. Reduce
    ``max_retries`` in config.ini if the scheduler interval is tight.

    Args:
        url: The full URL to HTTP GET.
        max_retries: Number of additional attempts after the first failure.
            Set to ``0`` to disable retries entirely.
        retry_delay: Base wait time in seconds before the first retry.
            Doubles on each subsequent retry.

    Returns:
        The decoded response body as a UTF-8 string.

    Raises:
        requests.Timeout: If every attempt times out.
        requests.HTTPError: If the server returns a 4xx/5xx status on the
            final attempt.
        requests.ConnectionError: If a TCP-level error persists across all
            retries.
        requests.RequestException: For any other requests-layer failure that
            persists after all retries.
        RuntimeError: Internal guard — raised if the retry loop exits without
            setting a concrete exception (should never happen in practice).
    """
    # Initialise to a concrete exception so `raise last_exc` below is never
    # `raise None` — guards against logic errors that could exit the loop
    # without setting last_exc through the normal exception path.
    last_exc: Exception = requests.RequestException(
        f"fetch: no attempt was made for '{url}' (max_retries={max_retries})"
    )

    for attempt in range(max_retries + 1):
        if attempt > 0:
            # Exponential back-off: 2 s, 4 s, 8 s, ...
            wait = retry_delay * (2 ** (attempt - 1))
            logger.warning(
                "Retry %d/%d for %s — waiting %.1f s (previous error: %s).",
                attempt, max_retries, url, wait, last_exc,
            )
            time.sleep(wait)

        try:
            response = requests.get(url, headers=HEADERS, timeout=25)
            response.raise_for_status()
            return response.text

        except requests.Timeout as exc:
            logger.debug("Attempt %d/%d timed out for %s.", attempt + 1, max_retries + 1, url)
            last_exc = exc
        except requests.HTTPError as exc:
            logger.debug(
                "Attempt %d/%d HTTP %s for %s.",
                attempt + 1, max_retries + 1, exc.response.status_code, url,
            )
            last_exc = exc
            # Do not retry client errors (4xx) — they will not resolve by
            # retrying. Only retry on transient server errors (5xx) or
            # network-layer failures.
            if exc.response.status_code < 500:
                break
        except requests.ConnectionError as exc:
            logger.debug(
                "Attempt %d/%d connection error for %s: %s.",
                attempt + 1, max_retries + 1, url, exc,
            )
            last_exc = exc
        except requests.RequestException as exc:
            logger.debug(
                "Attempt %d/%d request error for %s: %s.",
                attempt + 1, max_retries + 1, url, exc,
            )
            last_exc = exc

    # All attempts exhausted (or a non-retryable error was encountered).
    raise last_exc


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def clean_rate(value: str) -> str:
    """Strip thousands-separator commas and surrounding whitespace from a rate string.

    Args:
        value: Raw rate string as extracted from page text (e.g. ``"1,234.56"``).

    Returns:
        Cleaned numeric string (e.g. ``"1234.56"``).
    """
    return value.replace(",", "").strip()


def timestamp_is_today(timestamp: str) -> bool:
    """Return True if the timestamp string represents today's UTC date.

    Uses fuzzy parsing via ``python-dateutil`` to handle a wide variety of
    timestamp formats returned by different source sites.

    Args:
        timestamp: Free-form date/time string from the source page.

    Returns:
        ``True`` if the parsed date equals today's UTC date, ``False``
        for any parse failure or a date that does not match today.
    """
    today = get_today()
    try:
        parsed = dtparser.parse(timestamp, fuzzy=True)
        extracted = parsed.date().isoformat()
        if extracted != today:
            logger.debug(
                "timestamp_is_today: '%s' resolved to %s, expected %s.",
                timestamp, extracted, today,
            )
            return False
        return True
    except Exception as exc:
        logger.debug("timestamp_is_today: could not parse '%s': %s.", timestamp, exc)
        return False


# ---------------------------------------------------------------------------
# Per-site parsers
# Each parser receives raw HTML and returns (rate_str, timestamp_str) or None.
# Returning None signals a parse failure; collect_one records PARSE_FAIL.
# ---------------------------------------------------------------------------

def parse_xe(html: str, quote: str) -> Optional[tuple[str, str]]:
    """Extract rate and UTC timestamp from an XE currency converter page.

    Searches the page text for the canonical XE pattern::

        1 USD = <rate> <QUOTE>  <Month DD, YYYY, HH:MM UTC>

    Only returns a result when the embedded timestamp matches today's UTC
    date, ensuring stale cached pages are rejected.

    Args:
        html: Raw HTML response body from ``xe.com``.
        quote: ISO 4217 quote currency code (e.g. ``"INR"``).

    Returns:
        ``(rate, timestamp)`` tuple on success, or ``None`` if the pattern
        is absent or the timestamp is not today.
    """
    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)

    # Match: "1 USD = 83.471 INR" (commas allowed in rate)
    rate_pattern = rf"1\s+USD\s*=\s*([\d,]+(?:\.\d+)?)\s+{quote}"
    rate_match = re.search(rate_pattern, text)
    if not rate_match:
        logger.debug("parse_xe: rate pattern not found for %s.", quote)
        return None

    # Match the full "rate QUOTE <timestamp>" block to capture the UTC datetime.
    timestamp_pattern = (
        rf"1\s+USD\s*=\s*[\d,]+(?:\.\d+)?\s+{quote}\s+"
        rf"([A-Z][a-z]{{2}}\s+\d{{1,2}},\s+\d{{4}},\s+\d{{2}}:\d{{2}}\s+UTC)"
    )
    timestamp_match = re.search(timestamp_pattern, text)
    timestamp = timestamp_match.group(1) if timestamp_match else NONE

    if timestamp == NONE or not timestamp_is_today(timestamp):
        logger.debug(
            "parse_xe: timestamp '%s' not today for %s — rejecting.", timestamp, quote
        )
        return None

    return clean_rate(rate_match.group(1)), timestamp


def parse_netdania(html: str, quote: str) -> Optional[tuple[str, str]]:
    """Extract the bid rate and time from a Netdania forex quotes page.

    The Netdania page lists multiple pairs on a single URL in the format::

        USD/<QUOTE>  <bid>  <ask>  <change>  ...  HH:MM:SS

    Note:
        Netdania responses contain only a time-of-day (``HH:MM:SS``), not a
        calendar date. The returned timestamp is therefore annotated as
        ``"HH:MM:SS, date not shown"`` and date-staleness cannot be verified
        for this source.

    Args:
        html: Raw HTML response body from ``netdania.com``.
        quote: ISO 4217 quote currency code (e.g. ``"AUD"``).

    Returns:
        ``(bid_rate, timestamp)`` on success, or ``None`` if the currency
        pair is not present on the page.
    """
    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    pair = f"USD/{quote}"

    # The page lists pairs as: USD/AUD  1.41613  1.41620  +0.00123  ...  12:19:15
    pattern = (
        rf"{re.escape(pair)}.*?"
        rf"([\d,]+(?:\.\d+)?)\s+([\d,]+(?:\.\d+)?)\s+[-+]?\d+(?:\.\d+)?"
        rf".*?(\d{{2}}:\d{{2}}:\d{{2}})"
    )
    match = re.search(pattern, text)
    if not match:
        logger.debug("parse_netdania: pair %s not found on page.", pair)
        return None

    bid = clean_rate(match.group(1))
    time_only = match.group(3)
    return bid, f"{time_only}, date not shown"


def parse_investing(html: str) -> Optional[tuple[str, str]]:
    """Extract the live rate and time from an Investing.com currency pair page.

    Investing.com embeds real-time data in its page text in the form::

        Add to Watchlist  <rate>  <+/- change>(<pct>)  Real-time Data·HH:MM:SS

    Note:
        Like Netdania, the timestamp contains only time-of-day; date
        validation is not performed for this source.

    Args:
        html: Raw HTML response body from an ``investing.com /currencies/``
            pair page.

    Returns:
        ``(rate, timestamp)`` on success, or ``None`` if the expected
        ``Real-time Data`` pattern is absent.
    """
    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)

    match = re.search(
        r"Add to Watchlist\s+([\d,]+(?:\.\d+)?)\s+[+-][\d,]+(?:\.\d+)?"
        r"\([^)]+\)\s+Real-time Data·(\d{2}:\d{2}:\d{2})",
        text,
    )
    if not match:
        logger.debug("parse_investing: Real-time Data pattern not found.")
        return None

    return clean_rate(match.group(1)), f"{match.group(2)}, date not shown"


def parse_ibrlive(html: str) -> Optional[tuple[str, str]]:
    """Extract the USD/INR live rate from the IBR Live website.

    Searches the page text for a numeric value (typically a 5-6 digit
    float) adjacent to ``USD/INR`` or ``US Dollar`` wording. Returns
    :data:`NONE` as the timestamp because IBR Live does not expose a
    machine-readable timestamp alongside the rate.

    Note:
        The IBR Live page layout is subject to change. If this parser
        begins returning ``PARSE_FAIL``, the regex pattern on the
        ``re.search`` call below may need updating.

    Args:
        html: Raw HTML response body from ``ibrlive.com``.

    Returns:
        ``(rate, NONE)`` on success, or ``None`` if no matching numeric
        value is found.
    """
    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)

    # The site renders the live rate as a 2-3 digit integer part followed by
    # 3-6 decimal places, near "USD/INR" or "US Dollar" text.
    match = re.search(r"(?:USD/INR|US Dollar).*?([\d]{2,3}\.\d{3,6})", text, re.I)
    if not match:
        logger.debug("parse_ibrlive: USD/INR pattern not found.")
        return None

    return clean_rate(match.group(1)), NONE


def parse_sampath(html: str) -> Optional[tuple[str, str]]:
    """Extract the USD buying rate from the Sampath Bank exchange rate table.

    Sampath Bank's rates page lists currencies with a numeric rate column.
    The first numeric value found after ``USD`` in the page text is taken
    as the rate. Returns :data:`NONE` for the timestamp.

    Args:
        html: Raw HTML response body from ``sampath.lk``.

    Returns:
        ``(rate, NONE)`` on success, or ``None`` if the USD pattern is
        absent.
    """
    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)

    match = re.search(r"USD.*?([\d]{2,4}\.\d{2,6})", text, re.I)
    if not match:
        logger.debug("parse_sampath: USD rate pattern not found.")
        return None

    return clean_rate(match.group(1)), NONE


def parse_wise(html: str, quote: str) -> Optional[tuple[str, str]]:
    """Extract the mid-market rate from a Wise currency converter page.

    Wise renders the rate inline in plain HTML as::

        1 USD = <rate> <QUOTE>

    The rate element is present in the initial HTML response so no
    JavaScript execution is required. Wise does not embed a date in the
    rate element, so :data:`NONE` is returned as the timestamp.

    Args:
        html: Raw HTML response body from ``wise.com/us/currency-converter/``.
        quote: ISO 4217 quote currency code (e.g. ``"INR"``).

    Returns:
        ``(rate, NONE)`` on success, or ``None`` if the canonical rate
        pattern is absent.
    """
    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)

    # Pattern: "1 USD = 94.46 INR" — comma-thousands allowed (e.g. VND, IDR)
    match = re.search(
        rf"1\s+USD\s*=\s*([\d,]+(?:\.\d+)?)\s+{re.escape(quote)}",
        text,
        re.I,
    )
    if not match:
        logger.debug("parse_wise: rate pattern not found for %s.", quote)
        return None

    return clean_rate(match.group(1)), NONE


def parse_nrb(html: str) -> Optional[tuple[str, str]]:
    """Extract the USD buy rate from the Nepal Rastra Bank forex page.

    NRB publishes a daily rate table at ``nrb.org.np/forex/`` with two
    tables on the page. The second table (index 1) contains all foreign
    currency rows. Each row has the columns::

        Currency | Unit | Buy | Sell

    The ``unit`` column may be 1 or 100 (e.g. JPY quotes are per 100 JPY).
    The raw buy value is normalised to a per-1-USD rate by dividing by the
    unit before returning. The page includes today's date in plain text
    which is returned as the timestamp.

    Args:
        html: Raw HTML response body from ``nrb.org.np/forex/``.

    Returns:
        ``(rate_per_usd, date_str)`` on success, or ``None`` if the USD
        row or the expected table structure is not found.
    """
    soup = BeautifulSoup(html, "html.parser")

    # Extract today's date from the page text for timestamp validation.
    page_text = soup.get_text(" ", strip=True)
    date_match = re.search(
        r"\b([A-Z][a-z]{2,8}\s+\d{1,2},?\s+\d{4}|\d{4}-\d{2}-\d{2})\b",
        page_text,
    )
    date_str = date_match.group(1) if date_match else NONE

    tables = soup.find_all("table")
    if len(tables) < 2:
        logger.debug("parse_nrb: expected ≥2 tables, found %d.", len(tables))
        return None

    # The second table contains the main currency grid.
    for row in tables[1].find_all("tr"):
        cells = [c.get_text(strip=True) for c in row.find_all(["td", "th"])]
        if len(cells) < 4:
            continue
        # Cell 0: "usd(U.S. Dollar)" — match case-insensitively on "usd"
        if not re.match(r"usd\b", cells[0], re.I):
            continue
        try:
            unit = float(cells[1]) or 1.0
            buy = float(cells[2].replace(",", ""))
        except ValueError:
            logger.debug("parse_nrb: could not parse unit/buy from cells %s.", cells)
            return None

        # NRB quotes NPR per 1 USD, so rate = buy / unit.
        # For unit=1 this is a no-op; kept for correctness with other pairs.
        rate = buy / unit
        return f"{rate:.4f}", date_str

    logger.debug("parse_nrb: USD row not found in table.")
    return None


# ---------------------------------------------------------------------------
# Parser dispatcher
# ---------------------------------------------------------------------------

def _dispatch_parser(
    parser_type: str,
    html: str,
    quote: str,
) -> Optional[tuple[str, str]]:
    """Route an HTML response to the correct parser function by source type.

    Acts as a single registry of all known parser keys. Adding a new source
    type requires only a new branch here plus a corresponding ``parse_*``
    function.

    Args:
        parser_type: The ``type`` field from ``currencies.toml``
            (e.g. ``"xe"``, ``"netdania"``).
        html: Raw HTML response body to pass to the selected parser.
        quote: ISO 4217 quote currency code forwarded to parsers that
            require it (xe, netdania).

    Returns:
        A ``(rate, timestamp)`` tuple from the chosen parser, or ``None``
        if the type is an unimplemented placeholder or the parser finds
        no usable data.
    """
    if parser_type == "xe":
        return parse_xe(html, quote)
    elif parser_type == "wise":
        return parse_wise(html, quote)
    elif parser_type == "nrb":
        return parse_nrb(html)
    elif parser_type == "netdania":
        return parse_netdania(html, quote)
    elif parser_type == "investing":
        return parse_investing(html)
    elif parser_type == "ibrlive":
        return parse_ibrlive(html)
    elif parser_type == "sampath":
        return parse_sampath(html)
    elif parser_type in {"reuters", "fedan"}:
        # Parsers for these sites have not yet been implemented.
        logger.debug(
            "_dispatch_parser: '%s' is a known placeholder — skipping.", parser_type
        )
        return None
    else:
        logger.warning(
            "_dispatch_parser: Unknown source type '%s' — skipping.", parser_type
        )
        return None


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------

def collect_one(
    quote: str,
    source: dict[str, str],
    max_retries: int = 3,
    retry_delay: float = 2.0,
) -> SourceResult:
    """Fetch and parse one source for a single currency pair.

    This function always returns a :class:`SourceResult` — it never raises.
    Specific exception types from ``requests`` are mapped to distinct status
    codes so callers can distinguish network failures from parse failures.

    Args:
        quote: ISO 4217 quote currency code (e.g. ``"INR"``).
        source: Source dict with ``"type"`` and ``"url"`` keys, as loaded
            from ``currencies.toml``.
        max_retries: Number of retry attempts passed to :func:`fetch`.
        retry_delay: Base backoff delay in seconds passed to :func:`fetch`.

    Returns:
        :class:`SourceResult` with ``status="OK"`` and populated
        ``rate``/``timestamp`` fields on success, or a
        :class:`SourceResult` with an error status and :data:`NONE` values
        on any failure.
    """
    # Defensive key access: load_currencies() validates these at startup, but
    # guard here as well in case the dict is constructed elsewhere or the TOML
    # is hot-edited between load and collect.
    src_type = source.get("type")
    url = source.get("url")
    if not src_type or not url:
        logger.error(
            "collect_one: source dict for %s is missing 'type' or 'url': %s",
            quote, source,
        )
        return SourceResult(src_type=src_type or "UNKNOWN", status="ERROR")

    logger.info("    [%s] Fetching %s via %s ...", quote, src_type, url)

    # --- HTTP fetch with typed exception handling ---
    try:
        html = fetch(url, max_retries=max_retries, retry_delay=retry_delay)

    except requests.Timeout:
        logger.warning(
            "    [%s/%s] TIMEOUT — all %d attempt(s) timed out for %s.",
            quote, src_type, max_retries + 1, url,
        )
        return SourceResult(src_type=src_type, status="TIMEOUT")

    except requests.HTTPError as exc:
        logger.warning(
            "    [%s/%s] HTTP_ERROR %s from %s.",
            quote, src_type, exc.response.status_code, url,
        )
        return SourceResult(src_type=src_type, status="HTTP_ERROR")

    except requests.ConnectionError as exc:
        logger.warning(
            "    [%s/%s] CONN_ERROR for %s: %s.",
            quote, src_type, url, exc,
        )
        return SourceResult(src_type=src_type, status="CONN_ERROR")

    except requests.RequestException as exc:
        logger.warning(
            "    [%s/%s] REQUEST_ERROR for %s: %s.",
            quote, src_type, url, exc,
        )
        return SourceResult(src_type=src_type, status="ERROR")

    # --- Parse ---
    parsed = _dispatch_parser(src_type, html, quote)
    if parsed is None:
        logger.warning(
            "    [%s/%s] PARSE_FAIL — no matching pattern in response from %s.",
            quote, src_type, url,
        )
        return SourceResult(src_type=src_type, status="PARSE_FAIL")

    rate, timestamp = parsed
    logger.info(
        "    [%s/%s] OK — rate=%s  timestamp=%s.",
        quote, src_type, rate, timestamp,
    )
    return SourceResult(src_type=src_type, rate=rate, timestamp=timestamp, status="OK")


def collect_all(
    corridors: dict[str, list[dict[str, str]]],
    max_retries: int = 3,
    retry_delay: float = 2.0,
) -> list[Result]:
    """Collect rates from every configured source for every currency.

    Every source for every currency is queried — there is no stop-at-first-
    success behaviour. The intent is to give a complete, comparable picture
    of rates across providers on each run.

    Args:
        corridors: Mapping of quote-currency codes to source lists, as
            returned by :func:`load_currencies`.
        max_retries: Retry budget per HTTP request, forwarded to
            :func:`collect_one`.
        retry_delay: Base exponential back-off delay in seconds, forwarded
            to :func:`collect_one`.

    Returns:
        One :class:`Result` per currency. Each ``Result`` contains one
        :class:`SourceResult` per configured source, plus an
        ``overall_status`` of ``"OK"`` if at least one source succeeded or
        ``"NO_DATA"`` if every source failed.

    Raises:
        ValueError: If ``corridors`` is empty (no currencies configured).
    """
    if not corridors:
        raise ValueError(
            "collect_all: corridors dict is empty — no currencies to collect. "
            "Check currencies.toml."
        )

    # Capture the date once per run, not at module import, so midnight-boundary
    # runs record the correct date for each row.
    today = get_today()

    results: list[Result] = []
    total = len(corridors)

    for idx, (quote, sources) in enumerate(corridors.items(), start=1):
        logger.info(
            "[%d/%d] Collecting %s/%s — %d source(s) configured.",
            idx, total, BASE_CURRENCY, quote, len(sources),
        )

        row = Result(date=today, base_currency=BASE_CURRENCY, quote_currency=quote)

        for source in sources:
            sr = collect_one(quote, source, max_retries=max_retries, retry_delay=retry_delay)
            row.sources.append(sr)

        # Overall status: OK if at least one source returned a usable rate.
        row.overall_status = "OK" if any(s.status == "OK" for s in row.sources) else "NO_DATA"

        ok_sources = sum(1 for s in row.sources if s.status == "OK")
        logger.info(
            "[%d/%d] %s/%s — overall=%s (%d/%d sources OK).",
            idx, total, BASE_CURRENCY, quote, row.overall_status,
            ok_sources, len(sources),
        )
        results.append(row)

    ok_currencies = sum(1 for r in results if r.overall_status == "OK")
    logger.info(
        "Collection complete — %d/%d currencies have at least one OK source.",
        ok_currencies, total,
    )
    return results


# ---------------------------------------------------------------------------
# CSV output
# ---------------------------------------------------------------------------

def _col(src_type: str, attribute: str) -> str:
    """Build a CSV column name from a source type key and attribute name.

    Uses :data:`SOURCE_DISPLAY_NAMES` for the label prefix. Falls back to
    a ``Unknown_<type>`` prefix for unregistered types to prevent silent
    column-name collisions.

    Args:
        src_type: Parser type key (e.g. ``"xe"``).
        attribute: Column attribute name (``"rate"``, ``"timestamp"``,
            or ``"status"``).

    Returns:
        Column name string, e.g. ``"XE_rate"`` or ``"Unknown_mysite_rate"``.
    """
    label = SOURCE_DISPLAY_NAMES.get(src_type)
    if label is None:
        label = f"Unknown_{src_type}"
        logger.warning(
            "_col: source type '%s' not in SOURCE_DISPLAY_NAMES — using '%s'.",
            src_type, label,
        )
    return f"{label}_{attribute}"


def write_csv(path: str, rows: list[Result]) -> None:
    """Write collection results to a wide-format CSV file.

    Columns are grouped by attribute rather than by source, giving a layout
    that is easy to scan and compare across providers::

        date, base_currency, quote_currency,
        XE_rate, Netdania_rate, Investing_rate, ...,   ← all rates together
        XE_timestamp, Netdania_timestamp, ...,          ← all timestamps together
        XE_status, Netdania_status, ...,                ← all statuses together
        overall_status

    Only source types that appear in at least one row are included as
    columns. Rows that do not use a particular source leave those cells
    empty.

    The file is flushed and synced to disk before returning so that a
    subsequent crash does not leave a partial or zero-byte CSV behind.

    Args:
        path: Destination file path. Created or overwritten on each call.
        rows: List of :class:`Result` objects as returned by
            :func:`collect_all`. An empty list writes a header-only file.

    Raises:
        OSError: If the destination file cannot be opened or written after
            all internal retry attempts.
    """
    if not rows:
        logger.warning("write_csv: called with an empty rows list — writing header only.")

    # Determine which source types actually appear across all rows, preserving
    # the canonical display order defined in SOURCE_ORDER.
    present_types: list[str] = [
        t for t in SOURCE_ORDER
        if any(sr.src_type == t for r in rows for sr in r.sources)
    ]

    # Columns: fixed prefix | all rates | all timestamps | all statuses | overall
    fixed_fields  = ["date", "base_currency", "quote_currency"]
    rate_fields   = [_col(t, "rate")      for t in present_types]
    ts_fields     = [_col(t, "timestamp") for t in present_types]
    status_fields = [_col(t, "status")    for t in present_types]
    fieldnames    = fixed_fields + rate_fields + ts_fields + status_fields + ["overall_status"]

    # Retry the disk write up to 3 times to handle transient I/O errors.
    max_write_attempts = 3
    last_write_exc: Optional[Exception] = None

    for write_attempt in range(1, max_write_attempts + 1):
        try:
            with open(path, "w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
                writer.writeheader()

                for row in rows:
                    # Index this row's source results by their type key for O(1) lookup.
                    by_type: dict[str, SourceResult] = {sr.src_type: sr for sr in row.sources}

                    record: dict[str, str] = {
                        "date":           row.date,
                        "base_currency":  row.base_currency,
                        "quote_currency": row.quote_currency,
                        "overall_status": row.overall_status,
                    }

                    for src_type in present_types:
                        sr = by_type.get(src_type)
                        # Leave cells empty (not NONE) when this currency has no such source.
                        record[_col(src_type, "rate")]      = sr.rate      if sr else ""
                        record[_col(src_type, "timestamp")] = sr.timestamp if sr else ""
                        record[_col(src_type, "status")]    = sr.status    if sr else ""

                    writer.writerow(record)

                # Force all data to disk before returning so a subsequent crash
                # cannot leave a truncated or zero-byte file.
                fh.flush()
                os.fsync(fh.fileno())

            logger.info(
                "CSV written: %s  (%d row(s), sources: %s).",
                path, len(rows),
                ", ".join(SOURCE_DISPLAY_NAMES.get(t, t) for t in present_types),
            )
            return  # Success — exit the retry loop.

        except OSError as exc:
            last_write_exc = exc
            if write_attempt < max_write_attempts:
                wait = 2 ** (write_attempt - 1)  # 1 s, 2 s
                logger.warning(
                    "CSV write attempt %d/%d failed — retrying in %d s: %s",
                    write_attempt, max_write_attempts, wait, exc,
                )
                time.sleep(wait)
            else:
                logger.error(
                    "CSV write failed after %d attempts: %s",
                    max_write_attempts, exc,
                )

    # All write attempts exhausted — re-raise so the caller (scheduler) can
    # log it as a run-level failure and alert operators.
    raise last_write_exc  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Command-line entry point for one-shot FX rate collection.

    Parses command-line arguments, loads configuration and currency
    definitions, runs the full collection pass, prints a summary table to
    stdout, and optionally writes a wide-format CSV.

    CLI arguments:
        --csv PATH        Write results to this CSV path.
        --config PATH     Path to config.ini  (default: ``config.ini``).
        --currencies PATH Path to currencies.toml (default: ``currencies.toml``).
    """
    argp = argparse.ArgumentParser(
        description="Scrape live USD exchange rates from multiple sources."
    )
    argp.add_argument("--csv", metavar="PATH", help="Optional CSV output path.")
    argp.add_argument(
        "--config", metavar="PATH", default=CONFIG_FILE,
        help=f"Path to config.ini (default: {CONFIG_FILE}).",
    )
    argp.add_argument(
        "--currencies", metavar="PATH", default=CORRIDORS_FILE,
        help=f"Path to currencies.toml (default: {CORRIDORS_FILE}).",
    )
    args = argp.parse_args()

    # Install a basic logging handler for standalone CLI use.
    # When main.py is imported by scheduler.py, basicConfig is a no-op because
    # scheduler.py already attached handlers to the root logger.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)-8s] %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Read retry configuration from config.ini (silently use defaults if absent).
    cfg = configparser.ConfigParser()
    cfg.read(args.config)
    try:
        max_retries = cfg.getint("fetch", "max_retries", fallback=3)
        retry_delay = cfg.getfloat("fetch", "retry_delay", fallback=2.0)
    except ValueError as exc:
        logging.error("Invalid numeric value in config [fetch] section: %s", exc)
        sys.exit(1)

    # Load currency corridor definitions from TOML.
    try:
        corridors = load_currencies(args.currencies)
    except (FileNotFoundError, ValueError) as exc:
        logging.error("Failed to load currencies: %s", exc)
        sys.exit(1)

    # Run the collection.
    rows = collect_all(corridors, max_retries=max_retries, retry_delay=retry_delay)

    # Build a flat representation for the terminal summary table.
    display_rows = []
    for r in rows:
        flat: dict[str, str] = {
            "quote": r.quote_currency,
            "overall": r.overall_status,
        }
        for n, s in enumerate(r.sources, start=1):
            flat[f"s{n}({s.src_type})"] = f"{s.rate}  [{s.status}]"
        display_rows.append(flat)

    print(tabulate(display_rows, headers="keys", tablefmt="github"))

    if args.csv:
        try:
            write_csv(args.csv, rows)
            print(f"\nSaved CSV: {args.csv}")
        except OSError as exc:
            logging.error("Could not write CSV to '%s': %s", args.csv, exc)
            sys.exit(1)


if __name__ == "__main__":
    main()
