#!/usr/bin/env python3
"""
Camping Bakkum availability watcher.

Loads the (JS-rendered) camping overview page with headless Chromium,
selects the desired arrival/departure dates via the search widget,
intercepts booking-API XHR responses, and scans both API payloads and
the rendered DOM for availability of pitches in the target week.

Notifies via ntfy and/or a Home Assistant webhook when a previously
unavailable pitch category becomes available (i.e. a cancellation).

Env vars (all optional except where noted):
  ARRIVAL          default 2026-07-25
  DEPARTURE        default 2026-07-31
  PAGE_URL         default https://www.campingbakkum.de/ubernachten/campen
  NTFY_URL         e.g. https://ntfy.sh/bakkum-<random> or your self-hosted topic URL
  HA_WEBHOOK_URL   e.g. https://ha.example.ch/api/webhook/bakkum_watch
  STATE_FILE       default /data/state.json
  DISCOVERY_DIR    default /data/discovery  (XHR dumps, only when DISCOVERY=1)
  DISCOVERY        set to 1 to dump all captured JSON XHR bodies for inspection
  NOTIFY_ON_ERROR  set to 1 to also get notified when the check itself fails
"""

import json
import os
import re
import sys
import time
import hashlib
import urllib.request
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

ARRIVAL = os.environ.get("ARRIVAL", "2026-07-25")
DEPARTURE = os.environ.get("DEPARTURE", "2026-07-31")
PAGE_URL = os.environ.get("PAGE_URL", "https://www.campingbakkum.de/ubernachten/campen")
NTFY_URL = os.environ.get("NTFY_URL", "")
HA_WEBHOOK_URL = os.environ.get("HA_WEBHOOK_URL", "")
STATE_FILE = Path(os.environ.get("STATE_FILE", "/data/state.json"))
DISCOVERY_DIR = Path(os.environ.get("DISCOVERY_DIR", "/data/discovery"))
DISCOVERY = os.environ.get("DISCOVERY", "0") == "1"
NOTIFY_ON_ERROR = os.environ.get("NOTIFY_ON_ERROR", "0") == "1"

# Keywords that indicate a pitch category is NOT bookable (DE/NL/EN)
NEGATIVE = re.compile(
    r"ausgebucht|nicht\s+verf(ü|u)gbar|nicht\s+buchbar|volgeboekt|"
    r"niet\s+beschikbaar|uitverkocht|sold\s*out|not\s+available|fully\s+booked",
    re.IGNORECASE,
)
# A concrete price like "312,-" or "€ 481,50" strongly suggests availability
PRICE = re.compile(r"(€\s*)?\d{2,4}([.,]\d{2})?\s*,?-")


def log(msg: str) -> None:
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}", flush=True)


def notify(title: str, body: str, priority: str = "high") -> None:
    if NTFY_URL:
        try:
            req = urllib.request.Request(
                NTFY_URL,
                data=body.encode(),
                headers={
                    "Title": title.encode("ascii", "ignore").decode(),
                    "Priority": priority,
                    "Tags": "tent,tada" if priority == "high" else "warning",
                    "Click": PAGE_URL,
                },
                method="POST",
            )
            urllib.request.urlopen(req, timeout=15)
            log(f"ntfy sent: {title}")
        except Exception as e:
            log(f"ntfy failed: {e}")
    if HA_WEBHOOK_URL:
        try:
            payload = json.dumps({"title": title, "message": body, "url": PAGE_URL}).encode()
            req = urllib.request.Request(
                HA_WEBHOOK_URL, data=payload,
                headers={"Content-Type": "application/json"}, method="POST",
            )
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


