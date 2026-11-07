#!/usr/bin/env python3
"""Flight watcher Vienna -> Eindhoven: the SOONEST flight that is not expensive (fare <= budget_eur),
plus the soonest at any price and the cheapest.

Sources:
  - Ryanair fare API for every route in config `ryanair_routes` (Vienna and Bratislava,
    to Eindhoven and nearby airports). Lowest fare per day, months ahead.
  - Google Flights (faster-flights) for `google_routes`, next `google_days` days (KLM, Austrian, ...).
Every option is compared on total door cost: fare + bus to the departure airport + train from
the arrival airport (estimates in config).

Ranking adds an estimated cost of getting to each departure airport (to_airport_cost).
Telegram message whenever the soonest flight, the cheapest flight in the next 2 weeks or the
cheapest flight overall changes, plus one morning digest.

  flight_watch.py              normal check
  flight_watch.py --summary    send the overview now
"""
import html
import json
import os
import platform
import sqlite3
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
CFG = json.loads((HERE / "config.json").read_text())
DB = HERE / "prices.db"
TZ = ZoneInfo("Europe/Amsterdam")
RYANAIR = ("https://services-api.ryanair.com/farfnd/v4/oneWayFares/{o}/{d}/cheapestPerDay"
           "?outboundMonthOfDate={m}&currency=EUR")
HOME, DEST = CFG["home"], CFG["destination"]
CITY = {"VIE": "Vienna", "BTS": "Bratislava", "EIN": "Eindhoven", "AMS": "Amsterdam", "CRL": "Charleroi",
        "CGN": "Cologne", "DUS": "Düsseldorf", "NRN": "Weeze", "BRU": "Brussels"}


