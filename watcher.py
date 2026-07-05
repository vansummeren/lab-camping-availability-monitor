#!/usr/bin/env python3
"""
Camping Bakkum availability watcher — direct Holiday Agent API version.

The booking backend (api.holidayagent.nl) exposes a public JSON API that the
website itself uses. The /v1/resort/kdc-bakkum/arrivals endpoint returns, per
pitch category ("level"), every bookable arrival date with all possible
departure dates and the number of available pitches. No browser needed.

For the target window we report every bookable (arrival, departure) stay of
at least MIN_NIGHTS nights, per category, and notify on the transition
unavailable -> available.

Env vars:
  WINDOW_START     default 2026-07-25
  WINDOW_END       default 2026-07-31
  MIN_NIGHTS       default 4
  LEVELS           comma-separated category idents, default: camping categories
  ADULTS           default 2
  NTFY_URL         e.g. https://ntfy.sh/bakkum-<random>
  HA_WEBHOOK_URL   e.g. http://homeassistant.lan:8123/api/webhook/...
  STATE_FILE       default /data/state.json
  NOTIFY_ON_ERROR  1 = also notify when the check itself fails
  CHECK_INTERVAL   seconds between cycles; 0 = run once and exit
"""

import json
import os
import sys
import time
import urllib.request
import urllib.parse
from datetime import datetime, date
from pathlib import Path

RESORT = "kdc-bakkum"
API_BASE = f"https://api.holidayagent.nl/v1/resort/{RESORT}"
BOOKING_URL = os.environ.get(
    "PAGE_URL", "https://www.campingbakkum.de/ubernachten/campen")

WINDOW_START = os.environ.get("WINDOW_START", "2026-07-25")
WINDOW_END = os.environ.get("WINDOW_END", "2026-07-31")
MIN_NIGHTS = int(os.environ.get("MIN_NIGHTS", "4"))
ADULTS = os.environ.get("ADULTS", "2")
# default: the 8 camping pitch categories shown on /ubernachten/campen
DEFAULT_LEVELS = "257,265,21636,10514,259,260,266,258"
LEVELS = [x.strip() for x in os.environ.get("LEVELS", DEFAULT_LEVELS).split(",") if x.strip()]
# names for levels the API returns without a label
NAME_OVERRIDES = {"21636": "Campingplätze am Spielplatz",
                  "10514": "Wohnmobilplätze Deluxe"}

NTFY_URL = os.environ.get("NTFY_URL", "")
HA_WEBHOOK_URL = os.environ.get("HA_WEBHOOK_URL", "")
STATE_FILE = Path(os.environ.get("STATE_FILE", "/data/state.json"))
NOTIFY_ON_ERROR = os.environ.get("NOTIFY_ON_ERROR", "0") == "1"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "application/json",
    "Origin": "https://www.campingbakkum.de",
    "Referer": "https://www.campingbakkum.de/",
}


def log(msg: str) -> None:
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}", flush=True)


def api_get(path: str, params: list) -> dict:
    qs = urllib.parse.urlencode(params)
    url = f"{API_BASE}/{path}?{qs}"
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def notify(title: str, body: str, priority: str = "high") -> None:
    if NTFY_URL:
        try:
            req = urllib.request.Request(
                NTFY_URL, data=body.encode(),
                headers={"Title": title.encode("ascii", "ignore").decode(),
                         "Priority": priority,
                         "Tags": "tent,tada" if priority == "high" else "warning",
                         "Click": BOOKING_URL},
                method="POST")
            urllib.request.urlopen(req, timeout=15)
            log(f"ntfy sent: {title}")
        except Exception as e:
            log(f"ntfy failed: {e}")
    if HA_WEBHOOK_URL:
        try:
            payload = json.dumps({"title": title, "message": body,
                                  "url": BOOKING_URL}).encode()
            req = urllib.request.Request(
                HA_WEBHOOK_URL, data=payload,
                headers={"Content-Type": "application/json",
                         "User-Agent": HEADERS["User-Agent"]}, method="POST")
            urllib.request.urlopen(req, timeout=15)
            log(f"HA webhook sent: {title}")
        except Exception as e:
            log(f"HA webhook failed: {e}")


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def fetch_level_names() -> dict:
    """ident -> display name, via the levels endpoint."""
    names = dict(NAME_OVERRIDES)
    try:
        data = api_get("levels", [("lng", "de"), ("decode_htmlentities", "true")])
        for lvl in data["response"]["levels"].values():
            ident = str(lvl.get("ident", ""))
            name = (lvl.get("name") or "").strip()
            if ident and name and ident not in NAME_OVERRIDES:
                names[ident] = name
    except Exception as e:
        log(f"level-name fetch failed (using idents): {e}")
    return names


