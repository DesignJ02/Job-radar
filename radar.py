#!/usr/bin/env python3
"""
Job Radar
---------
Checks job sources every run, keeps only design roles that match your
filters, remembers what it has already sent, and alerts you on Telegram
(instantly) and email (one digest each morning).

Run modes:
  python radar.py            normal run (used by GitHub Actions)
  python radar.py --dry-run  print matches, send nothing, save nothing
  python radar.py --check    test every source in config.yaml and report which work
"""
import argparse
import html
import json
import os
import re
import smtplib
import sys
import time
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from pathlib import Path

import requests
import yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "state" / "seen.json"
PAGE_FILE = ROOT / "docs" / "index.html"
IST = timezone(timedelta(hours=5, minutes=30))
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}
TIMEOUT = 20


# ----------------------------------------------------------------- helpers
def log(msg):
    print(msg, flush=True)


def http_get(url, **kw):
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, **kw)
    r.raise_for_status()
    return r


def strip_html(text):
    if not text:
        return ""
    return BeautifulSoup(text, "html.parser").get_text(" ", strip=True)


def to_iso(value):
    """Accepts epoch ms, epoch s, or ISO strings. Returns ISO string or ''."""
    if value in (None, ""):
        return ""
    try:
        if isinstance(value, (int, float)):
            secs = value / 1000 if value > 1e11 else value
            return datetime.fromtimestamp(secs, tz=timezone.utc).isoformat()
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).isoformat()
    except Exception:
        return str(value)


