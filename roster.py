"""Google Sheets → local roster snapshot.

Sheet must have a header row with columns: callsign, full_name, email,
is_officer. Column order does not matter; extra columns are ignored.
The is_officer column accepts TRUE/FALSE, yes/no, 1/0, y/n, or a checkbox
(Google Sheets checkboxes serialize as TRUE/FALSE).
"""
from __future__ import annotations

import gspread
from google.oauth2.service_account import Credentials

SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

_TRUTHY = {"true", "yes", "y", "1", "t", "x", "officer"}


def _parse_bool(v) -> bool:
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    return str(v).strip().lower() in _TRUTHY


def fetch_roster(
    sa_json_path: str, sheet_id: str
) -> list[tuple[str, str, str, bool]]:
    """Return normalized list of (callsign, full_name, email, is_officer).

    Rows missing callsign or email are skipped. Duplicate callsigns keep the
    first occurrence. Callsigns are uppercased; emails lowercased.
    """
    creds = Credentials.from_service_account_file(sa_json_path, scopes=SCOPES)
    gc = gspread.authorize(creds)
    ws = gc.open_by_key(sheet_id).sheet1
    records = ws.get_all_records()  # header-name based

    out: list[tuple[str, str, str, bool]] = []
    seen: set[str] = set()
    for r in records:
        callsign = str(r.get("callsign", "")).strip().upper()
        full_name = str(r.get("full_name", "")).strip()
        email = str(r.get("email", "")).strip().lower()
        is_officer = _parse_bool(r.get("is_officer"))
        if not callsign or not email or callsign in seen:
            continue
        seen.add(callsign)
        out.append((callsign, full_name, email, is_officer))
    return out
