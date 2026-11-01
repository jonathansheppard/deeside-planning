#!/opt/homebrew/bin/python3.11
"""Flintshire planning applications: data for the Deeside.com planning search page.

Pulls applications registered OR decided in the last 90 days from the Agile Applications API,
fetches each application's detail (full proposal, postcode, community council, easting/northing),
converts British National Grid to lat/long, and writes data/planning.json.

  build_planning.py            normal run
  build_planning.py --refresh  re-fetch every detail record (ignore cache)

Cache: data/detail_cache.json, keyed by application id. A detail record is re-fetched
only when the application's status in the search results has changed.
"""
import json, sys, time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
from pyproj import Transformer

DIR = Path(__file__).resolve().parent
OUT = DIR / "data" / "planning.json"
CACHE = DIR / "data" / "detail_cache.json"
API = "https://planningapi.agileapplications.co.uk/api/application"
HDR = {"x-client": "FLINTSHIRE", "x-product": "CITIZENPORTAL", "x-service": "PA", "accept": "application/json"}
PORTAL = "https://planning.agileapplications.co.uk/flintshire/application-details/"
DAYS = 90
MAX_FETCH = 120   # detail calls per run; the API rate-limits at around 200 in a burst
DELAY = 0.5       # seconds between detail calls
BNG = Transformer.from_crs("EPSG:27700", "EPSG:4326", always_xy=True)

# Constraint checks against DataMapWales (same layers as ~/planning-constraints). Cached per application.
sys.path.insert(0, str(Path.home() / "datamapwales-mcp"))
import datamapwales_mcp as dmw
CCACHE = DIR / "data" / "constraints_cache.json"
MAX_CHECK = 60    # sites checked per run (about 6 seconds each); --fill ignores this
SHORT = {
    "Flood zone: rivers": "Flood zone (rivers)",
    "Flood zone: sea": "Flood zone (sea)",
    "Flood zone: surface water and small watercourses": "Surface water flood risk",
    "Conservation area": "Conservation area",
    "Listed buildings within radius": "Listed building within 100m",
    "Scheduled monuments within radius": "Scheduled monument within 100m",
    "SSSI": "SSSI",
    "AONB / National Landscape": "National Landscape",
    "Ancient woodland within radius": "Ancient woodland within 50m",
    "Registered common land": "Common land",
    "Air Quality Management Area": "Air quality management area",
}
NAME_KEYS = ("name", "sitename", "sssi_name", "aonb_name", "site_name", "conservation_area", "ca_name",
             "aqma_name", "aqmaname", "prefname", "pref_name", "recordname", "record_name", "wood_name", "title")


def check_site(e, n):
    """Return list of {"c": short label, "n": [names]} or None if any layer failed."""
    hits = []
    for typename, label, rad in dmw.PRESETS["all"]:
        try:
            total, rows = dmw.query_layer(typename, None, None, e, n, rad)
        except Exception as exc:
            print(f"  constraint layer failed ({label}): {str(exc)[:80]}")
            return None
        if total:
            names = []
            for p in rows[:3]:
                low = {k.lower(): v for k, v in p.items()}
                nm = next((str(low[k]).strip() for k in NAME_KEYS if low.get(k)), "")
                if nm and nm not in names:
                    names.append(nm)
            hits.append({"c": SHORT.get(label, label), "n": names})
    return hits


def search(field, f, t):
    r = httpx.get(f"{API}/search", headers=HDR, timeout=60,
                  params={f"{field}From": f"{f}T00:00:00", f"{field}To": f"{t}T23:59:59"})
    r.raise_for_status()
    return r.json().get("results", [])


def d(s):
    return s[:10] if s else None


def outcome(text):
    t = (text or "").strip()
    if not t:
        return "pending"
    if t[:1].upper() == "A":
        return "approved"
    if t[:1].upper() == "R":
        return "refused"
    if t.startswith("Z01"):
        return "withdrawn"
    return "other"


def tidy_decision(text):
    t = (text or "").strip()
    return t.split(" ", 1)[1] if len(t) > 4 and t[3] == " " else t


def strip_prefix(s, prefix):
    s = (s or "").strip()
    return s[len(prefix):].strip() if s.startswith(prefix) else s


