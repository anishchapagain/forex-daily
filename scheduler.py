"""
Forex-Daily Scheduler
=====================
Reads ``config.ini``, then fires a full FX collection run at every
``hour:minute`` combination listed in the ``[schedule]`` section. Each
run writes a timestamped wide-format CSV to the ``[output]`` directory.

The scheduler is a long-running blocking process. Start it once and leave
it running; APScheduler handles the cron timing internally.

Run:
    python scheduler.py
    python scheduler.py --config path/to/other.ini

Stop:
    Ctrl+C  (or kill the process — shutdown is graceful)

Windows auto-start:
    Run setup_task.bat as Administrator to register a Task Scheduler job
    that launches this script automatically at logon.

Fault tolerance
---------------
- A failed collection run is logged at CRITICAL level and a ``FAILED``
  sentinel file is written to the output directory so external monitoring
  tools (e.g. a watch-dog script, a shared-drive alert) can detect it.
- The scheduler process itself never crashes on a bad run; only a
  startup-validation failure or Ctrl+C will stop it.
- Log files are written to the ``logs/`` folder with daily filenames
  (``log_MMDDYYYY.log``). A new file is created automatically each day.
"""

import argparse
import configparser
import logging
import os
import sys
import threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.schedulers.blocking import BlockingScheduler

from main import collect_all, load_currencies, write_csv

# Script directory — used to resolve relative paths regardless of the
# working directory from which the scheduler is launched.
_SCRIPT_DIR = Path(__file__).parent.resolve()


def load_config(path: str = "config.ini") -> configparser.ConfigParser:
    """Load and return a ConfigParser from the given INI file path.

    Args:
        path: Path to the ``config.ini`` file.

    Returns:
        Populated :class:`configparser.ConfigParser` instance.

    Raises:
        SystemExit: If the file cannot be read.
    """
    cfg = configparser.ConfigParser()
    if not cfg.read(path):
        sys.exit(
            f"ERROR: Config file not found: '{path}'. "
            "Ensure config.ini exists or pass --config."
        )
    return cfg


def validate_config(cfg: configparser.ConfigParser) -> None:
    """Validate that all required config sections and values are present and sane.

    Exits the process immediately with a descriptive message on any
    validation failure so operators know exactly what to fix before the
    scheduler starts accepting jobs.

    Args:
        cfg: Populated :class:`configparser.ConfigParser` instance.

    Raises:
        SystemExit: On any missing section, missing key, or out-of-range value.
    """
    # --- Required sections ---
    for section in ("schedule", "output", "logging", "fetch"):
        if section not in cfg:
            sys.exit(
                f"ERROR: Missing [{section}] section in config.ini. "
                "Check the config file against the documented template."
            )

    # --- [schedule] hours ---
    raw_hours = cfg.get("schedule", "hours", fallback="")
    if not raw_hours.strip():
        sys.exit("ERROR: [schedule] hours is empty in config.ini.")
    try:
        hours = [int(h.strip()) for h in raw_hours.split(",")]
        for h in hours:
            if not 0 <= h <= 23:
                raise ValueError(f"hour {h} is outside the valid range [0, 23]")
    except ValueError as exc:
        sys.exit(f"ERROR: Invalid [schedule] hours '{raw_hours}': {exc}")

    # --- [schedule] minute ---
    try:
        minute = cfg.getint("schedule", "minute", fallback=30)
        if not 0 <= minute <= 59:
            raise ValueError(f"minute {minute} is outside the valid range [0, 59]")
    except ValueError as exc:
        sys.exit(f"ERROR: Invalid [schedule] minute in config.ini: {exc}")

    # --- [schedule] timezone ---
    tz_name = cfg.get("schedule", "timezone", fallback="UTC")
    try:
        ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, KeyError):
        sys.exit(
            f"ERROR: Unknown timezone '{tz_name}' in config.ini. "
            "Use an IANA name such as 'Asia/Colombo'. "
            "See https://en.wikipedia.org/wiki/List_of_tz_database_time_zones"
        )

    # --- [fetch] numeric values ---
    try:
        max_retries = cfg.getint("fetch", "max_retries", fallback=3)
        if max_retries < 0:
            raise ValueError(f"max_retries must be >= 0, got {max_retries}")
    except ValueError as exc:
        sys.exit(f"ERROR: Invalid [fetch] max_retries in config.ini: {exc}")

    try:
        retry_delay = cfg.getfloat("fetch", "retry_delay", fallback=2.0)
        if retry_delay < 0:
            raise ValueError(f"retry_delay must be >= 0, got {retry_delay}")
    except ValueError as exc:
        sys.exit(f"ERROR: Invalid [fetch] retry_delay in config.ini: {exc}")

    logging.debug("Config validation passed.")