def log(msg):
    print(f"{datetime.now(TZ):%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def http_json(url, data=None, timeout=30):
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(data).encode()
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def secrets():
    out = {}
    p = HERE / "telegram.env"
    if p.exists():
        for line in p.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"')
    for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        if os.environ.get(k):
            out[k] = os.environ[k]
    return out


# ---------------------------------------------------------------- sources

def today():
    return datetime.now(TZ).date()


def months_ahead(n):
    t = today()
    y, m = t.year, t.month
    for _ in range(n):
        yield f"{y}-{m:02d}-01"
        m += 1
        if m > 12:
            y, m = y + 1, 1


def ryanair_link(orig, dest, day):
    return (f"https://www.ryanair.com/gb/en/trip/flights/select?adults=1&teens=0&children=0&infants=0"
            f"&dateOut={day}&isReturn=false&originIata={orig}&destinationIata={dest}")


def google_link(orig, dest, day):
    q = f"Flights to {dest} from {orig} on {day} oneway"
    return "https://www.google.com/travel/flights?q=" + urllib.parse.quote(q) + "&curr=EUR"


def fetch_ryanair():
    """[(day, src, orig, dest, fare, carrier, stops, link)]"""
    t = today().isoformat()
    rows, errors = [], 0
    for orig, dest in CFG["ryanair_routes"]:
        for m in months_ahead(CFG["months_ahead"]):
            try:
                data = http_json(RYANAIR.format(o=orig, d=dest, m=m))
            except urllib.error.HTTPError as err:
                if err.code in (400, 404):
                    break
                errors += 1
                continue
            except Exception:
                errors += 1
                continue
            for f in data.get("outbound", {}).get("fares", []):
                if f.get("unavailable") or f.get("soldOut") or not f.get("price") or f["day"] < t:
                    continue
                rows.append((f["day"], "ryanair", orig, dest, round(float(f["price"]["value"]), 2),
                             "Ryanair", 0, ryanair_link(orig, dest, f["day"])))
    if errors and not rows:
        raise RuntimeError(f"Ryanair: {errors} failed requests, no fares")
    return rows


def fetch_google():
    from fast_flights import FlightQuery, Passengers, create_query, get_flights
    rows = []
    for i in range(CFG["google_days"] + 1):
        day = (today() + timedelta(days=i)).isoformat()
        for orig, dest in CFG["google_routes"]:
            q = create_query(flights=[FlightQuery(date=day, from_airport=orig, to_airport=dest)],
                             trip="one-way", seat="economy", passengers=Passengers(adults=1),
                             language="en-US", currency="EUR")
            res = None
            for attempt in range(2):
                try:
                    res = [f for f in get_flights(q) if f.price]
                    break
                except Exception as err:
                    if attempt:
                        log(f"google {orig}-{dest} {day}: {err!r}"[:160])
                    time.sleep(2)
            if not res:
                continue
            best = min(res, key=lambda f: f.price)
            stops = max(len(best.flights) - 1, 0) if getattr(best, "flights", None) else 0
            rows.append((day, "google", orig, dest, float(best.price), " / ".join(best.airlines), stops,
                         google_link(orig, dest, day)))
    return rows


# ---------------------------------------------------------------- state

def db():
    con = sqlite3.connect(DB)
    con.execute("CREATE TABLE IF NOT EXISTS prices (route TEXT, day TEXT, price REAL, seen_at TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS state (k TEXT PRIMARY KEY, v TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS opts4 (day TEXT, src TEXT, orig TEXT, dest TEXT, fare REAL, carrier TEXT,"
                " stops INT, link TEXT, seen_at TEXT, PRIMARY KEY(day, src, orig, dest))")
    return con


def get_state(con, k):
    r = con.execute("SELECT v FROM state WHERE k=?", (k,)).fetchone()
    return r[0] if r else None


def set_state(con, k, v):
    con.execute("INSERT OR REPLACE INTO state VALUES (?,?)", (k, str(v)))


def extra_cost(orig, dest):
    return CFG["to_airport_cost"].get(orig, 0) + CFG["from_airport_cost"].get(dest, 0)


def best_per_day(con):
    """{day: option} cheapest door-to-door option per departure day."""
    rows = con.execute("SELECT day, orig, dest, fare, carrier, stops, link FROM opts4 WHERE day>=?",
                       (today().isoformat(),)).fetchall()
    out = {}
    for day, orig, dest, fare, carrier, stops, link in sorted(rows, key=lambda r: r[3] + extra_cost(r[1], r[2])):
        x = extra_cost(orig, dest)
        out.setdefault(day, {"day": day, "orig": orig, "dest": dest, "fare": fare, "extra": x,
                             "total": round(fare + x, 2), "carrier": carrier, "stops": stops, "link": link})
    return out


def picks(after):
    """The three things Jericho cares about."""
    if not after:
        return {}
    days = sorted(after)
    soon_end = (today() + timedelta(days=CFG["soon_days"])).isoformat()
    soon = [after[d] for d in days if d <= soon_end]
    ok = [after[d] for d in days if after[d]["fare"] <= CFG["budget_eur"]]
    return {
        "best": ok[0] if ok else None,
        "soonest": after[days[0]],
        "cheap_soon": min(soon, key=lambda o: (o["total"], o["day"])) if soon else None,
        "cheapest": min(after.values(), key=lambda o: (o["total"], o["day"])),
    }


# ---------------------------------------------------------------- presentation

def e(s):
    return html.escape(str(s), quote=False)


def in_days(d):
    n = (datetime.strptime(d, "%Y-%m-%d").date() - today()).days
    return "today" if n == 0 else "tomorrow" if n == 1 else f"in {n} days"


def short_day(d):
    return datetime.strptime(d, "%Y-%m-%d").strftime("%a %d %b")


def route_txt(o):
    stops = "direct" if not o["stops"] else f"{o['stops']} stop" + ("s" if o["stops"] > 1 else "")
    return f"{o['carrier']} · {stops} · {CITY.get(o['orig'], o['orig'])} → {CITY.get(o['dest'], o['dest'])}"


def price_txt(o):
    return f"<b>€{o['fare']:.0f}</b>"


def bar(v, lo, hi, width=8):
    if hi <= lo:
        return "▰" * width
    n = 1 + round((v - lo) / (hi - lo) * (width - 1))
    return "▰" * n + "▱" * (width - n)


def block(icon, title, o, prev=None):
    delta = ""
    if prev:
        if prev["day"] != o["day"]:
            delta = f"  <i>(was {e(short_day(prev['day']))} €{prev.get('fare', prev['total']):.0f})</i>"
        elif abs(prev.get("fare", prev["total"]) - o["fare"]) >= 1:
            arrow = "📉" if o["fare"] < prev.get("fare", prev["total"]) else "📈"
            delta = f"  {arrow} <s>€{prev.get('fare', prev['total']):.0f}</s>"
    return (f"{icon} <b>{title}</b>\n"
            f"🗓 {e(short_day(o['day']))} · {e(in_days(o['day']))}\n"
            f"💶 {price_txt(o)}{delta}\n"
            f"✈️ {e(route_txt(o))}")


def buttons_for(p):
    seen, rows = set(), []
    for icon, key in (("⭐ Best", "best"), ("⏩ Soonest", "soonest"), ("💶 Cheapest 2 wks", "cheap_soon"),
                      ("🏆 Cheapest", "cheapest")):
        o = p.get(key)
        if not o or (o["day"], o["link"]) in seen:
            continue
        seen.add((o["day"], o["link"]))
        rows.append([{"text": f"{icon}: {short_day(o['day'])} · €{o['fare']:.0f} →", "url": o["link"]}])
    return rows


def update_message(p, prev, changed, after):
    head = "🔔 <b>Update</b>" if prev else "✈️ <b>Flight watch</b>"
    out = [f"{head} · flights to {CITY[DEST]}", f"<i>{datetime.now(TZ):%a %d %b, %H:%M}</i>", ""]
    titles = {"best": ("⭐", f"Soonest flight under €{CFG['budget_eur']}"),
              "soonest": ("⏩", "Soonest flight (any price)"),
              "cheap_soon": ("💶", f"Cheapest in the next {CFG['soon_days']} days"),
              "cheapest": ("🏆", "Cheapest overall")}
    shown = []
    for key in ("best", "soonest", "cheap_soon", "cheapest"):
        o = p.get(key)
        if not o:
            continue
        dup = next((k for k in shown if p[k]["day"] == o["day"] and p[k]["link"] == o["link"]), None)
        icon, title = titles[key]
        mark = " 🆕" if key in changed else ""
        if dup:
            out.append(f"{icon} <b>{title}</b>{mark}: same as above\n")
        else:
            out.append(block(icon, title + mark, o, (prev or {}).get(key) if key in changed else None) + "\n")
        shown.append(key)
    # 14-day chart
    soon_end = (today() + timedelta(days=CFG["soon_days"])).isoformat()
    soon = [o for d, o in sorted(after.items()) if d <= soon_end]
    if soon:
        lo, hi = min(o["fare"] for o in soon), max(o["fare"] for o in soon)
        rows = []
        for o in soon:
            rows.append(f"{short_day(o['day'])[:6]} {bar(o['fare'], lo, hi)} €{o['fare']:>4.0f} {o['orig']}{' ↪' if o['stops'] else ''}")
        out.append(f"📅 <b>Next {CFG['soon_days']} days</b>")
        out.append("<pre>" + e("\n".join(rows)) + "</pre>")
        out.append("<i>VIE = from Vienna · BTS = from Bratislava · ↪ with a stop</i>")
    return "\n".join(out), buttons_for(p)


# ---------------------------------------------------------------- notify

def send_telegram(text, buttons=None):
    s = secrets()
    tok, chat = s.get("TELEGRAM_BOT_TOKEN"), s.get("TELEGRAM_CHAT_ID")
    if not (tok and chat):
        log("telegram not configured")
        return False
    body = {"chat_id": chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if buttons:
        body["reply_markup"] = {"inline_keyboard": buttons}
    http_json(f"https://api.telegram.org/bot{tok}/sendMessage", body)
    return True


def notify(text, buttons=None, plain=None):
    try:
        send_telegram(text, buttons)
    except urllib.error.HTTPError as err:
        log(f"telegram failed: {err} {err.read()[:300]!r}")
    except Exception as err:
        log(f"telegram failed: {err!r}")
    if platform.system() == "Darwin" and plain:
        plain = plain.replace('"', "'")
        subprocess.run(["osascript", "-e", f'display notification "{plain}" with title "Flight watch"'], check=False)


# ---------------------------------------------------------------- main

def sig(o):
    return {"day": o["day"], "total": o["total"], "fare": o["fare"], "link": o["link"]} if o else None


def changed_keys(p, prev):
    out = []
    for k, o in p.items():
        old = prev.get(k)
        if o is None:
            continue
        if old is None or old["day"] != o["day"] or abs(old["total"] - o["total"]) >= CFG["min_change_eur"]:
            out.append(k)
    return out


def run(summary=False):
    con = db()
    now = datetime.now(TZ)
    failed = []

    batches = []
    try:
        batches.append(("ryanair", fetch_ryanair()))
    except Exception as err:
        failed.append(repr(err))
    last_g = get_state(con, "google_at")
    if summary or not last_g or now - datetime.fromisoformat(last_g) >= timedelta(minutes=CFG["google_every_min"]):
        g = fetch_google()
        if g:
            batches.append(("google", g))
            set_state(con, "google_at", now.isoformat(timespec="seconds"))
        else:
            failed.append("Google Flights returned nothing")

    stamp = now.isoformat(timespec="seconds")
    for src, rows in batches:
        con.execute("DELETE FROM opts4 WHERE src=?", (src,))  # full refresh per source
        for row in rows:
            con.execute("INSERT OR REPLACE INTO opts4 VALUES (?,?,?,?,?,?,?,?,?)", (*row, stamp))
            con.execute("INSERT INTO prices VALUES (?,?,?,?)", (f"{row[1]}:{row[2]}-{row[3]}", row[0], row[4], stamp))
    con.execute("DELETE FROM prices WHERE seen_at < ?", ((now - timedelta(days=3)).isoformat(),))
    con.execute("DELETE FROM opts4 WHERE day < ?", (today().isoformat(),))
    routes = {tuple(r) for r in CFG["ryanair_routes"] + CFG["google_routes"]}
    for orig, dest in con.execute("SELECT DISTINCT orig, dest FROM opts4").fetchall():
        if (orig, dest) not in routes:
            con.execute("DELETE FROM opts4 WHERE orig=? AND dest=?", (orig, dest))
    con.commit()

    after = best_per_day(con)
    p = picks(after)
    prev = json.loads(get_state(con, "picks") or "{}")
    changed = changed_keys(p, prev) if prev else list(p)

    # morning digest so there is news every day even without changes
    digest_due = False
    slot = f"{now.date()}"
    if now.hour >= CFG["digest_hour"] and get_state(con, "digest_day") != slot:
        digest_due = True

    log(f"{sum(len(r) for _, r in batches)} fares from {[s for s, _ in batches]}, {len(after)} days, "
        f"changed={changed}" + (f", FAILED {failed}" if failed else ""))

    if p and (changed or summary or digest_due):
        text, buttons = update_message(p, prev, changed if prev else [], after)
        notify(text, buttons, plain=" · ".join(f"{k} {short_day(p[k]['day'])} €{p[k]['fare']:.0f}" for k in changed))
        set_state(con, "digest_day", slot)
    set_state(con, "picks", json.dumps({k: sig(o) for k, o in p.items()}))

    if failed and get_state(con, "err_day") != slot:
        notify("⚠️ <b>Flight watch</b>: a price source failed\n<code>" + e("; ".join(failed)[:300]) + "</code>")
        set_state(con, "err_day", slot)
    con.commit()
    con.close()


if __name__ == "__main__":
    run(summary="--summary" in sys.argv)
