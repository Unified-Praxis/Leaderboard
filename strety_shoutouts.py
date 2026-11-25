#!/usr/bin/env python3
"""Pull people + shoutouts from the Strety API and write shoutouts.json for leaderboard.html.

Setup
  1. Strety > My Integrations > My Apps: create an app (scope: read), then run the OAuth flow once.
  2. export STRETY_TOKEN=<access token>
     (optional, for auto-refresh: STRETY_CLIENT_ID, STRETY_CLIENT_SECRET, STRETY_REFRESH_TOKEN)
  3. python strety_shoutouts.py --peek     # prints one raw person + shoutout so you can check field names
  4. python strety_shoutouts.py            # writes shoutouts.json

Optional photos.json (same folder) fills gaps for people with no Strety picture:
  {"<person id or exact name>": "https://.../pic.jpg" | "local/path.png" | "data:image/jpeg;base64,..."}
The leaderboard page's "Photo backup" button produces this file for you.

Not verified against Strety's docs: the shoutouts path, pagination params and shoutout field
names. Everything to tweak is in the three CONFIG / FIELD MAP spots below.
"""
import os, sys, json, time, base64, mimetypes, datetime as dt
import urllib.request, urllib.parse, urllib.error

# ---- CONFIG -------------------------------------------------------------
BASE = "https://2.strety.com/api/v1"
SHOUTOUTS_PATH = os.environ.get("STRETY_SHOUTOUTS_PATH", "/shoutouts")   # <- verify
FY_START_MONTH = int(os.environ.get("FISCAL_START_MONTH", "1"))          # 1 = calendar quarters
PAGE_SIZE = 20                                                           # API max per request
MAX_PHOTO_BYTES = 250_000
TOKEN = os.environ.get("STRETY_TOKEN", "")


# ---- HTTP (rate limit is 10 requests / 10 s) ----------------------------
def refresh_token():
    global TOKEN
    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "client_id": os.environ["STRETY_CLIENT_ID"],
        "client_secret": os.environ["STRETY_CLIENT_SECRET"],
        "refresh_token": os.environ["STRETY_REFRESH_TOKEN"],
    }).encode()
    req = urllib.request.Request(BASE + "/oauth/token", body,
                                 {"Accept": "application/json"})
    out = json.load(urllib.request.urlopen(req))
    TOKEN = out["access_token"]
    print("Refreshed token. New refresh token (save it):", out.get("refresh_token"), file=sys.stderr)


def get(url, retry=True):
    time.sleep(1.05)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {TOKEN}",
                                               "Accept": "application/json"})
    try:
        return json.load(urllib.request.urlopen(req))
    except urllib.error.HTTPError as e:
        if e.code == 401 and retry and os.environ.get("STRETY_REFRESH_TOKEN"):
            refresh_token()
            return get(url, retry=False)
        if e.code == 429 and retry:
            time.sleep(10)
            return get(url, retry=False)
        sys.exit(f"HTTP {e.code} for {url}\n{e.read()[:300]!r}")


def records(resp):
    return resp.get("data", []) if isinstance(resp, dict) else resp


def fetch_all(path):
    """Follows links.next when the API provides it, otherwise walks page numbers."""
    out, seen_first = [], None
    url = f"{BASE}{path}?{urllib.parse.urlencode({'page[number]': 1, 'page[size]': PAGE_SIZE})}"
    page = 1
    while url:
        resp = get(url)
        rows = records(resp)
        if not rows or (seen_first and rows[0].get("id") == seen_first):
            break  # empty, or the server ignored our page params
        seen_first = seen_first or rows[0].get("id")
        out += rows
        nxt = (resp.get("links") or {}).get("next") if isinstance(resp, dict) else None
        if nxt:
            url = nxt if nxt.startswith("http") else BASE.rsplit("/api", 1)[0] + nxt
        elif len(rows) >= PAGE_SIZE:
            page += 1
            url = f"{BASE}{path}?{urllib.parse.urlencode({'page[number]': page, 'page[size]': PAGE_SIZE})}"
        else:
            url = None
    return out


