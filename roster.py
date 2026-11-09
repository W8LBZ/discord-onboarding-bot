"""The club roster, from the W8LBZ Minutes app's roster API (it replaced the Google Sheet).

GET <ROSTER_API_URL> with "Authorization: Bearer <ROSTER_API_TOKEN>" returns active members as JSON:
[{"callsign": "W8AMY", "name": "Amy Able", "email": "amy@example.org", "officer": true, "former": ["KD8AAA"]}, ...]
"former" lists a member's earlier callsigns, so a claim made under an old call can follow them (callsign_changes).
The app answers only from addresses it's told to allow (this bot's container), with the token.
"""
from __future__ import annotations

import json
import urllib.request


def parse_roster(members: list[dict]) -> list[tuple[str, str, str, bool]]:
    """Normalized (callsign, full_name, email, is_officer). Rows missing a callsign or email are skipped;
    a duplicate callsign keeps the first. Callsigns are uppercased, emails lowercased."""
    out: list[tuple[str, str, str, bool]] = []
    seen: set[str] = set()
    for m in members:
        callsign = str(m.get("callsign") or "").strip().upper()
        email = str(m.get("email") or "").strip().lower()
        if not callsign or not email or callsign in seen:
            continue
        seen.add(callsign)
        out.append((callsign, str(m.get("name") or "").strip(), email, m.get("officer") is True))
    return out


def callsign_changes(members: list[dict]) -> dict[str, str]:
    """{old call: current call} from each member's "former" list."""
    out = {}
    for m in members:
        new = str(m.get("callsign") or "").strip().upper()
        for old in m.get("former") or []:
            old = str(old).strip().upper()
            if new and old and old != new:
                out[old] = new
    return out


def fetch_roster(url: str, token: str, timeout: int = 15) -> tuple[list[tuple[str, str, str, bool]], dict[str, str]]:
    """(roster rows, {old call: current call})."""
    if not url.startswith(("http://", "https://")):
        raise ValueError("ROSTER_API_URL must be an http:// or https:// address")
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}",
                                               "User-Agent": "SREL-onboarding-bot"})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # raises on 403/404/network errors
        data = json.load(r)
    if not isinstance(data, list):
        raise ValueError("roster API returned something other than a list")
    return parse_roster(data), callsign_changes(data)
