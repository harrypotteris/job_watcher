#!/usr/bin/env python3
"""
Job Watcher v2 - watches job aggregators (and optional company ATS boards),
keeps only entry-level jobs in your roles that are in India OR mention visa
sponsorship, and sends NEW matches to Telegram.

Env vars: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
          ADZUNA_APP_ID, ADZUNA_APP_KEY   (free at developer.adzuna.com)
Run:      python job_watcher.py            (normal)
          python job_watcher.py --dry-run  (print only, don't send/save)
"""
import html as htmllib
import json
import os
import re
import sys
import time
from pathlib import Path

import requests

HERE = Path(__file__).parent
CONFIG_FILE = HERE / "config.json"
SEEN_FILE = HERE / "seen.json"
DRY = "--dry-run" in sys.argv
UA = {"User-Agent": "Mozilla/5.0 (personal job-watcher project)"}
TIMEOUT = 25
MAX_SEEN = 30000
MAX_ALERTS_PER_RUN = 40
DESC_LIMIT = 8000


def clean(t):
    t = htmllib.unescape(t or "")
    t = re.sub(r"<[^>]+>", " ", t)
    t = htmllib.unescape(t)
    return re.sub(r"\s+", " ", t).strip()


# ======================= fetchers =======================
# each returns list of dicts: id, title, company, location, url, desc,
# optional: country (2-letter), aggregator (bool)

def fetch_adzuna(src):
    app_id = os.environ.get("ADZUNA_APP_ID")
    app_key = os.environ.get("ADZUNA_APP_KEY")
    if not app_id or not app_key:
        raise RuntimeError("ADZUNA_APP_ID / ADZUNA_APP_KEY not set")
    out = []
    for country in src.get("countries", ["in"]):
        for q in src.get("queries", ["firmware engineer"]):
            r = requests.get(
                f"https://api.adzuna.com/v1/api/jobs/{country}/search/1",
                params={"app_id": app_id, "app_key": app_key,
                        "results_per_page": 50, "what": q,
                        "sort_by": "date",
                        "max_days_old": src.get("max_days_old", 3),
                        "content-type": "application/json"},
                headers=UA, timeout=TIMEOUT)
            r.raise_for_status()
            for j in r.json().get("results", []):
                out.append({
                    "id": str(j.get("id")),
                    "title": clean(j.get("title")),
                    "company": (j.get("company") or {}).get("display_name", ""),
                    "location": (j.get("location") or {}).get("display_name", ""),
                    "url": j.get("redirect_url", ""),
                    "desc": clean(j.get("description"))[:DESC_LIMIT],
                    "country": country,
                    "aggregator": True,
                })
    return out


def fetch_remotive(src):
    out = []
    for q in src.get("queries", [""]):
        params = {"category": src.get("category", "software-dev"),
                  "limit": src.get("limit", 100)}
        if q:
            params["search"] = q
        r = requests.get("https://remotive.com/api/remote-jobs",
                         params=params, headers=UA, timeout=TIMEOUT)
        r.raise_for_status()
        for j in r.json().get("jobs", []):
            out.append({
                "id": str(j.get("id")),
                "title": clean(j.get("title")),
                "company": j.get("company_name", ""),
                "location": j.get("candidate_required_location", "") or "Remote",
                "url": j.get("url", ""),
                "desc": clean(j.get("description"))[:DESC_LIMIT],
                "aggregator": True,
                "remote": True,
            })
    return out