def parse_stamp(stamp: str) -> date:
    return date(int(stamp[0:4]), int(stamp[4:6]), int(stamp[6:8]))


def fetch_level_availability(ident: str) -> list:
    """All bookable (arrival_date, departure_date, nights, amount) stays
    for one category that lie fully inside the target window."""
    data = api_get("arrivals", [
        ("lng", "de"),
        ("amount_adults", ADULTS),
        ("levels[]", ident),
        ("startdate-use-nearest", "true"),
        ("amount-of-months", "3"),
    ])
    win_start = date.fromisoformat(WINDOW_START)
    win_end = date.fromisoformat(WINDOW_END)
    stays = []
    for a in data["response"].get("arrivals", []):
        arr = parse_stamp(a["stamp"])
        if not (win_start <= arr <= win_end):
            continue
        for d in a.get("departures", []):
            dep = parse_stamp(d["stamp"])
            nights = int(d["nights"])
            amount = int(d.get("amountAvailable") or 0)
            if amount > 0 and nights >= MIN_NIGHTS and dep <= win_end:
                stays.append((arr.isoformat(), dep.isoformat(), nights, amount))
    return stays


def maximal(stays: list) -> list:
    """Drop stays fully contained in a longer available stay."""
    out = []
    for i, (a1, b1, *_ ) in enumerate(stays):
        contained = any(j != i and a2 <= a1 and b1 <= b2
                        for j, (a2, b2, *_ ) in enumerate(stays))
        if not contained:
            out.append(stays[i])
    return out


def fmt(a: str, b: str, nights: int, amount: int = 0) -> str:
    da, db = date.fromisoformat(a), date.fromisoformat(b)
    s = f"{da.strftime('%d.%m.')}–{db.strftime('%d.%m.')} ({nights} Nächte"
    if amount:
        s += f", {amount} frei"
    return s + ")"


def main() -> int:
    log(f"checking window {WINDOW_START} → {WINDOW_END}, "
        f"min {MIN_NIGHTS} nights, levels: {','.join(LEVELS)}")
    names = fetch_level_names()

    availability: dict = {}
    errors = 0
    for ident in LEVELS:
        name = names.get(ident, f"Kategorie {ident}")
        try:
            stays = fetch_level_availability(ident)
        except Exception as e:
            log(f"  {name} ({ident}): API error: {e}")
            errors += 1
            continue
        if stays:
            availability[name] = stays
            for s in stays:
                log(f"  ✅ {name}: {fmt(*s)}")
        else:
            log(f"  ❌ {name}: nichts ≥ {MIN_NIGHTS} Nächte im Fenster")
        time.sleep(1)   # be polite

    if errors == len(LEVELS):
        log("all API calls failed")
        if NOTIFY_ON_ERROR:
            notify("Bakkum-Watcher Fehler", "Alle API-Aufrufe fehlgeschlagen.",
                   priority="default")
        return 1

    state = load_state()
    prev = {cat: {tuple(x[:2]) for x in v}
            for cat, v in state.get("availability", {}).items()}

    lines = []
    for cat, stays in sorted(availability.items()):
        new = [s for s in stays if (s[0], s[1]) not in prev.get(cat, set())]
        if not new:
            continue
        pretty = ", ".join(fmt(*s) for s in sorted(maximal(new)))
        lines.append(f"• {cat}: {pretty}")

    if lines:
        body = (f"Frei geworden im Fenster {WINDOW_START} – {WINDOW_END}:\n"
                + "\n".join(lines)
                + f"\n\nSchnell buchen: {BOOKING_URL}")
        notify("🏕️ Camping Bakkum: Stellplatz frei!", body)

    state["availability"] = {cat: [list(s) for s in v]
                             for cat, v in availability.items()}
    state["last_check"] = datetime.now().isoformat(timespec="seconds")
    save_state(state)
    return 0


if __name__ == "__main__":
    interval = int(os.environ.get("CHECK_INTERVAL", "0"))
    if interval > 0:
        while True:
            try:
                main()
            except Exception as e:
                log(f"cycle failed: {e}")
            log(f"sleeping {interval}s")
            time.sleep(interval)
    else:
        sys.exit(main())
