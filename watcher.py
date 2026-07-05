#!/usr/bin/env python3
"""
Camping Bakkum availability watcher.

Scans ALL possible stays within a target window (default 2026-07-25 to
2026-07-31) that are at least MIN_NIGHTS long. For each candidate stay it
loads the JS-rendered camping page headless, sets the dates, intercepts
booking-API XHR responses and scans the rendered DOM for availability.

Notifies via ntfy and/or Home Assistant webhook when a (category, date range)
combination flips from unavailable to available -- i.e. a cancellation opened
a bookable gap, even if it's shorter than the full week.

Env vars:
  WINDOW_START     default 2026-07-25   (aliases: ARRIVAL)
  WINDOW_END       default 2026-07-31   (aliases: DEPARTURE)
  MIN_NIGHTS       default 4            minimum acceptable stay length
  PAGE_URL         default https://www.campingbakkum.de/ubernachten/campen
  NTFY_URL         e.g. https://ntfy.sh/bakkum-<random>
  HA_WEBHOOK_URL   e.g. https://ha.example.ch/api/webhook/bakkum_watch
  STATE_FILE       default /data/state.json
  DISCOVERY_DIR    default /data/discovery  (XHR dumps, only when DISCOVERY=1)
  DISCOVERY        1 = dump captured JSON/HTML/screenshot per checked range
  NOTIFY_ON_ERROR  1 = also notify when the check itself fails
  CHECK_INTERVAL   seconds between cycles; 0 = run once and exit
"""

import json
import os
import re
import sys
import time
import hashlib
import urllib.request
from datetime import datetime, date, timedelta
from pathlib import Path

from playwright.sync_api import sync_playwright

WINDOW_START = os.environ.get("WINDOW_START", os.environ.get("ARRIVAL", "2026-07-25"))
WINDOW_END = os.environ.get("WINDOW_END", os.environ.get("DEPARTURE", "2026-07-31"))
MIN_NIGHTS = int(os.environ.get("MIN_NIGHTS", "4"))
PAGE_URL = os.environ.get("PAGE_URL", "https://www.campingbakkum.de/ubernachten/campen")
NTFY_URL = os.environ.get("NTFY_URL", "")
HA_WEBHOOK_URL = os.environ.get("HA_WEBHOOK_URL", "")
STATE_FILE = Path(os.environ.get("STATE_FILE", "/data/state.json"))
DISCOVERY_DIR = Path(os.environ.get("DISCOVERY_DIR", "/data/discovery"))
DISCOVERY = os.environ.get("DISCOVERY", "0") == "1"
NOTIFY_ON_ERROR = os.environ.get("NOTIFY_ON_ERROR", "0") == "1"

NEGATIVE = re.compile(
    r"ausgebucht|nicht\s+verf(ü|u)gbar|nicht\s+buchbar|volgeboekt|"
    r"niet\s+beschikbaar|uitverkocht|sold\s*out|not\s+available|fully\s+booked",
    re.IGNORECASE,
)
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


def build_ranges(start_s: str, end_s: str, min_nights: int) -> list:
    """All (arrival, departure) sub-stays within the window with >= min_nights,
    longest first so the best option is checked and reported first."""
    start = date.fromisoformat(start_s)
    end = date.fromisoformat(end_s)
    total = (end - start).days
    ranges = []
    for nights in range(total, min_nights - 1, -1):
        for offset in range(0, total - nights + 1):
            arr = start + timedelta(days=offset)
            dep = arr + timedelta(days=nights)
            ranges.append((arr.isoformat(), dep.isoformat(), nights))
    return ranges


