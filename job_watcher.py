#!/usr/bin/env python3
"""
Job Watcher - polls company career APIs, filters by keywords/location,
and sends NEW matches to Telegram. State is kept in seen.json.

Env vars: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
Run:      python job_watcher.py            (normal)
          python job_watcher.py --dry-run  (print only, don't send/save)
"""
import html as htmllib
import json
import os
import re
import sys
from pathlib import Path

import requests

HERE = Path(__file__).parent
CONFIG_FILE = HERE / "config.json"
SEEN_FILE = HERE / "seen.json"
DRY = "--dry-run" in sys.argv
UA = {"User-Agent": "Mozilla/5.0 (job-watcher personal project)"}
TIMEOUT = 25


# ---------- fetchers: each returns list of {id,title,location,url} ----------
def fetch_greenhouse(src):
    r = requests.get(
        f"https://boards-api.greenhouse.io/v1/boards/{src['slug']}/jobs",
        headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    return [{
        "id": str(j["id"]),
        "title": j.get("title", ""),
        "location": (j.get("location") or {}).get("name", ""),
        "url": j.get("absolute_url", ""),
    } for j in r.json().get("jobs", [])]


def fetch_lever(src):
    r = requests.get(
        f"https://api.lever.co/v0/postings/{src['slug']}?mode=json",
        headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    return [{
        "id": j["id"],
        "title": j.get("text", ""),
        "location": (j.get("categories") or {}).get("location", "") or "",
        "url": j.get("hostedUrl", ""),
    } for j in r.json()]


def fetch_workday(src):
    """src: host (e.g. qualcomm.wd5.myworkdayjobs.com), tenant, site, search[]"""
    host, tenant, site = src["host"], src["tenant"], src["site"]
    api = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    out, seen_ids = [], set()
    for term in src.get("search", [""]):
        for page in range(src.get("pages", 3)):
            body = {"appliedFacets": {}, "limit": 20,
                    "offset": page * 20, "searchText": term}
            r = requests.post(api, json=body, headers=UA, timeout=TIMEOUT)
            r.raise_for_status()
            posts = r.json().get("jobPostings", [])
            if not posts:
                break
            for j in posts:
                path = j.get("externalPath", "")
                if not path or path in seen_ids:
                    continue
                seen_ids.add(path)
                out.append({
                    "id": path,
                    "title": j.get("title", ""),
                    "location": j.get("locationsText", ""),
                    "url": f"https://{host}/en-US/{site}{path}",
                })
    return out


def fetch_greenhouse_html(src):
    """For Greenhouse boards where the public API says 'not found'.
    Reads the public board page (job-boards.greenhouse.io/<slug>)."""
    base = f"https://job-boards.greenhouse.io/{src['slug']}"
    out, ids = [], set()
    for page in range(1, src.get("pages", 10) + 1):
        r = requests.get(base, params={"page": page}, headers=UA,
                         timeout=TIMEOUT)
        r.raise_for_status()
        found = 0
        for m in re.finditer(
                r'<a[^>]+href="([^"]*/jobs/(\d+)[^"]*)"[^>]*>(.*?)</a>',
                r.text, re.S):
            href, jid, inner = m.groups()
            if jid in ids:
                continue
            ids.add(jid)
            found += 1
            text = htmllib.unescape(re.sub(r"<[^>]+>", " ", inner))
            text = re.sub(r"\s+", " ", text).strip()
            url = href if href.startswith("http") \
                else "https://job-boards.greenhouse.io" + href
            # title text also contains the location; location filter is
            # skipped for these (empty location)
            out.append({"id": jid, "title": text, "location": "", "url": url})
        if not found:
            break
    return out


FETCHERS = {"greenhouse": fetch_greenhouse, "lever": fetch_lever,
            "workday": fetch_workday, "greenhouse_html": fetch_greenhouse_html}


# ---------- filtering ----------
def matches(job, src, cfg):
    title = job["title"].lower()
    loc = job["location"].lower()
    include = [k.lower() for k in src.get("include", cfg.get("include", []))]
    exclude = [k.lower() for k in src.get("exclude", cfg.get("exclude", []))]
    locs = [k.lower() for k in src.get("locations", cfg.get("locations", []))]
    if include and not any(k in title for k in include):
        return False
    if any(k in title for k in exclude):
        return False
    if locs and loc and not any(k in loc for k in locs):
        return False
    return True


# ---------- telegram ----------
def notify(lines):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if DRY or not token or not chat:
        print("\n".join(lines))
        if not DRY:
            print("!! Telegram env vars missing - nothing sent")
        return
    chunk = ""
    for line in lines + [None]:
        if line is None or len(chunk) + len(line) > 3800:
            if chunk:
                requests.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    data={"chat_id": chat, "text": chunk,
                          "disable_web_page_preview": "true"},
                    timeout=TIMEOUT).raise_for_status()
            chunk = ""
        if line:
            chunk += line + "\n"


# ---------- main ----------
def main():
    cfg = json.loads(CONFIG_FILE.read_text())
    state = json.loads(SEEN_FILE.read_text()) if SEEN_FILE.exists() else {}
    seen = set(state.get("seen", []))
    seeded = set(state.get("seeded", []))
    alerts, errors = [], []

    for src in cfg["sources"]:
        name = src["name"]
        fetch = FETCHERS.get(src["type"])
        if not fetch:
            errors.append(f"{name}: unknown type {src['type']}")
            continue
        try:
            jobs = fetch(src)
        except Exception as e:  # keep going if one site breaks
            errors.append(f"{name}: {e}")
            continue

        new = []
        for j in jobs:
            key = f"{name}:{j['id']}"
            if key in seen:
                continue
            seen.add(key)  # remember everything, matched or not
            if matches(j, src, cfg):
                new.append(j)

        first_run = name not in seeded
        seeded.add(name)
        print(f"[{name}] fetched={len(jobs)} new_matches={len(new)}"
              f"{' (first run, seeding silently)' if first_run else ''}")
        if first_run and not cfg.get("alert_on_first_run", False):
            continue
        for j in new:
            alerts.append(f"🆕 {name}: {j['title']}\n📍 {j['location'] or 'n/a'}\n{j['url']}\n")

    if alerts:
        notify([f"{len(alerts)} new job match(es):\n"] + alerts)
    for e in errors:
        print("ERROR", e)

    if not DRY:
        SEEN_FILE.write_text(json.dumps(
            {"seen": sorted(seen), "seeded": sorted(seeded)}, indent=1))


if __name__ == "__main__":
    main()