def setup_logging(cfg: configparser.ConfigParser) -> None:
    """Configure the root logger with a stdout handler and a daily file handler.

    Log level is read from the ``[logging]`` section of ``config.ini``.
    Log files are written to the ``logs/`` folder (next to scheduler.py) with
    filenames of the form ``log_MMDDYYYY.log``, one file per calendar day.

    Args:
        cfg: Parsed :class:`configparser.ConfigParser` from ``config.ini``.
    """
    level = getattr(
        logging,
        cfg.get("logging", "level", fallback="INFO").upper(),
        logging.INFO,
    )

    logs_dir = _SCRIPT_DIR / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    today_stamp = datetime.now().strftime("%m%d%Y")
    log_file = str(logs_dir / f"log_{today_stamp}.log")

    fmt = "%(asctime)s [%(levelname)-8s] %(name)s — %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"

    handlers: list[logging.Handler] = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(log_file, encoding="utf-8"),
    ]
    logging.basicConfig(level=level, format=fmt, datefmt=datefmt, handlers=handlers)


def build_csv_path(cfg: configparser.ConfigParser, tz: ZoneInfo) -> str:
    """Construct the timestamped CSV output path for the current run.

    The output directory is created automatically if it does not exist.
    The filename is ``<prefix><YYYYMMDDHHmm>.csv``, where the timestamp
    reflects the local time in the configured timezone.

    Relative output directories are resolved against the script directory.

    Args:
        cfg: Parsed :class:`configparser.ConfigParser` from ``config.ini``.
        tz: :class:`~zoneinfo.ZoneInfo` for the configured timezone.

    Returns:
        Absolute file path string for the output CSV.
    """
    out_dir = cfg.get("output", "dir", fallback="output")
    if not os.path.isabs(out_dir):
        out_dir = str(_SCRIPT_DIR / out_dir)

    prefix = cfg.get("output", "prefix", fallback="report_")
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(tz).strftime("%Y%m%d%H%M")
    return os.path.join(out_dir, f"{prefix}{stamp}.csv")


def _write_sentinel(out_dir: str, stamp: str) -> None:
    """Write a ``FAILED_<stamp>.txt`` sentinel file to the output directory.

    Used by :func:`run_collection` to signal a failed run to any external
    monitoring tool watching the output directory. The file contains no
    content — its presence is the signal.

    Args:
        out_dir: Directory where the sentinel file is written.
        stamp: Timestamp string appended to the filename.
    """
    sentinel = os.path.join(out_dir, f"FAILED_{stamp}.txt")
    try:
        with open(sentinel, "w", encoding="utf-8") as fh:
            fh.write(
                f"Collection run failed at {stamp}. "
                "Check scheduler.log for details.\n"
            )
        logging.critical("Sentinel file written: %s", sentinel)
    except OSError as exc:
        logging.error("Could not write sentinel file to %s: %s", out_dir, exc)