# ---- FIELD MAP: adjust these after running --peek -----------------------
def attrs(item):
    return {**item.get("attributes", {}), "id": item.get("id"), "_rel": item.get("relationships", {})}


def pick(d, *keys):
    for k in keys:
        if d.get(k) not in (None, ""):
            return d[k]


def rel_ids(d, *names):
    """IDs from either a flat field (sender_id / recipient_ids) or a relationship block."""
    for n in names:
        v = d.get(n) or d.get(n + "_id") or d.get(n + "_ids")
        if v is None and n in d["_rel"]:
            v = (d["_rel"][n] or {}).get("data")
        if v is None:
            continue
        v = v if isinstance(v, list) else [v]
        return [str(x["id"] if isinstance(x, dict) else x) for x in v]
    return []


def parse_person(item):
    d = attrs(item)
    name = pick(d, "name", "full_name") or f"{d.get('first_name', '')} {d.get('last_name', '')}".strip()
    return {"id": str(d["id"]), "name": name or str(d["id"]),
            "photo_url": pick(d, "avatar_url", "photo_url", "image_url", "profile_image_url", "avatar")}


def parse_shoutout(item):
    d = attrs(item)
    when = pick(d, "created_at", "inserted_at", "date")
    sender = rel_ids(d, "sender", "from", "author", "giver", "created_by", "user")
    return {"id": str(d["id"]), "date": when,
            "from": sender[0] if sender else None,
            "to": rel_ids(d, "recipients", "recipient", "receivers", "to", "people"),
            "value": pick(d, "core_value_name", "core_value", "value") or ""}


# ---- Build --------------------------------------------------------------
def quarter(iso):
    d = dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    q = ((d.month - FY_START_MONTH) % 12) // 3 + 1
    fy = d.year + (1 if FY_START_MONTH > 1 and d.month >= FY_START_MONTH else 0)
    return f"{fy}-Q{q}"


def to_data_uri(src):
    try:
        if src.startswith("data:"):
            return src
        if src.startswith("http"):
            r = urllib.request.urlopen(urllib.request.Request(src, headers={"User-Agent": "leaderboard"}))
            blob, mime = r.read(), r.headers.get_content_type()
        else:
            blob, mime = open(src, "rb").read(), mimetypes.guess_type(src)[0] or "image/jpeg"
        if len(blob) > MAX_PHOTO_BYTES:
            print(f"  skipped (over {MAX_PHOTO_BYTES // 1000} KB): {src[:60]}", file=sys.stderr)
            return None
        return f"data:{mime};base64,{base64.b64encode(blob).decode()}"
    except Exception as e:
        print(f"  could not load photo {src[:60]}: {e}", file=sys.stderr)


def main():
    if not TOKEN:
        sys.exit("Set STRETY_TOKEN first (see the top of this file).")
    if "--peek" in sys.argv:
        for label, path in (("PERSON", "/people"), ("SHOUTOUT", SHOUTOUTS_PATH)):
            rows = records(get(f"{BASE}{path}?page[size]=1"))
            print(label, json.dumps(rows[0] if rows else None, indent=2)[:1500])
        return

    people = [parse_person(p) for p in fetch_all("/people")]
    shouts = []
    for s in fetch_all(SHOUTOUTS_PATH):
        s = parse_shoutout(s)
        if s["date"]:
            s["quarter"] = quarter(s["date"])
            shouts.append(s)

    overrides = json.load(open("photos.json")) if os.path.exists("photos.json") else {}
    for p in people:
        src = overrides.get(p["id"]) or overrides.get(p["name"]) or p.pop("photo_url", None)
        p.pop("photo_url", None)
        p["photo"] = to_data_uri(src) if src else None

    json.dump({"generated": dt.datetime.now().isoformat(timespec="seconds"),
               "people": people, "shoutouts": shouts}, open("shoutouts.json", "w"))
    missing = [p["name"] for p in people if not p["photo"]]
    print(f"{len(people)} people, {len(shouts)} shoutouts -> shoutouts.json")
    if missing:
        print("No photo yet:", ", ".join(missing))


if __name__ == "__main__":
    main()
