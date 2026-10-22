#!/usr/bin/env python3
"""
FILE: scraper_engine.py
Automated B2B Lead Aggregator (Reddit, RemoteOK, Hacker News, Python.org),
Webhook Sync, Email Dispatcher, Static Site & RSS Builder.
"""

import os
import re
import json
import html
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

# Environment Variables (Configured in GitHub Secrets)
GAS_WEBHOOK_URL = os.environ.get("GAS_WEBHOOK_URL", "")
GAS_SECRET_TOKEN = os.environ.get("GAS_SECRET_TOKEN", "")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
SENDER_EMAIL = os.environ.get("SENDER_EMAIL", "onboarding@resend.dev")
GUMROAD_CHECKOUT_URL = os.environ.get("GUMROAD_CHECKOUT_URL", "https://gumroad.com")

KEYWORD_WEIGHTS = {
    "python": 3,
    "scrape": 4,
    "scraping": 4,
    "automation": 4,
    "automate": 4,
    "google apps script": 5,
    "vba": 5,
    "excel": 3,
    "spreadsheet": 4,
    "playwright": 5,
    "selenium": 4,
    "api": 3,
    "webhook": 4,
    "etl": 4,
    "pipeline": 3,
    "script": 3,
    "bot": 3,
}

USER_AGENT = "Mozilla/5.0 (compatible; AutoScopeLeadBot/2.0; +https://github.com)"


