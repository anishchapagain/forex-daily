"""
OneDrive / Excel Uploader
=========================
Overwrites a named worksheet in an Excel workbook stored on OneDrive for
Business (Microsoft 365) using the Microsoft Graph API.

Authentication uses the OAuth 2.0 client-credentials flow — no interactive
login, safe for unattended EC2 execution. An Azure AD App Registration with
``Files.ReadWrite.All`` (or ``Sites.ReadWrite.All``) application permission
is required.

Required environment variables
-------------------------------
AZURE_TENANT_ID       Azure AD tenant ID (GUID or domain)
AZURE_CLIENT_ID       App Registration client ID (GUID)
AZURE_CLIENT_SECRET   App Registration client secret
ONEDRIVE_DRIVE_ID     OneDrive drive ID  (e.g. ``b!abc123...``)
ONEDRIVE_FILE_ID      Excel file item ID  (from the drive)
ONEDRIVE_SHEET_NAME   Worksheet name to overwrite  (e.g. ``FX_Rates``)

How to find drive/file IDs
--------------------------
1. Open the Excel file in OneDrive in a browser.
2. Copy the URL — it contains both the ``resid`` (file item ID) and the
   drive ID can be retrieved via Graph Explorer:
   GET https://graph.microsoft.com/v1.0/me/drive  →  ``id`` field
   GET https://graph.microsoft.com/v1.0/drives/<driveId>/root:/path/to/file.xlsx
   →  ``id`` field is the file item ID.

Standalone usage
----------------
    python onedrive_upload.py --csv output/report_202606191530.csv

Called by scheduler
-------------------
    from onedrive_upload import upload_to_excel
    upload_to_excel(headers, data_rows)
"""

import argparse
import csv
import logging
import os
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# Graph API base
_GRAPH = "https://graph.microsoft.com/v1.0"
_TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
_SCOPE = "https://graph.microsoft.com/.default"


# ---------------------------------------------------------------------------
# Credential helpers
# ---------------------------------------------------------------------------

def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise EnvironmentError(
            f"Required environment variable '{name}' is not set. "
            "Set it (or export it in your shell / EC2 task definition) before running."
        )
    return value


def _acquire_token() -> str:
    """Obtain a bearer token via client-credentials flow.

    Returns:
        Access token string.

    Raises:
        EnvironmentError: If any required env var is missing.
        requests.HTTPError: If the token request fails.
    """
    tenant    = _require_env("AZURE_TENANT_ID")
    client_id = _require_env("AZURE_CLIENT_ID")
    secret    = _require_env("AZURE_CLIENT_SECRET")

    resp = requests.post(
        _TOKEN_URL.format(tenant=tenant),
        data={
            "grant_type":    "client_credentials",
            "client_id":     client_id,
            "client_secret": secret,
            "scope":         _SCOPE,
        },
        timeout=30,
    )
    resp.raise_for_status()
    token = resp.json().get("access_token", "")
    if not token:
        raise ValueError("Token response did not contain access_token.")
    logger.debug("Graph API token acquired.")
    return token


# ---------------------------------------------------------------------------
# Graph API helpers
# ---------------------------------------------------------------------------

def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _sheet_url(drive_id: str, file_id: str, sheet_name: str) -> str:
    return (
        f"{_GRAPH}/drives/{drive_id}/items/{file_id}"
        f"/workbook/worksheets/{requests.utils.quote(sheet_name, safe='')}"
    )


def _clear_sheet(token: str, drive_id: str, file_id: str, sheet_name: str) -> None:
    """Delete all content from the worksheet's used range."""
    url = _sheet_url(drive_id, file_id, sheet_name) + "/usedRange/clear"
    resp = requests.post(url, headers=_headers(token), json={"applyTo": "contents"}, timeout=30)
    if resp.status_code == 404:
        # Sheet may be empty (no used range) — not an error.
        logger.debug("_clear_sheet: usedRange returned 404 (sheet is empty), skipping clear.")
        return
    resp.raise_for_status()
    logger.debug("_clear_sheet: worksheet cleared.")