def walk_json(obj, path=""):
    if isinstance(obj, dict):
        yield path, obj
        for k, v in obj.items():
            yield from walk_json(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from walk_json(v, f"{path}[{i}]")


def scan_api_payloads(payloads: list) -> dict:
    """{category_name: bool_available} from captured booking-API JSON."""
    found = {}
    for url, data in payloads:
        for _, d in walk_json(data):
            name = None
            for k in d:
                if k.lower() in ("name", "title", "label", "accommodation_name", "naam") \
                        and isinstance(d[k], str) and d[k].strip():
                    name = d[k].strip()
                    break
            if not name:
                continue
            avail = None
            for k in d:
                kl = k.lower()
                v = d[k]
                if kl in ("available", "availability", "is_available", "beschikbaar", "bookable"):
                    if isinstance(v, bool):
                        avail = v
                    elif isinstance(v, (int, float)):
                        avail = v > 0
                    elif isinstance(v, str):
                        avail = v.lower() in ("true", "yes", "1", "available")
            if avail is None:
                for k in d:
                    if k.lower() in ("price", "prijs", "total_price") and isinstance(d[k], (int, float)):
                        avail = d[k] > 0
            if avail is not None:
                found[name] = found.get(name, False) or avail
    return found


def scan_dom(page) -> dict:
    """Fallback: scan rendered pitch cards. {category_name: bool_available}."""
    results = {}
    for h in page.query_selector_all("h4, h3"):
        try:
            name = (h.inner_text() or "").strip()
        except Exception:
            continue
        if not name or len(name) > 60:
            continue
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


def set_dates(page, arrival: str, departure: str) -> None:
    """Best-effort interaction with the site's date widget."""
    trigger = page.get_by_text(re.compile(r"Datum hinzufügen|Datum wählen|An- .*Abreise", re.I)).first
    trigger.click(timeout=5_000)
    page.wait_for_timeout(1_000)
    target = date.fromisoformat(arrival)
    month_re = {7: r"Juli\s*2026|July\s*2026|juli\s*2026"}.get(target.month, r"Juli\s*2026")
    for _ in range(14):
        if page.locator(f"text=/{month_re}/i").count() > 0:
            break
        nxt = page.locator("[class*=next], [aria-label*=next], [aria-label*=weiter], [aria-label*=volgende]").first
        nxt.click(timeout=3_000)
        page.wait_for_timeout(400)
    arr_day = str(int(arrival.split("-")[2]))
    dep_day = str(int(departure.split("-")[2]))
    cells = "td, button, [role=gridcell]"
    page.locator(cells).filter(has_text=re.compile(rf"^{arr_day}$")).first.click(timeout=3_000)
    page.wait_for_timeout(500)
    page.locator(cells).filter(has_text=re.compile(rf"^{dep_day}$")).first.click(timeout=3_000)
    page.wait_for_timeout(500)
    for label in ("Suchen", "Zoeken", "Anwenden", "Übernehmen", "OK"):
        btn = page.get_by_text(label, exact=False)
        if btn.count() > 0:
            try:
                btn.first.click(timeout=2_000)
                break
            except Exception:
                pass
    page.wait_for_load_state("networkidle", timeout=30_000)


def check_range(page, payloads: list, arrival: str, departure: str) -> dict:
    """Check one (arrival, departure) stay. Returns {category: bool}."""
    payloads.clear()
    url = (f"{PAGE_URL}?arrival={arrival}&departure={departure}"
           f"&aankomst={arrival}&vertrek={departure}&persons=2")
    page.goto(url, wait_until="networkidle", timeout=60_000)
    try:
        set_dates(page, arrival, departure)
    except Exception as e:
        log(f"  date-widget interaction incomplete for {arrival}→{departure}: {e}")
    page.wait_for_timeout(2_000)

    if DISCOVERY:
        d = DISCOVERY_DIR / f"{arrival}_{departure}"
        d.mkdir(parents=True, exist_ok=True)
        for url_, data in payloads:
            h = hashlib.sha1(url_.encode()).hexdigest()[:10]
            (d / f"{h}.json").write_text(
                json.dumps({"url": url_, "data": data}, indent=2, ensure_ascii=False))
        (d / "page.html").write_text(page.content())
        page.screenshot(path=str(d / "page.png"), full_page=True)
        log(f"  discovery: {len(payloads)} payloads dumped to {d}")

    merged = scan_dom(page)
    merged.update(scan_api_payloads(payloads))   # API wins over DOM heuristics
    return merged


def maximal_ranges(ranges: list) -> list:
    """Drop ranges fully contained in another available range (same category)."""
    out = []
    parsed = [(date.fromisoformat(a), date.fromisoformat(b)) for a, b in ranges]
    for i, (a1, b1) in enumerate(parsed):
        contained = any(j != i and a2 <= a1 and b1 <= b2 for j, (a2, b2) in enumerate(parsed))
        if not contained:
            out.append(ranges[i])
    return out


def fmt_range(a: str, b: str) -> str:
    da, db = date.fromisoformat(a), date.fromisoformat(b)
    return f"{da.strftime('%d.%m.')}–{db.strftime('%d.%m.')} ({(db - da).days} Nächte)"


def main() -> int:
    ranges = build_ranges(WINDOW_START, WINDOW_END, MIN_NIGHTS)
    log(f"checking {PAGE_URL}: window {WINDOW_START} → {WINDOW_END}, "
        f"min {MIN_NIGHTS} nights → {len(ranges)} stay combinations")

    # availability[category] = set of "arrival|departure" strings that are bookable
    availability: dict = {}
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(
                locale="de-DE",
                user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
            page = ctx.new_page()
            payloads: list = []

            def on_response(resp):
                ct = (resp.headers or {}).get("content-type", "")
                if "json" not in ct:
                    return
                if any(x in resp.url for x in ("googletagmanager", "google-analytics",
                                               "facebook", "doubleclick", "hotjar", "cookiebot")):
                    return
                try:
                    payloads.append((resp.url, resp.json()))
                except Exception:
                    pass

            page.on("response", on_response)

            any_signal = False
            for arrival, departure, nights in ranges:
                result = check_range(page, payloads, arrival, departure)
                if result:
                    any_signal = True
                for cat, ok in sorted(result.items()):
                    log(f"  [{arrival}→{departure}] {'✅' if ok else '❌'} {cat}")
                    if ok:
                        availability.setdefault(cat, set()).add(f"{arrival}|{departure}")
            browser.close()
    except Exception as e:
        log(f"ERROR: {e}")
        if NOTIFY_ON_ERROR:
            notify("Bakkum-Watcher Fehler", str(e), priority="default")
        return 1

    if not any_signal:
        log("no availability signals parsed in any range. "
            "Run once with DISCOVERY=1 and inspect /data/discovery to refine parsing.")
        if NOTIFY_ON_ERROR:
            notify("Bakkum-Watcher: keine Daten erkannt",
                   "Parser fand keine Verfügbarkeitssignale – DISCOVERY=1 laufen lassen.",
                   priority="default")
        return 2

    state = load_state()
    prev = {cat: set(v) for cat, v in state.get("availability", {}).items()}

    lines = []
    for cat, ranges_now in sorted(availability.items()):
        new = ranges_now - prev.get(cat, set())
        if not new:
            continue
        # report only maximal new ranges (a free full week implies free sub-stays)
        max_new = maximal_ranges([tuple(r.split("|")) for r in new])
        pretty = ", ".join(fmt_range(a, b) for a, b in
                           sorted(max_new, key=lambda x: (x[0], x[1])))
        lines.append(f"• {cat}: {pretty}")

    if lines:
        body = ("Frei geworden im Fenster "
                f"{fmt_range(WINDOW_START, WINDOW_END)}:\n"
                + "\n".join(lines)
                + f"\n\nSchnell buchen: {PAGE_URL}")
        notify("🏕️ Camping Bakkum: Stellplatz frei!", body)

    state["availability"] = {cat: sorted(v) for cat, v in availability.items()}
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