def main():
    refresh = "--refresh" in sys.argv
    t = date.today()
    f = t - timedelta(days=DAYS)
    rows = {}
    for field in ("registrationDate", "decisionDate"):
        for a in search(field, f, t):
            rows[a["id"]] = a
    cache = {} if refresh or not CACHE.exists() else json.loads(CACHE.read_text())

    out, fetched, failed, skipped = [], 0, 0, 0
    blocked = False
    with httpx.Client(headers=HDR, timeout=60) as client:
        for app_id, a in rows.items():
            key = str(app_id)
            c = cache.get(key)
            if c is None or c.get("_status") != a.get("status"):
                if blocked or fetched >= MAX_FETCH:
                    skipped += 1          # picked up on the next run
                    c = c or {}
                else:
                    new = r = None
                    for attempt in range(3):
                        try:
                            r = client.get(f"{API}/{app_id}")
                            if r.status_code == 429:
                                wait = int(r.headers.get("retry-after") or 0) or 20 * (attempt + 1)
                                time.sleep(min(wait, 60))
                                continue
                            r.raise_for_status()
                            new = r.json()
                            break
                        except Exception as exc:
                            print(f"  detail failed {a.get('reference')}: {str(exc)[:80]}")
                            break
                    if new is None:
                        failed += 1
                        if r is not None and r.status_code == 429:
                            blocked = True
                            print("  rate limited: stopping detail fetches for this run")
                        c = c or {}
                    else:
                        new["_status"] = a.get("status")
                        cache[key] = c = new
                        fetched += 1
                    time.sleep(DELAY)
            e, n = c.get("easting"), c.get("northing")
            if not (e and n):
                try:
                    e, n = [float(x) for x in (a.get("gridReference") or "").split(",")]
                except ValueError:
                    e = n = None
            lat = lng = None
            if e and n:
                lng, lat = BNG.transform(float(e), float(n))
                lat, lng = round(lat, 6), round(lng, 6)
            reg, dec = d(a.get("registrationDate")), d(a.get("decisionDate"))
            days = (date.fromisoformat(dec) - date.fromisoformat(reg)).days if reg and dec else None
            out.append({
                "id": app_id,
                "ref": a.get("reference"),
                "type": a.get("applicationType"),
                "proposal": (c.get("fullProposal") or a.get("proposal") or "").strip(),
                "location": (a.get("location") or "").strip(),
                "postcode": (c.get("postcode") or "").strip().upper(),
                "ward": strip_prefix(a.get("ward"), "Flintshire Ward"),
                "community": strip_prefix(c.get("parish"), "Flintshire Community"),
                "applicant": (a.get("applicantSurname") or "").strip(),
                "agent": (a.get("agentName") or "").strip(),
                "registered": reg,
                "decided": dec,
                "decision": tidy_decision(a.get("decisionText")),
                "outcome": outcome(a.get("decisionText")),
                "status": (a.get("status") or "").strip(),
                "days": days,
                "lat": lat,
                "lng": lng,
                "url": PORTAL + str(app_id),
            })

    out.sort(key=lambda x: max(x["registered"] or "", x["decided"] or ""), reverse=True)
    CACHE.write_text(json.dumps(cache))

    # constraints: newest applications first, cached by application id
    ccache = json.loads(CCACHE.read_text()) if CCACHE.exists() else {}
    checked, budget = 0, (10**6 if "--fill" in sys.argv else MAX_CHECK)
    for x in out:
        key = str(x["id"])
        if key not in ccache and x["lat"] is not None and checked < budget:
            c = cache.get(key) or {}
            e, n = c.get("easting"), c.get("northing")
            hits = check_site(float(e), float(n)) if e and n else None
            if hits is not None:
                ccache[key] = hits
                checked += 1
                if checked % 20 == 0:
                    CCACHE.write_text(json.dumps(ccache))
                    print(f"  constraints checked: {checked}")
        x["constraints"] = ccache.get(key)   # None = not checked yet, [] = nothing found
    CCACHE.write_text(json.dumps(ccache))
    unchecked = sum(1 for x in out if x["constraints"] is None and x["lat"] is not None)
    payload = {
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "window_days": DAYS,
        "from": f.isoformat(),
        "to": t.isoformat(),
        "count": len(out),
        "applications": out,
    }
    tmp = OUT.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    tmp.replace(OUT)
    print(f"{len(out)} applications, {fetched} details fetched, {failed} failed, {skipped} left for next run, {checked} sites constraint-checked ({unchecked} still to check), "
          f"{sum(1 for x in out if x['lat'] is None)} without location")


if __name__ == "__main__":
    main()