def _write_range(
    token: str,
    drive_id: str,
    file_id: str,
    sheet_name: str,
    values: list[list[str]],
) -> None:
    """Write a 2-D list of values starting at cell A1 of the worksheet.

    The Graph API requires the range address to match the dimensions of the
    values array exactly (rows × columns).

    Args:
        values: List of rows; each row is a list of cell values (strings).
    """
    if not values:
        logger.warning("_write_range: called with empty values — nothing to write.")
        return

    n_rows = len(values)
    n_cols = len(values[0])

    # Convert column index (1-based) to Excel letter(s): 1→A, 26→Z, 27→AA …
    def col_letter(n: int) -> str:
        result = ""
        while n:
            n, remainder = divmod(n - 1, 26)
            result = chr(65 + remainder) + result
        return result

    end_col = col_letter(n_cols)
    address = f"A1:{end_col}{n_rows}"
    url = _sheet_url(drive_id, file_id, sheet_name) + f"/range(address='{address}')"

    resp = requests.patch(
        url,
        headers=_headers(token),
        json={"values": values},
        timeout=60,
    )
    resp.raise_for_status()
    logger.debug("_write_range: wrote %d rows × %d cols to %s.", n_rows, n_cols, address)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def upload_to_excel(
    headers: list[str],
    data_rows: list[list[str]],
    drive_id: Optional[str] = None,
    file_id: Optional[str] = None,
    sheet_name: Optional[str] = None,
) -> None:
    """Overwrite the target Excel worksheet with the provided table data.

    Credentials and target identifiers are read from environment variables
    when the corresponding keyword arguments are omitted. This allows
    scheduler.py to call this function with no arguments while still
    permitting overrides in tests or one-off scripts.

    Args:
        headers:   List of column-name strings (becomes row 1 of the sheet).
        data_rows: List of data rows; each row is a list of cell values.
        drive_id:  OneDrive drive ID. Defaults to ``ONEDRIVE_DRIVE_ID`` env var.
        file_id:   Excel file item ID. Defaults to ``ONEDRIVE_FILE_ID`` env var.
        sheet_name: Worksheet name to overwrite. Defaults to
            ``ONEDRIVE_SHEET_NAME`` env var.

    Raises:
        EnvironmentError: If any required credential env var is missing.
        requests.HTTPError: On any Graph API error.
    """
    drive_id   = drive_id   or _require_env("ONEDRIVE_DRIVE_ID")
    file_id    = file_id    or _require_env("ONEDRIVE_FILE_ID")
    sheet_name = sheet_name or _require_env("ONEDRIVE_SHEET_NAME")

    token = _acquire_token()
    _clear_sheet(token, drive_id, file_id, sheet_name)

    all_rows: list[list[str]] = [headers] + data_rows
    _write_range(token, drive_id, file_id, sheet_name, all_rows)

    logger.info(
        "OneDrive upload complete — %d data row(s) written to sheet '%s'.",
        len(data_rows), sheet_name,
    )


# ---------------------------------------------------------------------------
# Standalone CLI
# ---------------------------------------------------------------------------

def _load_csv(path: str) -> tuple[list[str], list[list[str]]]:
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        rows = list(reader)
    if not rows:
        raise ValueError(f"CSV file is empty: {path}")
    return rows[0], rows[1:]


def main() -> None:
    argp = argparse.ArgumentParser(
        description="Upload a collected FX CSV to an Excel sheet on OneDrive."
    )
    argp.add_argument("--csv", required=True, metavar="PATH", help="Path to the CSV file.")
    args = argp.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)-8s] %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    csv_path = Path(args.csv)
    if not csv_path.exists():
        argp.error(f"CSV file not found: {args.csv}")

    headers, data_rows = _load_csv(str(csv_path))
    upload_to_excel(headers, data_rows)
    print(f"Uploaded {len(data_rows)} rows from {args.csv}.")


if __name__ == "__main__":
    main()
