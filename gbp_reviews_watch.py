#!/usr/bin/env python3
"""
GBP Reviews Watcher -> Telegram
FREITTY / 26 locations, one GBP account.

Modes:
  python gbp_reviews_watch.py --auth      one-time: get refresh_token
  python gbp_reviews_watch.py --locations one-time / weekly: refresh location cache
  python gbp_reviews_watch.py --init      seed state, send nothing (first run)
  python gbp_reviews_watch.py            normal poll cycle (cron)
  python gbp_reviews_watch.py --dry-run   poll, print to stdout, send nothing

Config: env vars (see .env.example) or environment.
State:  ./state/ directory next to the script.
"""

import json
import os
import sys
import time
import html
import pathlib
import datetime as dt
import requests

# ---------------------------------------------------------------- config

CLIENT_ID = os.environ.get("GBP_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("GBP_CLIENT_SECRET", "")
REFRESH_TOKEN = os.environ.get("GBP_REFRESH_TOKEN", "")
# One or more account ids, comma-separated. Numeric only, no "accounts/" prefix.
ACCOUNT_IDS = [a.strip() for a in os.environ.get("GBP_ACCOUNT_ID", "").split(",") if a.strip()]

TG_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TG_CHAT_ID", "")

SCOPE = "https://www.googleapis.com/auth/business.manage"
TOKEN_URL = "https://oauth2.googleapis.com/token"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
REDIRECT = "http://localhost:8080/"

API_INFO = "https://mybusinessbusinessinformation.googleapis.com/v1"
API_ACCT = "https://mybusinessaccountmanagement.googleapis.com/v1"
API_V4 = "https://mybusiness.googleapis.com/v4"

BATCH_SIZE = 10          # max locationNames per batchGetReviews call
PAGE_SIZE = 20           # reviews per location per call; 20 is plenty for a 5-min cycle
REQ_DELAY = 0.25         # smooth request distribution (Google guidance)
MAX_SEND = 8             # hard cap on messages per cycle; above this -> one summary line
HTTP_TIMEOUT = 30

STATE_DIR = pathlib.Path(__file__).resolve().parent / "state"
TOKEN_FILE = STATE_DIR / "access_token.json"
LOC_FILE = STATE_DIR / "locations.json"
SEEN_FILE = STATE_DIR / "seen_reviews.json"
LOG_FILE = STATE_DIR / "cycles.log"

STARS = {"ONE": 1, "TWO": 2, "THREE": 3, "FOUR": 4, "FIVE": 5}
DOT = {1: "🔴", 2: "🔴", 3: "🟡", 4: "🟢", 5: "🟢"}


def log(msg):
    line = f"{dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')} {msg}"
    print(line, flush=True)
    STATE_DIR.mkdir(exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def load(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save(path, data):
    STATE_DIR.mkdir(exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------- oauth

def get_access_token():
    """Cached access token; refreshes only when <5 min of life left."""
    cached = load(TOKEN_FILE, {})
    if cached.get("token") and cached.get("expires_at", 0) - time.time() > 300:
        return cached["token"]

    r = requests.post(TOKEN_URL, data={
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "refresh_token": REFRESH_TOKEN,
        "grant_type": "refresh_token",
    }, timeout=HTTP_TIMEOUT)
    if r.status_code != 200:
        raise SystemExit(f"token refresh failed {r.status_code}: {r.text[:400]}")
    d = r.json()
    save(TOKEN_FILE, {"token": d["access_token"],
                      "expires_at": time.time() + d.get("expires_in", 3600)})
    return d["access_token"]


def bootstrap_refresh_token():
    """One-time interactive flow -> prints refresh_token."""
    import urllib.parse
    from http.server import BaseHTTPRequestHandler, HTTPServer

    if not CLIENT_ID or not CLIENT_SECRET:
        raise SystemExit("set GBP_CLIENT_ID and GBP_CLIENT_SECRET first")

    params = urllib.parse.urlencode({
        "client_id": CLIENT_ID, "redirect_uri": REDIRECT, "response_type": "code",
        "scope": SCOPE, "access_type": "offline", "prompt": "consent",
    })
    print("\nOpen this URL in a browser, approve, and come back:\n")
    print(f"{AUTH_URL}?{params}\n")

    box = {}

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.urlparse(self.path).query
            box.update(urllib.parse.parse_qs(q))
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write("Done. Return to the terminal.".encode())

        def log_message(self, *a):
            pass

    srv = HTTPServer(("localhost", 8080), H)
    srv.handle_request()

    if "code" not in box:
        raise SystemExit(f"no code received: {box}")

    r = requests.post(TOKEN_URL, data={
        "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
        "code": box["code"][0], "grant_type": "authorization_code",
        "redirect_uri": REDIRECT,
    }, timeout=HTTP_TIMEOUT)
    d = r.json()
    if "refresh_token" not in d:
        raise SystemExit(f"no refresh_token in response: {d}")
    print("\n=== PUT THIS IN YOUR ENV ===")
    print(f"GBP_REFRESH_TOKEN={d['refresh_token']}\n")


# ---------------------------------------------------------------- gbp api

def api_get(url, token, params=None):
    for attempt in range(5):
        r = requests.get(url, headers={"Authorization": f"Bearer {token}"},
                         params=params, timeout=HTTP_TIMEOUT)
        if r.status_code == 429 or r.status_code >= 500:
            wait = (2 ** attempt) + (attempt * 0.3)
            log(f"  {r.status_code} on GET, backoff {wait:.1f}s")
            time.sleep(wait)
            continue
        r.raise_for_status()
        return r.json()
    raise SystemExit(f"GET gave up: {url}")


def api_post(url, token, body):
    for attempt in range(5):
        r = requests.post(url, headers={"Authorization": f"Bearer {token}"},
                          json=body, timeout=HTTP_TIMEOUT)
        if r.status_code == 429 or r.status_code >= 500:
            wait = (2 ** attempt) + (attempt * 0.3)
            log(f"  {r.status_code} on POST, backoff {wait:.1f}s")
            time.sleep(wait)
            continue
        r.raise_for_status()
        return r.json()
    raise SystemExit(f"POST gave up: {url}")


def list_accounts():
    """Print all GBP accounts visible to this login, with location counts."""
    token = get_access_token()
    accts, page = [], None
    while True:
        params = {"pageSize": 20}
        if page:
            params["pageToken"] = page
        data = api_get(f"{API_ACCT}/accounts", token, params)
        accts.extend(data.get("accounts", []))
        page = data.get("nextPageToken")
        if not page:
            break
        time.sleep(REQ_DELAY)

    print(f"\nfound {len(accts)} account(s):\n")
    for a in accts:
        aid = a["name"].split("/")[-1]
        # count locations under this account
        try:
            d = api_get(f"{API_INFO}/accounts/{aid}/locations", token,
                        {"readMask": "name", "pageSize": 100})
            n = len(d.get("locations", []))
            more = "+" if d.get("nextPageToken") else ""
        except Exception as e:
            n, more = f"err ({str(e)[:40]})", ""
        print(f"  GBP_ACCOUNT_ID={aid}")
        print(f"    name       : {a.get('accountName', '?')}")
        print(f"    type       : {a.get('type', '?')}   role: {a.get('role', '?')}")
        print(f"    locations  : {n}{more}")
        print()
        time.sleep(REQ_DELAY)
    print("Take the id whose location count matches your real number.\n")


def refresh_locations():
    """Cache locationId -> {title, label, placeId, account}. Run weekly / after adding a location."""
    token = get_access_token()
    out = {}
    for aid in ACCOUNT_IDS:
        page, n = None, 0
        while True:
            params = {"readMask": "name,title,storefrontAddress,metadata", "pageSize": 100}
            if page:
                params["pageToken"] = page
            data = api_get(f"{API_INFO}/accounts/{aid}/locations", token, params)
            for loc in data.get("locations", []):
                lid = loc["name"].split("/")[-1]
                addr = loc.get("storefrontAddress", {}) or {}
                city = (addr.get("locality") or "").strip()
                region = (addr.get("administrativeArea") or "").strip()
                label = ", ".join([x for x in (city, region) if x]) or loc.get("title", lid)
                out[lid] = {
                    "title": loc.get("title", lid),
                    "label": label,
                    "placeId": (loc.get("metadata") or {}).get("placeId", ""),
                    "account": aid,
                }
                n += 1
            page = data.get("nextPageToken")
            if not page:
                break
            time.sleep(REQ_DELAY)
        log(f"  account {aid}: {n} locations")
    save(LOC_FILE, out)
    log(f"locations cached: {len(out)} across {len(ACCOUNT_IDS)} account(s)")
    return out


def fetch_reviews(token, locations):
    """batchGetReviews per account, chunks of 10. Returns (reviews, api_call_count)."""
    by_account = {}
    for lid, meta in locations.items():
        by_account.setdefault(meta.get("account", ""), []).append(lid)

    reviews, calls = [], 0
    for aid, lids in by_account.items():
        if not aid:
            continue
        for i in range(0, len(lids), BATCH_SIZE):
            chunk = lids[i:i + BATCH_SIZE]
            body = {
                "locationNames": [f"accounts/{aid}/locations/{lid}" for lid in chunk],
                "pageSize": PAGE_SIZE,
                "orderBy": "updateTime desc",
                "ignoreRatingOnlyReviews": False,
            }
            data = api_post(f"{API_V4}/accounts/{aid}/locations:batchGetReviews", token, body)
            calls += 1
            for block in data.get("locationReviews", []):
                rev = block.get("review", {})
                rev["_location"] = block.get("name", "").split("/")[-1]
                reviews.append(rev)
            time.sleep(REQ_DELAY)
    return reviews, calls


# ---------------------------------------------------------------- telegram

def tg_send(text):
    r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", json={
        "chat_id": TG_CHAT, "text": text, "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }, timeout=HTTP_TIMEOUT)
    if r.status_code != 200:
        log(f"  telegram error {r.status_code}: {r.text[:300]}")
        return False
    return True


def format_review(rev, locmeta, is_update):
    stars = STARS.get(rev.get("starRating", ""), 0)
    dot = DOT.get(stars, "⚪")
    label = (locmeta or {}).get("label", rev.get("_location", "?"))
    place = (locmeta or {}).get("placeId", "")

    head = f"{dot} <b>{'★' * stars if stars else '—'} · {html.escape(label.upper())}</b>"
    if is_update:
        head += "  <i>(відгук змінено)</i>"

    comment = (rev.get("comment") or "").strip()
    body = f"\n\n<i>без тексту, тільки оцінка</i>" if not comment \
        else "\n\n" + html.escape(comment[:900]) + ("…" if len(comment) > 900 else "")

    who = ((rev.get("reviewer") or {}).get("displayName") or "Анонім")
    when = (rev.get("updateTime") or rev.get("createTime") or "")[:16].replace("T", " ")
    meta = f"\n\n{html.escape(who)} · {when} UTC"

    if rev.get("reviewReply"):
        meta += "\n✅ відповідь є"
    else:
        meta += "\n⚠️ <b>без відповіді</b>"

    link = ""
    if place:
        link = f'\n<a href="https://search.google.com/local/reviews?placeid={place}">Відкрити відгуки локації</a>'

    return head + body + meta + link


# ---------------------------------------------------------------- main

def cycle(init=False, dry=False):
    missing = [k for k, v in {
        "GBP_CLIENT_ID": CLIENT_ID, "GBP_CLIENT_SECRET": CLIENT_SECRET,
        "GBP_REFRESH_TOKEN": REFRESH_TOKEN, "GBP_ACCOUNT_ID": ",".join(ACCOUNT_IDS),
    }.items() if not v]
    if missing:
        raise SystemExit(f"missing config: {', '.join(missing)}")

    locations = load(LOC_FILE, {})
    if not locations:
        locations = refresh_locations()

    token = get_access_token()
    reviews, calls = fetch_reviews(token, locations)

    seen = load(SEEN_FILE, {})          # review_name -> updateTime
    had_state = bool(seen)              # empty state = seed, never flood
    new, changed = [], []
    for rev in reviews:
        name = rev.get("name")
        if not name:
            continue
        ut = rev.get("updateTime", "")
        if name not in seen:
            new.append(rev)
        elif seen[name] != ut:
            changed.append(rev)
        seen[name] = ut

    # --- seeding path: explicit --init, OR state was lost / first ever run ---
    if init or not had_state:
        save(SEEN_FILE, seen)
        why = "init" if init else "STATE WAS EMPTY - seeded instead of flooding"
        log(f"{why}: {len(seen)} reviews recorded, nothing sent, {calls} api calls")
        if not dry and TG_TOKEN and TG_CHAT:
            tg_send(f"🤖 Базу відгуків синхронізовано.\n"
                    f"{len(locations)} локацій, {len(seen)} відгуків в базі.\n"
                    f"Далі приходитимуть тільки нові.")
        return

    # --- flood guard: never dump more than MAX_SEND at once ---
    queue = [(r, False) for r in new] + [(r, True) for r in changed]
    if len(queue) > MAX_SEND:
        save(SEEN_FILE, seen) if not dry else None
        log(f"cycle: {calls} calls, {len(queue)} pending > MAX_SEND={MAX_SEND}, sent summary only")
        if not dry:
            tg_send(f"⚠️ За цикл знайдено {len(new)} нових і {len(changed)} змінених відгуків "
                    f"— це більше за ліміт {MAX_SEND}, тому окремі повідомлення не шлю.\n"
                    f"Перевір локації вручну. Наступні відгуки прийдуть як звичайно.")
        return

    sent = 0
    for rev, is_upd in queue:
        msg = format_review(rev, locations.get(rev["_location"]), is_upd)
        if dry:
            print("\n---\n" + msg)
        elif tg_send(msg):
            sent += 1
            time.sleep(1.2)          # stay well under Telegram group rate limit

    if not dry:
        save(SEEN_FILE, seen)
    log(f"cycle: {calls} calls, {len(reviews)} fetched, "
        f"{len(new)} new, {len(changed)} changed, {sent} sent")


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--auth" in args:
        bootstrap_refresh_token()
    elif "--accounts" in args:
        list_accounts()
    elif "--locations" in args:
        refresh_locations()
    else:
        cycle(init="--init" in args, dry="--dry-run" in args)