def fetch_arbeitnow(src):
    out = []
    for page in range(1, src.get("pages", 3) + 1):
        r = requests.get("https://www.arbeitnow.com/api/job-board-api",
                         params={"page": page}, headers=UA, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json().get("data", [])
        if not data:
            break
        for j in data:
            out.append({
                "id": j.get("slug", ""),
                "title": clean(j.get("title")),
                "company": j.get("company_name", ""),
                "location": j.get("location", "") + (" (remote)" if j.get("remote") else ""),
                "url": j.get("url", ""),
                "desc": clean(j.get("description"))[:DESC_LIMIT],
                "aggregator": True,
            })
    return out


def fetch_greenhouse(src):
    r = requests.get(
        f"https://boards-api.greenhouse.io/v1/boards/{src['slug']}/jobs",
        params={"content": "true"}, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    return [{
        "id": str(j["id"]),
        "title": clean(j.get("title")),
        "company": src["name"],
        "location": (j.get("location") or {}).get("name", ""),
        "url": j.get("absolute_url", ""),
        "desc": clean(j.get("content"))[:DESC_LIMIT],
    } for j in r.json().get("jobs", [])]


def fetch_lever(src):
    r = requests.get(
        f"https://api.lever.co/v0/postings/{src['slug']}?mode=json",
        headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    return [{
        "id": j["id"],
        "title": clean(j.get("text")),
        "company": src["name"],
        "location": (j.get("categories") or {}).get("location", "") or "",
        "url": j.get("hostedUrl", ""),
        "desc": clean(j.get("descriptionPlain"))[:DESC_LIMIT],
    } for j in r.json()]


def fetch_workday(src):
    host, tenant, site = src["host"], src["tenant"], src["site"]
    api = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    out, ids = [], set()
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
                if not path or path in ids:
                    continue
                ids.add(path)
                out.append({
                    "id": path,
                    "title": clean(j.get("title")),
                    "company": src["name"],
                    "location": j.get("locationsText", ""),
                    "url": f"https://{host}/en-US/{site}{path}",
                    "desc": "",
                })
    return out


FETCHERS = {"adzuna": fetch_adzuna, "remotive": fetch_remotive,
            "arbeitnow": fetch_arbeitnow, "greenhouse": fetch_greenhouse,
            "lever": fetch_lever, "workday": fetch_workday}


# ======================= smart filter =======================
INDIA_WORDS = ["india", "bengaluru", "bangalore", "hyderabad", "pune",
               "mumbai", "chennai", "noida", "gurgaon", "gurugram", "delhi",
               "kolkata", "ahmedabad", "kochi", "coimbatore", "jaipur",
               "indore", "thiruvananthapuram", "trivandrum", "chandigarh",
               "mysuru", "mysore", "nagpur", "bhubaneswar", "vadodara"]
REMOTE_OK_WORDS = ["worldwide", "anywhere", "global", "apac", "asia"]

ENTRY_TITLE = re.compile(
    r"\b(interns?|internship|trainee|freshers?|new[\s-]?grad(?:uate)?s?|"
    r"graduate|entry[\s-]?level|junior|jr\.?|associate|campus|apprentice|"
    r"early career|(?:engineer|developer|scientist|analyst)\s*-?\s*(?:i|1)|"
    r"sde[\s-]?(?:1|i))\b", re.I)
ENTRY_DESC = re.compile(
    r"\b(freshers?|new[\s-]?grads?|entry[\s-]?level|recent graduates?|"
    r"campus (?:hire|hiring)|0\s*(?:-|–|to)\s*[123]\s*(?:\+\s*)?years?|"
    r"(?:graduat\w+|batch|class)\s+(?:of\s+|in\s+)?(?:20)?2[5-8])\b", re.I)
YEARS = re.compile(
    r"(\d{1,2})\s*(?:\+|\s*(?:-|–|to)\s*\d{1,2})?\s*(?:years?|yrs?)", re.I)
VISA = re.compile(
    r"visa sponsorship|sponsor(?:ship)?\s+(?:is\s+)?(?:available|provided|offered)|"
    r"(?:we|will|can)\s+(?:also\s+)?sponsor|visa support|"
    r"relocation (?:support|assistance|package|bonus)|blue card|"
    r"work permit (?:support|sponsorship)|immigration support", re.I)
NO_VISA = re.compile(
    r"no (?:visa )?sponsorship|(?:unable|not able|cannot|can't|won't|will not|"
    r"do not|does not|not)\s+(?:to\s+)?(?:provide\s+|offer\s+)?(?:visa\s+)?sponsor|"
    r"without (?:visa )?sponsorship|must (?:be|have).{0,40}"
    r"(?:authorized|authorised|eligible|right) to work|security clearance|"
    r"u\.?s\.? citizen", re.I)


def word_re(words):
    return re.compile(r"(?<![a-z0-9])(?:" +
                      "|".join(re.escape(w.lower()) for w in words) +
                      r")(?![a-z0-9])", re.I)


def min_years(text):
    ys = []
    for m in YEARS.finditer(text):
        ctx = text[max(0, m.start() - 60): m.end() + 60].lower()
        if "experience" in ctx or " exp" in ctx:
            ys.append(int(m.group(1)))
    return min(ys) if ys else None


def evaluate(job, cfg, src, role_re, excl_re):
    """Return list of tag strings if job should alert, else None."""
    title = job["title"]
    desc = job.get("desc", "")
    text = f"{title}. {desc}"
    loc = job.get("location", "").lower()

    if not role_re.search(title):
        return None
    if excl_re.search(title):
        return None

    # --- experience level ---
    max_y = cfg.get("max_years", 3)
    yrs = min_years(text)
    entry_t = bool(ENTRY_TITLE.search(title))
    entry_d = bool(ENTRY_DESC.search(desc))
    if yrs is not None and yrs > max_y and not entry_t:
        return None
    tags = []
    if entry_t or entry_d:
        tags.append("entry-level")
    if yrs is not None and yrs <= max_y:
        tags.append(f"{yrs}y exp")
    default_mode = "strict" if job.get("aggregator") else "loose"
    if src.get("level_mode", default_mode) == "strict" and not tags:
        return None

    # --- location / visa ---
    src_locs = [k.lower() for k in src.get("locations", [])]
    if src_locs:  # source-level override: you vouch for the location
        if not any(k in loc for k in src_locs):
            return None
        tags.append("location ok")
    else:
        india = job.get("country") == "in" or any(k in loc for k in INDIA_WORDS)
        remote_ok = job.get("remote") and (
            not loc or any(k in loc for k in REMOTE_OK_WORDS + INDIA_WORDS))
        if india:
            tags.append("India")
        elif remote_ok:
            tags.append("remote")
        else:
            if NO_VISA.search(text):
                return None
            if VISA.search(text):
                tags.append("visa/relocation ✓")
            elif cfg.get("require_visa_outside_india", True):
                return None
    return tags


# ======================= telegram =======================
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


# ======================= main =======================
def main():
    cfg = json.loads(CONFIG_FILE.read_text())
    state = json.loads(SEEN_FILE.read_text()) if SEEN_FILE.exists() else {}
    seen = dict.fromkeys(state.get("seen", []))
    seeded = set(state.get("seeded", []))
    fresh_start = not seeded
    last_run = state.get("last_run", {})
    now = time.time()
    role_re = word_re(cfg["roles"])
    excl_re = word_re(cfg.get("exclude_title", []))
    alerts, errors = [], []

    for src in cfg["sources"]:
        if not src.get("enabled", True):
            continue
        name = src["name"]
        gap = src.get("every_hours", 0) * 3600
        if gap and now - last_run.get(name, 0) < gap - 120:
            print(f"[{name}] skipped (every {src['every_hours']}h)")
            continue
        fetch = FETCHERS.get(src["type"])
        if not fetch:
            errors.append(f"{name}: unknown type {src['type']}")
            continue
        try:
            jobs = fetch(src)
        except Exception as e:
            errors.append(f"{name}: {e}")
            continue
        last_run[name] = now

        new = []
        for j in jobs:
            key = f"{name}:{j['id']}"
            if key in seen:
                continue
            seen[key] = None
            tags = evaluate(j, cfg, src, role_re, excl_re)
            if tags is not None:
                new.append((j, tags))

        first_run = name not in seeded
        seeded.add(name)
        print(f"[{name}] fetched={len(jobs)} new_matches={len(new)}"
              f"{' (first run, seeding silently)' if first_run else ''}")
        if first_run and not cfg.get("alert_on_first_run", False):
            continue
        for j, tags in new:
            alerts.append(
                f"🆕 {j['title']}\n🏢 {j.get('company') or name}\n"
                f"📍 {j.get('location') or 'n/a'}\n🏷 {' · '.join(tags)}\n"
                f"{j['url']}\n")

    if alerts:
        extra = len(alerts) - MAX_ALERTS_PER_RUN
        shown = alerts[:MAX_ALERTS_PER_RUN]
        head = f"{len(alerts)} new job match(es):\n"
        tail = [f"...and {extra} more"] if extra > 0 else []
        notify([head] + shown + tail)
    for e in errors:
        print("ERROR", e)
    if fresh_start:  # one-time ping so you know Telegram is wired up
        notify(["✅ Job watcher is live. Sources checked: "
                + ", ".join(sorted(seeded)) + ". New matches will show up here."])

    if not DRY:
        SEEN_FILE.write_text(json.dumps(
            {"seen": list(seen)[-MAX_SEEN:], "seeded": sorted(seeded),
             "last_run": last_run}, indent=1))


if __name__ == "__main__":
    main()
