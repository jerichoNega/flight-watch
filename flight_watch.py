#!/usr/bin/env python3
"""Flight watch: the SOONEST flight to Eindhoven that is not expensive.

Every run (GitHub Actions, ~5 min):
  1. Reads Telegram commands (/now, /budget N, /help).
  2. Fetches fares: Ryanair fare API for `ryanair_routes` (months ahead) and Google Flights
     for `google_routes` (next `google_days` days, every `google_every_min` min).
  3. Drops flights that can no longer be caught (departure < now + min_lead_hours).
  4. Picks: best = soonest flight with fare <= budget; soonest at any price; cheapest in the
     next `soon_days` days; cheapest overall. Departure airports are ranked with an estimated
     cost of getting there (to_airport_cost), only the fare is shown.
  5. Telegram alert when best changes (earlier date / price move) or a cheaper flight appears.
     No repeats of the same alert within `cooldown_hours`, silent at night, digest every morning.

Secrets: env TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID (or local telegram.env).
"""
import html
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
CFG = json.loads((HERE / "config.json").read_text())
DB = HERE / "prices.db"
TZ = ZoneInfo("Europe/Vienna")  # Vienna and Amsterdam share a time zone
RYANAIR = ("https://services-api.ryanair.com/farfnd/v4/oneWayFares/{o}/{d}/cheapestPerDay"
           "?outboundMonthOfDate={m}&currency=EUR")
CITY = {"VIE": "Vienna", "BTS": "Bratislava", "EIN": "Eindhoven"}
ICON = {"best": "⭐", "soonest": "⏩", "cheap_soon": "💶", "cheapest": "🏆"}


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


def now():
    return datetime.now(TZ)


def today():
    return now().date()


# ---------------------------------------------------------------- sources
# row = (day, src, orig, dest, fare, carrier, stops, dep "HH:MM", arr "HH:MM", link)

def months_ahead(n):
    y, m = today().year, today().month
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