def walk_json(obj, path=""):
    """Yield (path, dict) for every dict in a nested JSON structure."""
    if isinstance(obj, dict):
        yield path, obj
        for k, v in obj.items():
            yield from walk_json(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from walk_json(v, f"{path}[{i}]")


def scan_api_payloads(payloads: list) -> dict:
    """
    Heuristically extract availability info from captured booking-API JSON.
    Looks for objects that carry both a name/title and an availability/price signal.
    Returns {category_name: bool_available}.
    """
    found = {}
    for url, data in payloads:
        for _, d in walk_json(data):
            keys = {k.lower() for k in d.keys()}
            name = None
            for nk in ("name", "title", "label", "accommodation_name", "naam"):
                if nk in {k.lower() for k in d} :
                    for k in d:
                        if k.lower() == nk and isinstance(d[k], str) and d[k].strip():
                            name = d[k].strip()
                            break
                if name:
                    break
            if not name:
                continue
            avail = None
            for ak in ("available", "availability", "is_available", "beschikbaar", "bookable"):
                for k in d:
                    if k.lower() == ak:
                        v = d[k]
                        if isinstance(v, bool):
                            avail = v
                        elif isinstance(v, (int, float)):
                            avail = v > 0
                        elif isinstance(v, str):
                            avail = v.lower() in ("true", "yes", "1", "available")
            if avail is None and ("price" in keys or "prijs" in keys or "total_price" in keys):
                for k in d:
                    if k.lower() in ("price", "prijs", "total_price"):
                        v = d[k]
                        if isinstance(v, (int, float)):
                            avail = v > 0
            if avail is not None:
                found[name] = found.get(name, False) or avail
    return found


def scan_dom(page) -> dict:
    """
    Fallback: scan the rendered pitch cards for availability signals.
    Returns {category_name: bool_available}.
    """
    results = {}
    cards = page.query_selector_all("h4, h3")
    for h in cards:
        try:
            name = (h.inner_text() or "").strip()
        except Exception:
            continue
        if not name or len(name) > 60:
            continue
        # walk up to the card container and inspect its text
        container = h
        for _ in range(4):
            parent = container.evaluate_handle("el => el.parentElement")
            if parent is None:
                break
            container = parent.as_element() or container
        try:
            text = container.inner_text()
        except Exception:
            continue
        if NEGATIVE.search(text):
            results[name] = False
        elif PRICE.search(text) and ("Buchen" in text or "vanaf" in text or "ab " in text):
            results[name] = True
    return results


def set_dates_and_capture(play) -> tuple[dict, list]:
    browser = play.chromium.launch(headless=True)
    ctx = browser.new_context(locale="de-DE",
                              user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                                         "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
    page = ctx.new_page()

    payloads: list = []

    def on_response(resp):
        ct = (resp.headers or {}).get("content-type", "")
        if "json" not in ct:
            return
        url = resp.url
        # ignore analytics/tag-manager noise
        if any(x in url for x in ("googletagmanager", "google-analytics", "facebook",
                                  "doubleclick", "hotjar", "cookiebot")):
            return
        try:
            data = resp.json()
        except Exception:
            return
        payloads.append((url, data))

    page.on("response", on_response)

    # try several common query-param spellings for arrival/departure;
    # harmless if the site ignores them, and the widget below is the real path
    url = (f"{PAGE_URL}?arrival={ARRIVAL}&departure={DEPARTURE}"
           f"&aankomst={ARRIVAL}&vertrek={DEPARTURE}&persons=2")
    page.goto(url, wait_until="networkidle", timeout=60_000)

    # Try to drive the date widget: click "Datum hinzufügen", then pick dates.
    # Site widgets vary, so every step is best-effort.
    try:
        trigger = page.get_by_text(re.compile(r"Datum hinzufügen|Datum wählen|An- .*Abreise", re.I)).first
        trigger.click(timeout=5_000)
        page.wait_for_timeout(1_000)
        # date cells are usually buttons/tds with the day number; navigate to July 2026 if needed
        for _ in range(14):
            month_label = page.locator("text=/Juli\\s*2026|July\\s*2026|juli\\s*2026/i")
            if month_label.count() > 0:
                break
            nxt = page.locator("[class*=next], [aria-label*=next], [aria-label*=weiter], [aria-label*=volgende]").first
            nxt.click(timeout=3_000)
            page.wait_for_timeout(400)
        arr_day = str(int(ARRIVAL.split("-")[2]))
        dep_day = str(int(DEPARTURE.split("-")[2]))
        page.locator(f"td, button, [role=gridcell]").filter(has_text=re.compile(rf"^{arr_day}$")).first.click(timeout=3_000)
        page.wait_for_timeout(500)
        page.locator(f"td, button, [role=gridcell]").filter(has_text=re.compile(rf"^{dep_day}$")).first.click(timeout=3_000)
        page.wait_for_timeout(500)
        # confirm/search button if present
        for label in ("Suchen", "Zoeken", "Anwenden", "Übernehmen", "OK"):
            btn = page.get_by_text(label, exact=False)
            if btn.count() > 0:
                try:
                    btn.first.click(timeout=2_000)
                    break
                except Exception:
                    pass
        page.wait_for_load_state("networkidle", timeout=30_000)
    except Exception as e:
        log(f"date-widget interaction incomplete (falling back to defaults): {e}")

    page.wait_for_timeout(3_000)

    if DISCOVERY:
        DISCOVERY_DIR.mkdir(parents=True, exist_ok=True)
        for url_, data in payloads:
            h = hashlib.sha1(url_.encode()).hexdigest()[:10]
            fn = DISCOVERY_DIR / f"{h}.json"
            fn.write_text(json.dumps({"url": url_, "data": data}, indent=2, ensure_ascii=False))
        (DISCOVERY_DIR / "page.html").write_text(page.content())
        page.screenshot(path=str(DISCOVERY_DIR / "page.png"), full_page=True)
        log(f"discovery: dumped {len(payloads)} JSON payloads + page.html/png to {DISCOVERY_DIR}")

    api_avail = scan_api_payloads(payloads)
    dom_avail = scan_dom(page)

    browser.close()

    # merge: API wins over DOM heuristics
    merged = dict(dom_avail)
    merged.update(api_avail)
    return merged, payloads


def main() -> int:
    log(f"checking {PAGE_URL} for {ARRIVAL} → {DEPARTURE}")
    try:
        with sync_playwright() as p:
            availability, payloads = set_dates_and_capture(p)
    except Exception as e:
        log(f"ERROR: {e}")
        if NOTIFY_ON_ERROR:
            notify("Bakkum-Watcher Fehler", str(e), priority="default")
        return 1

    if not availability:
        log(f"no availability signals parsed ({len(payloads)} JSON payloads captured). "
            f"Run once with DISCOVERY=1 and inspect /data/discovery to refine parsing.")
        if NOTIFY_ON_ERROR:
            notify("Bakkum-Watcher: keine Daten erkannt",
                   "Parser fand keine Verfügbarkeitssignale – DISCOVERY=1 laufen lassen.",
                   priority="default")
        return 2

    state = load_state()
    prev = state.get("availability", {})
    newly_available = [name for name, ok in availability.items()
                       if ok and not prev.get(name, False)]

    for name, ok in sorted(availability.items()):
        log(f"  {'✅' if ok else '❌'} {name}")

    if newly_available:
        body = (f"Frei geworden für {ARRIVAL} – {DEPARTURE}:\n"
                + "\n".join(f"• {n}" for n in newly_available)
                + f"\n\nSchnell buchen: {PAGE_URL}")
        notify("🏕️ Camping Bakkum: Stellplatz frei!", body)

    state["availability"] = availability
    state["last_check"] = datetime.now().isoformat(timespec="seconds")
    save_state(state)
    return 0


if __name__ == "__main__":
    interval = int(os.environ.get("CHECK_INTERVAL", "0"))
    if interval > 0:
        while True:
            main()
            log(f"sleeping {interval}s")
            time.sleep(interval)
    else:
        sys.exit(main())