def age_text(iso_str):
    if not iso_str:
        return ""
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        mins = int((datetime.now(timezone.utc) - dt).total_seconds() // 60)
        if mins < 0:
            return "just now"
        if mins < 60:
            return f"{mins}m ago"
        if mins < 48 * 60:
            return f"{mins // 60}h ago"
        return f"{mins // 1440}d ago"
    except Exception:
        return ""


def job(id, title, company, location, url, source, posted="", desc="", salary=""):
    return {
        "id": id,
        "title": (title or "").strip(),
        "company": (company or "").strip(),
        "location": (location or "").strip(),
        "url": url or "",
        "source": source,
        "posted": to_iso(posted),
        "desc": desc or "",
        "salary": salary or "",
    }


# ----------------------------------------------------------------- sources
def fetch_greenhouse(slug, name):
    data = http_get(
        f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", params={"content": "true"}
    ).json()
    for j in data.get("jobs", []):
        yield job(
            id=f"gh:{slug}:{j.get('id')}",
            title=j.get("title"),
            company=name,
            location=(j.get("location") or {}).get("name", ""),
            url=j.get("absolute_url"),
            source="Greenhouse",
            posted=j.get("first_published") or j.get("updated_at"),
            desc=strip_html(html.unescape(j.get("content") or "")),
        )


def fetch_lever(slug, name):
    data = http_get(f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json"}).json()
    for j in data if isinstance(data, list) else []:
        cats = j.get("categories") or {}
        locs = [cats.get("location") or ""] + list(cats.get("allLocations") or [])
        if (j.get("workplaceType") or "").lower() == "remote":
            locs.append("Remote")
        loc = ", ".join(dict.fromkeys(l for l in locs if l))
        parts = [j.get("descriptionPlain") or "", j.get("additionalPlain") or ""]
        for block in j.get("lists") or []:
            parts.append(strip_html(block.get("content") or ""))
        sal = j.get("salaryRange") or {}
        salary = ""
        if sal.get("min") or sal.get("max"):
            salary = f"{sal.get('currency', '')} {sal.get('min', '')}–{sal.get('max', '')} {sal.get('interval', '')}".strip()
        yield job(
            id=f"lv:{slug}:{j.get('id')}",
            title=j.get("text"),
            company=name,
            location=loc,
            url=j.get("hostedUrl"),
            source="Lever",
            posted=j.get("createdAt"),
            desc=" ".join(parts),
            salary=salary,
        )


def fetch_ashby(slug, name):
    data = http_get(
        f"https://api.ashbyhq.com/posting-api/job-board/{slug}",
        params={"includeCompensation": "true"},
    ).json()
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        locs = [j.get("location") or ""]
        locs += [(s or {}).get("location", "") for s in j.get("secondaryLocations") or []]
        country = (((j.get("address") or {}).get("postalAddress") or {}).get("addressCountry")) or ""
        if j.get("isRemote") or (j.get("workplaceType") or "").lower() == "remote":
            locs.append(f"Remote {country}".strip())
        comp = (j.get("compensation") or {}).get("compensationTierSummary") or ""
        yield job(
            id=f"ab:{slug}:{j.get('id')}",
            title=j.get("title"),
            company=name,
            location=", ".join(dict.fromkeys(l for l in locs if l)),
            url=j.get("jobUrl"),
            source="Ashby",
            posted=j.get("publishedAt"),
            desc=j.get("descriptionPlain") or strip_html(j.get("descriptionHtml") or ""),
            salary=comp,
        )


LINKEDIN_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"


def parse_linkedin(page_html):
    soup = BeautifulSoup(page_html, "html.parser")
    for card in soup.select("div.base-card, div.job-search-card"):
        urn = card.get("data-entity-urn", "")
        job_id = urn.rsplit(":", 1)[-1] if urn else ""
        link = card.select_one("a.base-card__full-link, a.base-search-card__full-link")
        url = (link.get("href") if link else "") or ""
        url = url.split("?")[0]
        if not job_id:
            m = re.search(r"-(\d{6,})/?$", url)
            job_id = m.group(1) if m else url
        title = card.select_one(".base-search-card__title")
        company = card.select_one(".base-search-card__subtitle")
        loc = card.select_one(".job-search-card__location")
        t = card.select_one("time")
        yield job(
            id=f"li:{job_id}",
            title=title.get_text(strip=True) if title else "",
            company=company.get_text(strip=True) if company else "",
            location=loc.get_text(strip=True) if loc else "",
            url=url,
            source="LinkedIn",
            posted=(t.get("datetime") if t else "") or "",
        )


def fetch_linkedin(cfg):
    li = cfg.get("linkedin") or {}
    if not li.get("enabled", True):
        return
    for search in li.get("searches", []):
        params = {
            "keywords": li.get("keywords"),
            "location": search["location"],
            "f_TPR": f"r{int(li.get('posted_within_seconds', 86400))}",
            "f_E": li.get("experience_levels", "3,4"),
            "sortBy": "DD",
            "start": 0,
        }
        if search.get("distance"):
            params["distance"] = search["distance"]
        if search.get("remote_only"):
            params["f_WT"] = "2"
        r = requests.get(LINKEDIN_URL, params=params, headers=HEADERS, timeout=TIMEOUT)
        if r.status_code == 429 or r.status_code >= 500:
            raise RuntimeError(f"LinkedIn blocked or busy (HTTP {r.status_code}); will retry next run")
        r.raise_for_status()
        yield from parse_linkedin(r.text)
        time.sleep(2)


FETCHERS = {"greenhouse": fetch_greenhouse, "lever": fetch_lever, "ashby": fetch_ashby}


# ----------------------------------------------------------------- filters
YEARS_RE = re.compile(
    r"(\d{1,2})\s*(?:\+\s*)?(?:(?:-|–|to)\s*(\d{1,2})\s*\+?\s*)?(?:years|yrs|year)", re.I
)


def compile_filters(cfg):
    f = cfg["filters"]
    return {
        "include": [re.compile(p, re.I) for p in f["title_include"]],
        "exclude": [re.compile(p, re.I) for p in f.get("title_exclude", [])],
        "locations": [s.lower() for s in f["locations"]],
        "remote_block": [s.lower() for s in f.get("remote_exclude_if_contains", [])],
        "min_years": f.get("experience", {}).get("min", 0),
        "max_years": f.get("experience", {}).get("max", 99),
    }


def title_ok(title, flt):
    return any(p.search(title) for p in flt["include"]) and not any(
        p.search(title) for p in flt["exclude"]
    )


def location_ok(loc, flt):
    l = loc.lower()
    if any(k in l for k in flt["locations"]):
        return True
    if "remote" in l or "anywhere" in l:
        # Allow "Remote", "Remote India", "Remote - APAC"; block "Remote - US" etc.
        padded = f" {re.sub(r'[^a-z]+', ' ', l)} "
        return not any(f" {b} " in padded for b in flt["remote_block"])
    return False


def experience_ok(desc, flt):
    """Drop roles that clearly ask for far more (or far less) experience.
    If the description says nothing about years, keep it."""
    if not desc:
        return True
    lows, highs = [], []
    for m in YEARS_RE.finditer(desc):
        lo = int(m.group(1))
        hi = int(m.group(2)) if m.group(2) else None
        if lo == 0 or lo > 20:
            continue
        lows.append(lo)
        if hi:
            highs.append(hi)
    if not lows:
        return True
    need = min(lows)
    if need > flt["max_years"]:
        return False
    if highs and max(highs) < flt["min_years"]:
        return False
    return True


def matches(j, flt, check_location=True):
    if not title_ok(j["title"], flt):
        return False
    if check_location and not location_ok(j["location"], flt):
        return False
    return experience_ok(j["desc"], flt)


# ----------------------------------------------------------------- state
def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return None


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    state["seen"] = {k: v for k, v in state["seen"].items() if v >= cutoff}
    state["recent"] = state["recent"][:300]
    STATE_FILE.write_text(json.dumps(state, indent=1, ensure_ascii=False))


# ----------------------------------------------------------------- alerts
def esc(s):
    return html.escape(s or "", quote=True)


def telegram_send(text):
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat:
        log("Telegram not set up (no TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID) - skipping.")
        return
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={
            "chat_id": chat,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=TIMEOUT,
    )
    if not r.ok:
        log(f"Telegram error {r.status_code}: {r.text[:200]}")


def telegram_jobs(jobs, page_url, cap):
    blocks = []
    for j in jobs[:cap]:
        meta = " · ".join(x for x in [j["source"], age_text(j["posted"]), j["salary"]] if x)
        blocks.append(
            f"<b>{esc(j['title'])}</b>\n{esc(j['company'])} · {esc(j['location'])}\n"
            f"{esc(meta)}\n<a href=\"{esc(j['url'])}\">Open job</a>"
        )
    if len(jobs) > cap:
        more = f"…and {len(jobs) - cap} more."
        if page_url:
            more += f' <a href="{esc(page_url)}">See all</a>'
        blocks.append(more)
    msg = ""
    for b in blocks:
        if len(msg) + len(b) > 3500:
            telegram_send(msg)
            msg = ""
        msg += b + "\n\n"
    if msg.strip():
        telegram_send(msg)


def email_send(subject, body_html):
    user, pw, to = os.getenv("SMTP_USER"), os.getenv("SMTP_PASS"), os.getenv("EMAIL_TO")
    if not (user and pw and to):
        log("Email not set up (no SMTP_USER / SMTP_PASS / EMAIL_TO) - skipping.")
        return False
    host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    port = int(os.getenv("SMTP_PORT", "587"))
    msg = MIMEText(body_html, "html", "utf-8")
    msg["Subject"], msg["From"], msg["To"] = subject, user, to
    try:
        with smtplib.SMTP(host, port, timeout=TIMEOUT) as s:
            s.starttls()
            s.login(user, pw)
            s.sendmail(user, [a.strip() for a in to.split(",")], msg.as_string())
        return True
    except Exception as e:
        log(f"Email error: {e}")
        return False


def email_digest_html(jobs, page_url):
    rows = "".join(
        f'<tr><td style="padding:8px 0;border-bottom:1px solid #eee">'
        f'<a href="{esc(j["url"])}" style="font-weight:600;color:#111">{esc(j["title"])}</a><br>'
        f'<span style="color:#555">{esc(j["company"])} · {esc(j["location"])} · {esc(j["source"])}'
        f'{" · " + esc(j["salary"]) if j["salary"] else ""}</span></td></tr>'
        for j in jobs
    )
    link = f'<p><a href="{esc(page_url)}">Open the full job list</a></p>' if page_url else ""
    return (
        f'<div style="font-family:Arial,sans-serif;max-width:640px">'
        f"<h2>{len(jobs)} new design jobs since yesterday</h2>{link}"
        f'<table style="width:100%;border-collapse:collapse">{rows}</table></div>'
    )


# ----------------------------------------------------------------- page
def render_page(recent, updated):
    cards = []
    for j in recent:
        sal = f'<span class="tag">{esc(j["salary"])}</span>' if j.get("salary") else ""
        cards.append(
            f'<a class="card" href="{esc(j["url"])}" target="_blank" rel="noopener" '
            f'data-text="{esc((j["title"] + " " + j["company"] + " " + j["location"]).lower())}">'
            f'<div class="t">{esc(j["title"])}</div>'
            f'<div class="m">{esc(j["company"])} · {esc(j["location"])}</div>'
            f'<div class="m"><span class="tag">{esc(j["source"])}</span>'
            f'<span class="age" data-ts="{esc(j.get("first_seen", ""))}"></span>{sal}</div></a>'
        )
    body = "\n".join(cards) or '<p class="empty">No matching jobs yet. Check back after the next run.</p>'
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Job Radar</title>
<style>
:root{{--bg:#f7f7f5;--card:#fff;--ink:#16161a;--sub:#5d5d66;--line:#e4e4e0;--accent:#2f5bea}}
@media (prefers-color-scheme:dark){{:root{{--bg:#111113;--card:#1b1b1f;--ink:#f1f1f3;--sub:#a0a0aa;--line:#2b2b31;--accent:#7b9bff}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 -apple-system,Segoe UI,Roboto,sans-serif}}
main{{max-width:760px;margin:0 auto;padding:28px 16px 60px}}
h1{{font-size:22px;margin:0 0 4px}}.sub{{color:var(--sub);margin:0 0 18px;font-size:13px}}
input{{width:100%;padding:11px 12px;border:1px solid var(--line);border-radius:10px;background:var(--card);color:var(--ink);font-size:15px;margin-bottom:14px}}
.card{{display:block;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px;margin-bottom:10px;text-decoration:none;color:inherit}}
.card:hover{{border-color:var(--accent)}}.t{{font-weight:600}}.m{{color:var(--sub);font-size:13px;margin-top:3px}}
.tag{{display:inline-block;border:1px solid var(--line);border-radius:6px;padding:0 6px;margin-right:8px;font-size:12px}}
.age{{margin-right:8px}}.empty{{color:var(--sub)}}
</style></head><body><main>
<h1>Job Radar</h1>
<p class="sub">Product / UX / UI-UX roles · Delhi NCR, Bangalore, Hyderabad, Remote · updated <span id="upd" data-ts="{esc(updated)}"></span></p>
<input id="q" placeholder="Filter by company, title or city" autocomplete="off">
<div id="list">{body}</div>
</main>
<script>
function ago(ts){{if(!ts)return"";var m=Math.round((Date.now()-new Date(ts))/60000);
if(m<60)return m+"m ago";if(m<2880)return Math.round(m/60)+"h ago";return Math.round(m/1440)+"d ago"}}
document.querySelectorAll("[data-ts]").forEach(function(e){{e.textContent=ago(e.dataset.ts)}});
document.getElementById("q").addEventListener("input",function(e){{var q=e.target.value.toLowerCase();
document.querySelectorAll(".card").forEach(function(c){{c.style.display=c.dataset.text.indexOf(q)>-1?"":"none"}})}});
</script></body></html>"""


# ----------------------------------------------------------------- main
def collect(cfg, flt):
    found, errors = {}, []
    for c in cfg.get("companies", []):
        fetch = FETCHERS.get(c.get("ats", "").lower())
        if not fetch:
            continue
        try:
            for j in fetch(c["slug"], c.get("name", c["slug"])):
                if matches(j, flt):
                    found[j["id"]] = j
        except Exception as e:
            errors.append(f"{c.get('name', c['slug'])} ({c['ats']}): {str(e)[:120]}")
    try:
        for j in fetch_linkedin(cfg):
            # LinkedIn search is already scoped to your locations.
            if matches(j, flt, check_location=False):
                found.setdefault(j["id"], j)
    except Exception as e:
        errors.append(f"LinkedIn: {str(e)[:160]}")
    return list(found.values()), errors


def check_sources(cfg):
    flt = compile_filters(cfg)
    lines = ["| Source | Status | Jobs listed | Matching now |", "|---|---|---|---|"]
    for c in cfg.get("companies", []):
        name = c.get("name", c["slug"])
        fetch = FETCHERS.get(c.get("ats", "").lower())
        if not fetch:
            lines.append(f"| {name} | unknown ats '{c.get('ats')}' | - | - |")
            continue
        try:
            jobs = list(fetch(c["slug"], name))
            hits = sum(matches(j, flt) for j in jobs)
            lines.append(f"| {name} ({c['ats']}/{c['slug']}) | OK | {len(jobs)} | {hits} |")
        except Exception as e:
            lines.append(f"| {name} ({c['ats']}/{c['slug']}) | FAILED: {str(e)[:70]} | - | - |")
    try:
        jobs = list(fetch_linkedin(cfg))
        hits = sum(matches(j, flt, check_location=False) for j in jobs)
        lines.append(f"| LinkedIn search | OK | {len(jobs)} | {hits} |")
    except Exception as e:
        lines.append(f"| LinkedIn search | FAILED: {str(e)[:70]} | - | - |")
    report = "\n".join(lines)
    log(report)
    summary = os.getenv("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write("## Source check\n\nRemove any FAILED company from config.yaml (or fix its slug).\n\n" + report + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    if args.check:
        check_sources(cfg)
        return

    flt = compile_filters(cfg)
    page_url = os.getenv("PAGE_URL") or cfg.get("page_url") or ""
    now = datetime.now(timezone.utc)
    state = load_state()
    first_run = state is None
    state = state or {"seen": {}, "recent": [], "email_pending": [], "last_email_date": ""}

    jobs, errors = collect(cfg, flt)
    for e in errors:
        log(f"warning: {e}")

    new = [j for j in jobs if j["id"] not in state["seen"]]
    new.sort(key=lambda j: j["posted"] or "", reverse=True)
    log(f"{len(jobs)} matching jobs found, {len(new)} new.")

    if args.dry_run:
        for j in new:
            log(f"- {j['title']} | {j['company']} | {j['location']} | {j['source']} | {j['url']}")
        return

    for j in new:
        state["seen"][j["id"]] = now.isoformat()
        slim = {k: j[k] for k in ("id", "title", "company", "location", "url", "source", "posted", "salary")}
        slim["first_seen"] = now.isoformat()
        state["recent"].insert(0, slim)

    if first_run:
        # Don't flood you with every open job on day one.
        telegram_send(
            f"✅ Job Radar is live. Tracking {len(jobs)} open roles that match your filters. "
            f"From now on you'll only hear about new ones."
            + (f'\n<a href="{esc(page_url)}">See current list</a>' if page_url else "")
        )
    elif new:
        telegram_jobs(new, page_url, cap=int(cfg.get("telegram_max_per_run", 15)))
        state["email_pending"].extend(state["recent"][: len(new)])

    # Morning email digest (first run on/after the set hour, IST, once a day).
    ist_now = now.astimezone(IST)
    today = ist_now.date().isoformat()
    if (
        ist_now.hour >= int(cfg.get("email_digest_hour_ist", 9))
        and state.get("last_email_date") != today
        and state["email_pending"]
    ):
        pending = state["email_pending"]
        if email_send(f"Job Radar: {len(pending)} new design jobs", email_digest_html(pending, page_url)):
            state["email_pending"], state["last_email_date"] = [], today

    save_state(state)
    PAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
    PAGE_FILE.write_text(render_page(state["recent"], now.isoformat()))


if __name__ == "__main__":
    sys.exit(main())