def run_collection(cfg: configparser.ConfigParser, tz: ZoneInfo) -> None:
    """Execute one complete FX collection run and persist the results.

    Reads retry parameters and the currencies file path from ``config.ini``,
    loads the currency corridor definitions, queries every source for every
    currency, and writes a wide-format CSV named with the current timestamp.

    A wall-clock timeout (configurable via ``[schedule] run_timeout_seconds``,
    default 1800 s / 30 min) is enforced using a daemon thread so a hung
    HTTP request cannot delay or block subsequent scheduled ticks.

    If the run fails for any reason, the exception is logged at CRITICAL
    level and a ``FAILED_<stamp>.txt`` sentinel file is written to the output
    directory so external monitors can detect the failure without parsing
    logs.

    This function is called by APScheduler on each cron tick. It never
    raises — APScheduler will continue scheduling future ticks regardless.

    Args:
        cfg: Parsed :class:`configparser.ConfigParser` from ``config.ini``.
        tz: :class:`~zoneinfo.ZoneInfo` used to stamp the output filename.
    """
    stamp = datetime.now(tz).strftime("%Y%m%d%H%M")
    csv_path = build_csv_path(cfg, tz)
    out_dir = os.path.dirname(csv_path)

    logging.info("=" * 60)
    logging.info("Collection started  →  %s", csv_path)
    logging.info("=" * 60)

    # --- Read run parameters ---
    max_retries = cfg.getint("fetch", "max_retries", fallback=3)
    retry_delay = cfg.getfloat("fetch", "retry_delay", fallback=2.0)
    currencies_path = cfg.get("fetch", "currencies_file", fallback="currencies.toml")
    if not os.path.isabs(currencies_path):
        currencies_path = str(_SCRIPT_DIR / currencies_path)

    # Wall-clock timeout for the entire collection pass.
    # Default: 1800 s (30 min). With 14 currencies × 2 sources × worst-case
    # ~114 s per source, actual max ≈ 3192 s — operators should tune
    # max_retries down if that margin is unacceptable.
    timeout_sec = cfg.getint("schedule", "run_timeout_seconds", fallback=1800)

    # --- Run in a daemon thread so we can enforce the timeout ---
    run_error: list[Exception] = []  # mutable container — thread writes, main reads

    def _run() -> None:
        try:
            corridors = load_currencies(currencies_path)
            rows = collect_all(corridors, max_retries=max_retries, retry_delay=retry_delay)
            write_csv(csv_path, rows)
            ok_count = sum(1 for r in rows if r.overall_status == "OK")
            logging.info(
                "Run complete — %d/%d currencies OK.  CSV: %s",
                ok_count, len(rows), csv_path,
            )
        except Exception as exc:
            run_error.append(exc)

    worker = threading.Thread(target=_run, daemon=True, name="fx-collect")
    worker.start()
    worker.join(timeout=timeout_sec)

    if worker.is_alive():
        # Thread is still blocked — the run has exceeded its budget.
        logging.critical(
            "Collection run TIMED OUT after %d s — worker thread abandoned. "
            "The next scheduled tick will start a new run. "
            "Check network connectivity and source site availability.",
            timeout_sec,
        )
        _write_sentinel(out_dir, stamp)
        return

    if run_error:
        exc = run_error[0]
        logging.critical(
            "Collection run FAILED: %s — %s",
            type(exc).__name__, exc,
            exc_info=exc,
        )
        _write_sentinel(out_dir, stamp)
    # On success no sentinel is written; monitoring tools watch for FAILED_*.txt


def main() -> None:
    """Entry point for the long-running scheduler process.

    Execution sequence:
        1. Parse ``--config`` argument.
        2. Load ``config.ini``.
        3. Validate all required sections and values — exit immediately on
           any error so problems are caught before the first cron tick.
        4. Set up rotating log handlers.
        5. Register the APScheduler cron job.
        6. Start the blocking event loop.

    The scheduler fires :func:`run_collection` at every ``hour:minute``
    combination listed in the ``[schedule]`` section of ``config.ini``.
    ``misfire_grace_time = 300`` (5 minutes) means a run missed because the
    machine was asleep or the process was restarting will still fire if the
    scheduler comes up within 5 minutes of the scheduled time.

    ``max_instances = 1`` prevents overlapping runs if a collection takes
    longer than the interval between scheduled ticks.
    """
    argp = argparse.ArgumentParser(
        description="Forex-Daily long-running cron scheduler."
    )
    argp.add_argument(
        "--config", default="config.ini",
        help="Path to config.ini (default: config.ini).",
    )
    args = argp.parse_args()

    # --- Step 1-2: Load config ---
    cfg = load_config(args.config)

    # --- Step 3: Validate all values before touching logging or scheduling ---
    validate_config(cfg)

    # --- Step 4: Set up logging (must happen after validation so we can exit
    #             cleanly with sys.exit() if validation fails, but before
    #             any scheduled work starts) ---
    setup_logging(cfg)

    tz_name = cfg.get("schedule", "timezone", fallback="UTC")
    tz = ZoneInfo(tz_name)  # Safe: validate_config already confirmed this name

    raw_hours = cfg.get("schedule", "hours", fallback="9,11,13,15,17,19,21,23")
    hours = [int(h.strip()) for h in raw_hours.split(",")]
    minute = cfg.getint("schedule", "minute", fallback=30)

    # --- Step 5: Register the cron job ---
    scheduler = BlockingScheduler(timezone=tz)
    scheduler.add_job(
        run_collection,
        trigger="cron",
        hour=",".join(str(h) for h in hours),
        minute=minute,
        args=[cfg, tz],
        id="fx_collect",
        name="Forex rate collection",
        # Prevent two runs from overlapping if one takes longer than the interval.
        max_instances=1,
        # Fire a missed tick if the scheduler comes up within 5 minutes of it.
        misfire_grace_time=300,
    )

    logging.info(
        "Scheduler started  |  Timezone: %s  |  Fires at :%02d on hours: [%s]",
        tz_name, minute, raw_hours,
    )

    # --- Step 6: Block ---
    try:
        scheduler.start()
    except KeyboardInterrupt:
        logging.info("Scheduler stopped by user (KeyboardInterrupt).")


if __name__ == "__main__":
    main()