def fetch_url(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read()


def fetch_json(url: str):
    return json.loads(fetch_url(url).decode("utf-8"))


def strip_html(raw_html: str) -> str:
    clean = re.sub(r"<[^>]+>", " ", html.unescape(raw_html or ""))
    return re.sub(r"\s+", " ", clean).strip()


def score_and_enrich(title: str, description: str, url: str, source: str):
    combined = f"{title} {description}".lower()
    matched_keywords = []
    score = 0

    for kw, weight in KEYWORD_WEIGHTS.items():
        if kw in combined:
            matched_keywords.append(kw)
            score += weight

    if score < 3:
        return None

    kw_str = ", ".join(matched_keywords[:4])
    clean_title = title.strip()[:80]
    pitch = (
        f"Hi — saw your listing regarding '{clean_title}'. I specialize in {kw_str} "
        f"workflows and can deploy a clean, documented solution within 24-48 hours. "
        f"Happy to share a quick architecture outline if this is still open."
    )

    return {
        "source": source,
        "title": clean_title,
        "keywords": kw_str,
        "score": score,
        "url": url,
        "pitch": pitch,
    }


def scrape_reddit_forhire():
    leads = []
    url = "https://www.reddit.com/r/forhire/new.json?limit=50"
    try:
        data = fetch_json(url)
        posts = data.get("data", {}).get("children", [])
        for post in posts:
            p = post.get("data", {})
            title = p.get("title", "")
            if "[hiring]" not in title.lower():
                continue
            selftext = p.get("selftext", "")
            permalink = f"https://www.reddit.com{p.get('permalink', '')}"
            enriched = score_and_enrich(title, selftext, permalink, "Reddit r/forhire")
            if enriched:
                leads.append(enriched)
    except Exception as e:
        print(f"[WARN] Reddit scrape error: {e}")
    return leads


def scrape_remoteok():
    leads = []
    url = "https://remoteok.com/api"
    try:
        data = fetch_json(url)
        for item in data[1:50]:
            title = item.get("position", "")
            desc = strip_html(item.get("description", ""))
            tags = " ".join(item.get("tags", []))
            link = item.get("url", "")
            enriched = score_and_enrich(title, f"{desc} {tags}", link, "RemoteOK")
            if enriched:
                leads.append(enriched)
    except Exception as e:
        print(f"[WARN] RemoteOK scrape error: {e}")
    return leads


def scrape_hackernews():
    leads = []
    query = urllib.parse.quote("python automation OR scraping OR contract OR freelance")
    url = f"https://hn.algolia.com/api/v1/search_by_date?tags=comment&query={query}&hitsPerPage=50"
    try:
        data = fetch_json(url)
        for hit in data.get("hits", []):
            story_title = (hit.get("story_title") or "").lower()
            if "who is hiring" not in story_title and "hiring" not in story_title:
                continue
            raw_comment = hit.get("comment_text") or ""
            clean_comment = strip_html(raw_comment)
            if not clean_comment:
                continue
            first_line = clean_comment.split(".")[0][:75]
            title = f"HN Hiring: {first_line}"
            link = f"https://news.ycombinator.com/item?id={hit.get('objectID')}"
            enriched = score_and_enrich(title, clean_comment, link, "Hacker News")
            if enriched:
                leads.append(enriched)
    except Exception as e:
        print(f"[WARN] Hacker News scrape error: {e}")
    return leads


def scrape_python_org_jobs():
    leads = []
    url = "https://www.python.org/jobs/feed/rss/"
    try:
        xml_bytes = fetch_url(url)
        root = ET.fromstring(xml_bytes)
        for item in root.findall(".//item")[:40]:
            title = item.findtext("title") or ""
            desc = strip_html(item.findtext("description") or "")
            link = item.findtext("link") or ""
            enriched = score_and_enrich(title, desc, link, "Python.org Jobs")
            if enriched:
                leads.append(enriched)
    except Exception as e:
        print(f"[WARN] Python.org RSS scrape error: {e}")
    return leads


def sync_with_google_sheets(leads: list) -> list:
    if not GAS_WEBHOOK_URL or not GAS_SECRET_TOKEN:
        print("[INFO] GAS Webhook not configured. Skipping cloud sync.")
        return []

    payload = json.dumps({
        "token": GAS_SECRET_TOKEN,
        "action": "ingest_leads",
        "leads": leads,
    }).encode("utf-8")

    req = urllib.request.Request(
        GAS_WEBHOOK_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            print(f"[INFO] Sheets Sync: {result.get('inserted', 0)} new leads inserted.")
            return result.get("subscribers", [])
    except Exception as e:
        print(f"[ERROR] Failed to sync with Google Sheets: {e}")
        return []


def send_daily_digest(subscribers: list, leads: list):
    if not RESEND_API_KEY or not subscribers or not leads:
        return

    rows_html = "".join(
        f"<tr>"
        f"<td style='padding:8px;border:1px solid #ddd;'><strong>{html.escape(l['title'])}</strong><br><small>{html.escape(l['source'])} | Match: {html.escape(l['keywords'])}</small></td>"
        f"<td style='padding:8px;border:1px solid #ddd;'>{html.escape(l['pitch'])}</td>"
        f"<td style='padding:8px;border:1px solid #ddd;'><a href='{html.escape(l['url'])}'>Direct Link</a></td>"
        f"</tr>"
        for l in leads[:15]
    )

    email_html = f"""
    <div style="font-family:sans-serif;max-width:680px;margin:auto;">
      <h2>AutoScope Daily High-Intent Contract Feed</h2>
      <p>Top scored technical automation & scripting leads discovered today:</p>
      <table style="border-collapse:collapse;width:100%;font-size:14px;">
        <thead>
          <tr style="background:#111;color:#fff;">
            <th style="padding:8px;text-align:left;">Opportunity</th>
            <th style="padding:8px;text-align:left;">Ready-to-Send Pitch</th>
            <th style="padding:8px;text-align:left;">Apply</th>
          </tr>
        </thead>
        <tbody>{rows_html}</tbody>
      </table>
    </div>
    """

    for sub_email in subscribers[:95]:
        payload = json.dumps({
            "from": SENDER_EMAIL,
            "to": [sub_email],
            "subject": f"[AutoScope] {len(leads)} High-Intent Automation Leads ({datetime.now(timezone.utc).strftime('%b %d')})",
            "html": email_html,
        }).encode("utf-8")

        req = urllib.request.Request(
            "https://api.resend.com/emails",
            data=payload,
            headers={
                "Authorization": f"Bearer {RESEND_API_KEY}",
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
            },
            method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=15)
        except Exception as e:
            print(f"[WARN] Resend error for {sub_email}: {e}")


def build_public_teaser_site(leads: list):
    out_dir = Path("public")
    out_dir.mkdir(exist_ok=True)
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    teaser_rows = ""
    for l in leads[:10]:
        teaser_rows += f"""
        <div class="card">
          <div class="badge">Score: {l['score']} | {html.escape(l['source'])}</div>
          <h3>{html.escape(l['title'])}</h3>
          <p class="tags">Matched: <code>{html.escape(l['keywords'])}</code></p>
          <a class="locked-btn" href="{html.escape(GUMROAD_CHECKOUT_URL)}">Unlock Direct Client Link & Pitch Template →</a>
        </div>
        """

    page_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>AutoScope | Daily High-Intent Automation & Scripting Contracts</title>
  <style>
    body {{ font-family: system-ui, sans-serif; background: #0d1117; color: #e6edf3; max-width: 760px; margin: 0 auto; padding: 2rem 1rem; }}
    header {{ border-bottom: 1px solid #30363d; padding-bottom: 1.5rem; margin-bottom: 2rem; }}
    .cta-box {{ background: #161b22; border: 1px solid #238636; padding: 1.25rem; border-radius: 8px; margin-bottom: 2rem; }}
    .cta-btn {{ display: inline-block; background: #238636; color: #fff; padding: 0.75rem 1.25rem; border-radius: 6px; text-decoration: none; font-weight: 600; margin-top: 0.5rem; }}
    .card {{ background: #161b22; border: 1px solid #30363d; padding: 1rem; border-radius: 6px; margin-bottom: 1rem; }}
    .badge {{ font-size: 0.75rem; color: #58a6ff; text-transform: uppercase; font-weight: 700; }}
    .locked-btn {{ color: #f0883e; text-decoration: none; font-size: 0.9rem; font-weight: 600; }}
    code {{ background: #21262d; padding: 2px 6px; border-radius: 4px; }}
  </style>
</head>
<body>
  <header>
    <h1>AutoScope Contract Feed</h1>
    <p>Automated daily discovery of high-intent Python, Scraping, VBA, and Apps Script contracts across Reddit, Hacker News, RemoteOK, and Python.org. Last updated: {today_str}</p>
  </header>
  <div class="cta-box">
    <h2>Get Unredacted Instant Access + Daily 7 AM Digest</h2>
    <p>Unlock the live Google Sheet database, direct client URLs, and pre-written outreach hooks for every lead discovered today.</p>
    <a class="cta-btn" href="{html.escape(GUMROAD_CHECKOUT_URL)}">Subscribe on Gumroad ($29/mo)</a>
  </div>
  <h2>Today's Top Filtered Opportunities ({len(leads)} Total Found)</h2>
  {teaser_rows}
</body>
</html>"""

    (out_dir / "index.html").write_text(page_html, encoding="utf-8")
    print("[INFO] Built public/index.html teaser page.")


def build_rss_feed(leads: list):
    out_dir = Path("public")
    out_dir.mkdir(exist_ok=True)
    pub_date = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")

    items_xml = ""
    for l in leads[:5]:
        items_xml += f"""
        <item>
          <title>{html.escape(l['title'])} [{html.escape(l['keywords'])}]</title>
          <link>{html.escape(GUMROAD_CHECKOUT_URL)}</link>
          <description>Source: {html.escape(l['source'])} | Matched Keywords: {html.escape(l['keywords'])} (Score: {l['score']}). Unlock direct client link and outreach template on AutoScope.</description>
          <pubDate>{pub_date}</pubDate>
          <guid isPermaLink="false">{html.escape(l['url'])}</guid>
        </item>"""

    rss_xml = f"""<?xml version="1.0" encoding="UTF-8" ?>
<rss version="2.0">
  <channel>
    <title>AutoScope Daily High-Intent Contract Feed</title>
    <link>{html.escape(GUMROAD_CHECKOUT_URL)}</link>
    <description>Daily Python, Scraping, VBA, and Apps Script Contracts</description>
    {items_xml}
  </channel>
</rss>"""

    (out_dir / "rss.xml").write_text(rss_xml, encoding="utf-8")
    print("[INFO] Built public/rss.xml syndication feed.")


def main():
    raw_leads = (
        scrape_reddit_forhire()
        + scrape_remoteok()
        + scrape_hackernews()
        + scrape_python_org_jobs()
    )

    # Deduplicate by URL and sort highest score first
    deduped = {lead["url"]: lead for lead in raw_leads}
    all_leads = sorted(deduped.values(), key=lambda x: x["score"], reverse=True)
    print(f"[INFO] Total unique high-intent leads scored: {len(all_leads)}")

    subscribers = sync_with_google_sheets(all_leads)
    send_daily_digest(subscribers, all_leads)
    build_public_teaser_site(all_leads)
    build_rss_feed(all_leads)


if __name__ == "__main__":
    main()
