#!/usr/bin/env python3
"""Scrape https://vrede.be/nl/beweging/kalender into an iCalendar feed.

Events are remembered in events.json so they stay in subscribers' calendars
after they drop off the (upcoming-only) website listing.
"""
import hashlib
import html
import json
import re
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = "https://vrede.be"
LIST_URL = BASE + "/nl/beweging/kalender"
OUT_DIR = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
STORE = OUT_DIR / "events.json"
ICS = OUT_DIR / "vrede.ics"
KEEP_PAST_DAYS = 365
UA = "Mozilla/5.0 (vrede-ical feed builder)"


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def text(fragment):
    fragment = re.sub(r"<br\s*/?>|</p>", "\n", fragment, flags=re.I)
    fragment = re.sub(r"<[^>]+>", "", fragment)
    lines = [re.sub(r"[ \t]+", " ", l).strip() for l in html.unescape(fragment).split("\n")]
    return "\n".join(l for l in lines if l)


def field(page, name):
    """Inner HTML of the first Drupal field block with the given machine name."""
    m = re.search(
        r'class="[^"]*field--name-%s[^"]*"[^>]*>(.*?)</div>\s*(?:</div>|<div)' % re.escape(name),
        page, re.S)
    return m.group(1) if m else ""


def parse_listing(page):
    events = []
    for card in re.findall(r'<article class="node-card.*?</article>', page, re.S):
        link = re.search(r'node-card__title">\s*<a\s+href="([^"]+)"[^>]*>(.*?)</a>', card, re.S)
        times = re.findall(r'<time\s+datetime="([^"]+)"', card)
        if not link or not times:
            continue
        tag = re.search(r'node-card__image-tag">(.*?)<', card)
        events.append({
            "url": BASE + link.group(1),
            "title": text(link.group(2)),
            "start": times[0],
            "end": times[1] if len(times) > 1 else None,
            "category": text(tag.group(1)) if tag else "",
            "teaser": text(field(card, "field-teaser")),
        })
    return events


def enrich(ev):
    page = fetch(ev["url"])
    addr = re.search(r'<p class="address"[^>]*>(.*?)</p>', page, re.S)
    ev["location"] = ", ".join(text(addr.group(1)).split("\n")) if addr else ""
    ev["body"] = text(field(page, "body")) or ev["teaser"]
    org = re.search(r'field--name-field-organization.*?class="field__item">(.*?)</div>', page, re.S)
    ev["organizer"] = text(org.group(1)) if org else ""


def parse_dt(s):
    if "T" not in s:
        return datetime.fromisoformat(s).date()
    return datetime.fromisoformat(s).astimezone(timezone.utc)


def esc(s):
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def fold(line):
    out, b = [], line.encode("utf-8")
    while len(b) > 75:
        cut = 75 if not out else 74
        while (b[cut] & 0xC0) == 0x80:  # don't split a UTF-8 sequence
            cut -= 1
        out.append(b[:cut].decode())
        b = b[cut:]
    out.append(b.decode())
    return "\r\n ".join(out)


def vevent(ev, stamp):
    start = parse_dt(ev["start"])
    end = parse_dt(ev["end"]) if ev.get("end") else None
    lines = ["BEGIN:VEVENT",
             "UID:%s@vrede-ical" % hashlib.sha1(ev["url"].encode()).hexdigest()[:16],
             "DTSTAMP:" + stamp]
    if isinstance(start, datetime):
        lines.append("DTSTART:" + start.strftime("%Y%m%dT%H%M%SZ"))
        end = end if isinstance(end, datetime) and end > start else start + timedelta(hours=2)
        lines.append("DTEND:" + end.strftime("%Y%m%dT%H%M%SZ"))
    else:
        end = end if end and end >= start else start
        lines.append("DTSTART;VALUE=DATE:" + start.strftime("%Y%m%d"))
        lines.append("DTEND;VALUE=DATE:" + (end + timedelta(days=1)).strftime("%Y%m%d"))
    desc = "\n\n".join(x for x in [ev.get("body") or ev.get("teaser", ""),
                                   ("Organisatie: " + ev["organizer"]) if ev.get("organizer") else "",
                                   ev["url"]] if x)
    lines += ["SUMMARY:" + esc(ev["title"]),
              "DESCRIPTION:" + esc(desc),
              "URL:" + ev["url"]]
    if ev.get("location"):
        lines.append("LOCATION:" + esc(ev["location"]))
    if ev.get("category"):
        lines.append("CATEGORIES:" + esc(ev["category"]))
    lines.append("END:VEVENT")
    return [fold(l) for l in lines]


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    store = json.loads(STORE.read_text()) if STORE.exists() else {}
    listed = parse_listing(fetch(LIST_URL))
    if not listed:
        sys.exit("No events found on %s - page layout may have changed" % LIST_URL)
    for ev in listed:
        try:
            enrich(ev)
        except Exception as e:  # keep the listing data if a detail page fails
            print("warning: %s: %s" % (ev["url"], e), file=sys.stderr)
            old = store.get(ev["url"], {})
            ev.setdefault("location", old.get("location", ""))
            ev.setdefault("body", old.get("body", ev["teaser"]))
        store[ev["url"]] = ev
        time.sleep(0.5)

    # Upcoming events that vanished from the site were cancelled; past ones are kept a while.
    now = datetime.now(timezone.utc)
    listed_urls = {e["url"] for e in listed}
    cutoff = now - timedelta(days=KEEP_PAST_DAYS)

    def end_of(e):
        d = parse_dt(e.get("end") or e["start"])
        return d if isinstance(d, datetime) else datetime(d.year, d.month, d.day, tzinfo=timezone.utc) + timedelta(days=1)

    store = {u: e for u, e in store.items()
             if u in listed_urls or cutoff < end_of(e) < now}

    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    out = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//vrede-ical//NL", "CALSCALE:GREGORIAN",
           "METHOD:PUBLISH", "X-WR-CALNAME:Vrede vzw kalender", "X-WR-TIMEZONE:Europe/Brussels",
           "X-WR-CALDESC:Activiteiten van " + LIST_URL,
           "REFRESH-INTERVAL;VALUE=DURATION:PT6H", "X-PUBLISHED-TTL:PT6H"]
    for ev in sorted(store.values(), key=lambda e: e["start"]):
        out += vevent(ev, stamp)
    out.append("END:VCALENDAR")

    ICS.write_text("\r\n".join(out) + "\r\n", encoding="utf-8", newline="")
    STORE.write_text(json.dumps(store, ensure_ascii=False, indent=1, sort_keys=True))
    print("wrote %d events (%d on site) to %s" % (len(store), len(listed), ICS))


if __name__ == "__main__":
    main()