def catchable(day, dep):
    """False if the flight leaves too soon to still get to the airport."""
    if day < today().isoformat():
        return False
    if not dep:
        return True
    try:
        t = datetime.strptime(f"{day} {dep}", "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
    except ValueError:
        return True
    return t >= now() + timedelta(hours=CFG["min_lead_hours"])


def fetch_ryanair():
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
                if f.get("unavailable") or f.get("soldOut") or not f.get("price"):
                    continue
                dep, arr = f.get("departureDate") or "", f.get("arrivalDate") or ""
                rows.append((f["day"], "ryanair", orig, dest, round(float(f["price"]["value"]), 2), "Ryanair", 0,
                             dep[11:16], arr[11:16], ryanair_link(orig, dest, f["day"])))
    if errors and not rows:
        raise RuntimeError(f"Ryanair: {errors} failed requests, no fares")
    return rows


def _hm(t, ref=None):
    """'HH:MM', plus ' +1' when it lands on a later date than ref."""
    hm = list(getattr(t, "time", None) or [])
    if not hm:
        return ""
    out = f"{hm[0]:02d}:{(hm[1] if len(hm) > 1 else 0):02d}"
    d, rd = list(getattr(t, "date", None) or []), list(getattr(ref, "date", None) or [])
    if ref is not None and d and rd and d > rd:
        try:
            from datetime import date as _d
            out += f" +{(_d(*d) - _d(*rd)).days}"
        except Exception:
            out += " +1"
    return out


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
                    res = [f for f in get_flights(q) if f.price and getattr(f, "flights", None)]
                    break
                except Exception as err:
                    if attempt:
                        log(f"google {orig}-{dest} {day}: {err!r}"[:160])
                    time.sleep(2)
            for f in sorted(res or [], key=lambda f: f.price):
                dep = _hm(f.flights[0].departure)
                if not catchable(day, dep):
                    continue  # cheapest one she can still make
                rows.append((day, "google", orig, dest, float(f.price), " / ".join(f.airlines),
                             len(f.flights) - 1, dep, _hm(f.flights[-1].arrival, f.flights[0].departure), google_link(orig, dest, day)))
                break
    return rows


# ---------------------------------------------------------------- state

def db():
    con = sqlite3.connect(DB)
    con.execute("CREATE TABLE IF NOT EXISTS state (k TEXT PRIMARY KEY, v TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS opts5 (day TEXT, src TEXT, orig TEXT, dest TEXT, fare REAL, carrier TEXT,"
                " stops INT, dep TEXT, arr TEXT, link TEXT, seen_at TEXT, PRIMARY KEY(day, src, orig, dest))")
    con.execute("CREATE TABLE IF NOT EXISTS sent (sig TEXT PRIMARY KEY, at TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS hist (day TEXT, orig TEXT, fare REAL, at TEXT)")
    for old in ("opts", "opts4", "prices", "latest", "options"):
        con.execute(f"DROP TABLE IF EXISTS {old}")
    return con


def get_state(con, k, default=None):
    r = con.execute("SELECT v FROM state WHERE k=?", (k,)).fetchone()
    return r[0] if r else default


def set_state(con, k, v):
    con.execute("INSERT OR REPLACE INTO state VALUES (?,?)", (k, str(v)))


def budget(con):
    return float(get_state(con, "budget", CFG["budget_eur"]))


def best_per_day(con):
    """{day: option}: per departure day the option with the lowest fare + cost of reaching that airport."""
    rows = con.execute("SELECT day, orig, dest, fare, carrier, stops, dep, arr, link FROM opts5").fetchall()
    cost = CFG["to_airport_cost"]
    out = {}
    for day, orig, dest, fare, carrier, stops, dep, arr, link in sorted(rows, key=lambda r: r[3] + cost.get(r[1], 0)):
        if day in out or not catchable(day, dep):
            continue
        out[day] = {"day": day, "orig": orig, "dest": dest, "fare": fare, "carrier": carrier, "stops": stops,
                    "dep": dep, "arr": arr, "link": link}
    return out


def picks(after, bud):
    if not after:
        return {}
    days = sorted(after)
    soon_end = (today() + timedelta(days=CFG["soon_days"])).isoformat()
    soon = [after[d] for d in days if d <= soon_end]
    ok = [after[d] for d in days if after[d]["fare"] <= bud]
    return {
        "best": ok[0] if ok else None,
        "soonest": after[days[0]],
        "cheap_soon": min(soon, key=lambda o: (o["fare"], o["day"])) if soon else None,
        "cheapest": min(after.values(), key=lambda o: (o["fare"], o["day"])),
    }


def record_history(con, after, stamp):
    """Store a fare per departure day only when it changed."""
    for d, o in after.items():
        last = con.execute("SELECT fare, orig FROM hist WHERE day=? ORDER BY at DESC LIMIT 1", (d,)).fetchone()
        if not last or abs(last[0] - o["fare"]) >= 0.5 or last[1] != o["orig"]:
            con.execute("INSERT INTO hist VALUES (?,?,?,?)", (d, o["orig"], o["fare"], stamp))
    con.execute("DELETE FROM hist WHERE day < ?", (today().isoformat(),))


def trend(con, o, hours=24):
    """Fare change for this day over the last `hours` (None if unknown)."""
    since = (now() - timedelta(hours=hours)).isoformat()
    r = con.execute("SELECT fare FROM hist WHERE day=? AND at<=? ORDER BY at DESC LIMIT 1", (o["day"], since)).fetchone()
    if not r:
        r = con.execute("SELECT fare FROM hist WHERE day=? ORDER BY at ASC LIMIT 1", (o["day"],)).fetchone()
    return round(o["fare"] - r[0]) if r else None


def recommend(con, after, p, bud):
    """Pick the flight with the best balance of arriving soon and paying little."""
    end = (today() + timedelta(days=CFG["recommend_days"])).isoformat()
    cands = [o for d, o in after.items() if d <= end]
    if not cands:
        return None
    vpd, cost = CFG["value_per_day_eur"], CFG["to_airport_cost"]

    def days_out(o):
        return (datetime.strptime(o["day"], "%Y-%m-%d").date() - today()).days

    def score(o):
        return o["fare"] + cost.get(o["orig"], 0) + days_out(o) * vpd

    rec = min(cands, key=lambda o: (score(o), o["day"]))
    why = []
    best = p.get("best")
    if best and best["day"] != rec["day"]:
        dd = days_out(rec) - days_out(best)
        saved = best["fare"] - rec["fare"]
        if dd > 0:
            why.append(f"{dd} day{'s' if dd > 1 else ''} later than the ⭐ flight but €{saved:.0f} cheaper")
        else:
            why.append(f"{-dd} day{'s' if dd < -1 else ''} earlier than the ⭐ flight for €{-saved:.0f} more")
    elif best:
        why.append("the soonest flight under budget is also the best value")
    else:
        why.append(f"nothing under €{bud:.0f}, this is the best balance of soon and cheap")
    if rec["fare"] > bud:
        why.append(f"€{rec['fare'] - bud:.0f} over your €{bud:.0f} budget")
    if rec["orig"] == "BTS":
        why.append("from Bratislava (a bit longer trip there)")
    t = trend(con, rec)
    if t is not None and t <= -3:
        why.append(f"just dropped €{-t} in 24h")
    elif t is not None and t >= 3:
        why.append(f"went up €{t} in 24h")
    n = days_out(rec)
    if n <= 14 or (t is not None and t > 0):
        advice = "Book now: low-cost fares rarely get cheaper this close to departure."
    elif rec["fare"] <= 30:
        advice = "Book now: this is already a rock-bottom fare."
    else:
        advice = "No rush yet: I keep watching and tell you if it goes up or something better shows up."
    return {"o": rec, "why": why, "advice": advice}


def day_drops(after, prev_days, bud):
    """Days in the next soon_days that got cheaper since the last run."""
    end = (today() + timedelta(days=CFG["soon_days"])).isoformat()
    out = []
    for d, o in sorted(after.items()):
        old = prev_days.get(d)
        if d <= end and old is not None and o["fare"] <= old - CFG["alert_change_eur"] \
                and o["fare"] <= bud * CFG["drop_max_vs_budget"]:
            out.append((o, old))
    return out


# ---------------------------------------------------------------- presentation

def e(s):
    return html.escape(str(s), quote=False)


def in_days(d):
    n = (datetime.strptime(d, "%Y-%m-%d").date() - today()).days
    return "today" if n == 0 else "tomorrow" if n == 1 else f"in {n} days"


def short_day(d):
    return datetime.strptime(d, "%Y-%m-%d").strftime("%a %d %b")


def flight_txt(o):
    stops = "direct" if not o["stops"] else f"{o['stops']} stop" + ("s" if o["stops"] > 1 else "")
    times = f"{o['dep']} → {o['arr']} · " if o.get("dep") else ""
    return f"{times}{o['carrier']} {stops} · {CITY.get(o['orig'], o['orig'])} → {CITY.get(o['dest'], o['dest'])}"


def bar(v, lo, hi, width=8):
    if hi <= lo:
        return "▰" * width
    n = 1 + round((v - lo) / (hi - lo) * (width - 1))
    return "▰" * n + "▱" * (width - n)


def titles(bud):
    return {"best": f"Soonest flight under €{bud:.0f}", "soonest": "Soonest flight (any price)",
            "cheap_soon": f"Cheapest in the next {CFG['soon_days']} days", "cheapest": "Cheapest overall"}


def block(key, title, o, prev=None, mark=""):
    delta = ""
    if prev:
        if prev["day"] != o["day"]:
            delta = f"  <i>(was {e(short_day(prev['day']))}, €{prev['fare']:.0f})</i>"
        elif abs(prev["fare"] - o["fare"]) >= 1:
            delta = f"  {'📉' if o['fare'] < prev['fare'] else '📈'} <s>€{prev['fare']:.0f}</s>"
    return (f"{ICON[key]} <b>{title}</b>{mark}\n"
            f"🗓 <b>{e(short_day(o['day']))}</b> · {e(in_days(o['day']))}\n"
            f"💶 <b>€{o['fare']:.0f}</b>{delta}\n"
            f"✈️ {e(flight_txt(o))}")


def buttons_for(p):
    labels = {"best": "⭐ Book best", "soonest": "⏩ Soonest", "cheap_soon": "💶 Cheapest 2 wks", "cheapest": "🏆 Cheapest"}
    seen, rows = set(), []
    for key in ("best", "soonest", "cheap_soon", "cheapest"):
        o = p.get(key)
        if not o or (o["day"], o["link"]) in seen:
            continue
        seen.add((o["day"], o["link"]))
        rows.append([{"text": f"{labels[key]}: {short_day(o['day'])} · €{o['fare']:.0f} →", "url": o["link"]}])
    return rows


def message(p, prev, changed, after, bud, headline, footer="", rec=None, drops=()):
    out = [headline, f"<i>{now():%a %d %b, %H:%M}</i>", ""]
    if drops:
        out.append("📉 <b>Got cheaper</b>")
        for o, old in drops:
            out.append(f"• {e(short_day(o['day']))} · <b>€{o['fare']:.0f}</b> <s>€{old:.0f}</s> · "
                       f"{e(o['dep'] or '')} {CITY.get(o['orig'], o['orig'])} · <a href=\"{e(o['link'])}\">book</a>")
        out.append("")
    if rec:
        o = rec["o"]
        out += [f"🧠 <b>My pick: {e(short_day(o['day']))} · €{o['fare']:.0f}</b>",
                f"✈️ {e(flight_txt(o))}",
                f"<i>Why: {e('; '.join(rec['why']))}.</i>",
                f"👉 {e(rec['advice'])}", ""]
    if not p.get("best"):
        out += [f"⭐ <b>No flight under €{bud:.0f}</b> in the next {CFG['months_ahead']} months.", ""]
    shown = []
    for key in ("best", "soonest", "cheap_soon", "cheapest"):
        o = p.get(key)
        if not o:
            continue
        t = titles(bud)[key]
        dup = next((k for k in shown if p[k]["day"] == o["day"] and p[k]["link"] == o["link"]), None)
        if dup:
            out.append(f"{ICON[key]} <b>{t}</b>: same as {ICON[dup]}\n")
        else:
            out.append(block(key, t, o, (prev or {}).get(key) if key in changed else None,
                             " 🆕" if key in changed else "") + "\n")
        shown.append(key)
    soon_end = (today() + timedelta(days=CFG["soon_days"])).isoformat()
    soon = [o for d, o in sorted(after.items()) if d <= soon_end]
    if soon:
        lo, hi = min(o["fare"] for o in soon), max(o["fare"] for o in soon)
        rows = [f"{short_day(o['day'])[:6]} {bar(o['fare'], lo, hi)} €{o['fare']:>4.0f} {o['orig']}"
                f"{' ↪' if o['stops'] else ''}{' ✓' if o['fare'] <= bud else ''}" for o in soon]
        out += [f"📅 <b>Next {CFG['soon_days']} days</b>", "<pre>" + e("\n".join(rows)) + "</pre>",
                f"<i>VIE Vienna · BTS Bratislava · ↪ stop · ✓ under €{bud:.0f}</i>"]
    if footer:
        out += ["", footer]
    buttons = buttons_for(p)
    if rec and not any(rec["o"]["link"] == b[0]["url"] and short_day(rec["o"]["day"]) in b[0]["text"] for b in buttons):
        buttons.insert(0, [{"text": f"🧠 My pick: {short_day(rec['o']['day'])} · €{rec['o']['fare']:.0f} →",
                            "url": rec["o"]["link"]}])
    elif rec:
        for b in buttons:
            if rec["o"]["link"] == b[0]["url"] and short_day(rec["o"]["day"]) in b[0]["text"]:
                b[0]["text"] = "🧠 " + b[0]["text"]
    return "\n".join(out), buttons


# ---------------------------------------------------------------- telegram

def tg(method, body):
    tok = secrets().get("TELEGRAM_BOT_TOKEN")
    if not tok:
        raise RuntimeError("telegram not configured")
    return http_json(f"https://api.telegram.org/bot{tok}/{method}", body, timeout=40)


def send(text, buttons=None):
    chat = secrets().get("TELEGRAM_CHAT_ID")
    if not chat:
        log("telegram not configured")
        return
    h = now().hour
    body = {"chat_id": chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True,
            "disable_notification": h >= CFG["quiet_from"] or h < CFG["quiet_until"]}
    if buttons:
        body["reply_markup"] = {"inline_keyboard": buttons}
    try:
        tg("sendMessage", body)
    except urllib.error.HTTPError as err:
        log(f"telegram failed: {err} {err.read()[:200]!r}")
    except Exception as err:
        log(f"telegram failed: {err!r}")


def read_commands(con):
    """Handle /now, /budget N, /help from the configured chat. Returns True if an overview was asked."""
    chat = str(secrets().get("TELEGRAM_CHAT_ID", ""))
    try:
        upd = tg("getUpdates", {"offset": int(get_state(con, "tg_offset", 0)), "timeout": 0}).get("result", [])
    except Exception as err:
        log(f"telegram getUpdates failed: {err!r}")
        return False
    want_now = False
    for u in upd:
        set_state(con, "tg_offset", u["update_id"] + 1)
        m = u.get("message") or {}
        if str(m.get("chat", {}).get("id")) != chat:
            continue
        txt = (m.get("text") or "").strip().lower()
        if txt.startswith("/budget"):
            arg = txt.split()[1].replace("€", "") if len(txt.split()) > 1 else ""
            if arg.isdigit() and 10 <= int(arg) <= 2000:
                set_state(con, "budget", int(arg))
                send(f"✅ Budget set to <b>€{int(arg)}</b>. Overview follows.")
                want_now = True
            else:
                send(f"Current budget: <b>€{budget(con):.0f}</b>. Change it with <code>/budget 80</code>.")
        elif txt.startswith("/now") or txt in ("now", "?", "status"):
            want_now = True
        elif txt.startswith("/help") or txt.startswith("/start"):
            send("✈️ <b>Flight watch</b> checks flights to Eindhoven every ~5 min.\n\n"
                 "/now  current soonest and cheapest flights\n"
                 "/budget 80  what counts as \"not expensive\"\n\n"
                 "Alerts come by themselves when the best flight changes. "
                 "Replies can take up to ~10 min (that is how often it wakes up).")
    return want_now


# ---------------------------------------------------------------- main

def sig(o):
    return {"day": o["day"], "fare": o["fare"], "link": o["link"]} if o else None


def alert_keys(p, prev):
    out = []
    b, pb = p.get("best"), prev.get("best")
    step = CFG["alert_change_eur"]
    if b and (not pb or pb["day"] != b["day"] or abs(pb["fare"] - b["fare"]) >= step):
        out.append("best")
    elif not b and pb:
        out.append("best")  # the affordable flight disappeared
    for k in ("cheap_soon", "cheapest"):
        o, po = p.get(k), prev.get(k)
        if o and po and o["fare"] <= po["fare"] - step:
            out.append(k)
    return out


def headline(fresh, p, prev, bud, digest_due):
    b, pb = p.get("best"), prev.get("best")
    if "best" in fresh:
        if not b:
            return f"⚠️ <b>No flight under €{bud:.0f} anymore</b>"
        if pb and b["day"] < pb["day"]:
            return "🎯 <b>Earlier affordable flight!</b>"
        if pb and b["day"] > pb["day"]:
            return "⚠️ <b>The best flight is gone or got pricier</b>"
        if pb and b["fare"] > pb["fare"]:
            return "📈 <b>Best flight got more expensive, book soon</b>"
        return "🔔 <b>Best flight got cheaper</b>"
    if fresh:
        return "🔔 <b>Price drop</b>"
    return "☀️ <b>Morning update</b>" if digest_due else "✈️ <b>Flight watch</b>"


def run(force_summary=False):
    con = db()
    t0 = now()
    want_now = read_commands(con) or force_summary
    failed, batches = [], []

    try:
        batches.append(("ryanair", fetch_ryanair()))
    except Exception as err:
        failed.append(repr(err))
    last_g = get_state(con, "google_at")
    if want_now or not last_g or t0 - datetime.fromisoformat(last_g) >= timedelta(minutes=CFG["google_every_min"]):
        g = fetch_google()
        if g:
            batches.append(("google", g))
            set_state(con, "google_at", t0.isoformat(timespec="seconds"))
        else:
            failed.append("Google Flights returned nothing")

    stamp = t0.isoformat(timespec="seconds")
    routes = {tuple(r) for r in CFG["ryanair_routes"] + CFG["google_routes"]}
    for src, rows in batches:
        con.execute("DELETE FROM opts5 WHERE src=?", (src,))
        for row in rows:
            con.execute("INSERT OR REPLACE INTO opts5 VALUES (?,?,?,?,?,?,?,?,?,?,?)", (*row, stamp))
    for orig, dest in con.execute("SELECT DISTINCT orig, dest FROM opts5").fetchall():
        if (orig, dest) not in routes:
            con.execute("DELETE FROM opts5 WHERE orig=? AND dest=?", (orig, dest))
    con.execute("DELETE FROM opts5 WHERE day < ?", (today().isoformat(),))
    con.execute("DELETE FROM sent WHERE at < ?", ((t0 - timedelta(days=2)).isoformat(),))

    day_key = today().isoformat()
    if get_state(con, "runs_day") != day_key:
        set_state(con, "runs_yesterday", get_state(con, "runs_today", 0))
        set_state(con, "runs_day", day_key)
        set_state(con, "runs_today", 0)
    set_state(con, "runs_today", int(get_state(con, "runs_today", 0)) + 1)
    con.commit()

    bud = budget(con)
    after = best_per_day(con)
    p = picks(after, bud)
    prev = json.loads(get_state(con, "picks", "{}"))
    budget_changed = prev.get("_budget") not in (None, bud)
    keys = alert_keys(p, prev) if prev and not budget_changed else []

    prev_days = json.loads(get_state(con, "days", "{}"))
    drops = []
    for o, old in day_drops(after, prev_days, bud) if prev_days else []:
        s = f"drop|{o['day']}|{o['fare']}"
        if con.execute("SELECT 1 FROM sent WHERE sig=?", (s,)).fetchone():
            continue
        drops.append((o, old))
        con.execute("INSERT OR REPLACE INTO sent VALUES (?,?)", (s, stamp))
    record_history(con, after, stamp)
    rec = recommend(con, after, p, bud)

    fresh = []  # cooldown: never the exact same alert twice within cooldown_hours
    for k in keys:
        s = f"{k}|{json.dumps(sig(p.get(k)))}"
        r = con.execute("SELECT at FROM sent WHERE sig=?", (s,)).fetchone()
        if r and t0 - datetime.fromisoformat(r[0]) < timedelta(hours=CFG["cooldown_hours"]):
            continue
        fresh.append(k)
        con.execute("INSERT OR REPLACE INTO sent VALUES (?,?)", (s, stamp))

    digest_due = t0.hour >= CFG["digest_hour"] and get_state(con, "digest_day") != day_key
    log(f"{sum(len(r) for _, r in batches)} fares from {[s for s, _ in batches]}, {len(after)} days, "
        f"alerts={fresh}, drops={len(drops)}" + (f", FAILED {failed}" if failed else ""))

    if p and (fresh or drops or want_now or digest_due or not prev):
        footer = ""
        if digest_due:
            footer = (f"<i>✅ Running: {get_state(con, 'runs_yesterday', 0)} checks yesterday. "
                      f"/now for a fresh look, /budget 80 to change the limit.</i>")
        head = headline(fresh, p, prev, bud, digest_due)
        if drops and not fresh:
            head = "📉 <b>Prices dropped</b>"
        text, buttons = message(p, prev, fresh, after, bud, head + " · to Eindhoven", footer, rec, drops)
        send(text, buttons)
        if digest_due:
            set_state(con, "digest_day", day_key)
    set_state(con, "days", json.dumps({d: o["fare"] for d, o in after.items()}))
    set_state(con, "picks", json.dumps({**{k: sig(o) for k, o in p.items()}, "_budget": bud}))

    if failed and get_state(con, "err_day") != day_key:
        send("⚠️ <b>Flight watch</b>: a price source failed, still using the other one\n<code>"
             + e("; ".join(failed)[:300]) + "</code>")
        set_state(con, "err_day", day_key)
    con.commit()
    con.close()


if __name__ == "__main__":
    run(force_summary="--summary" in sys.argv)
