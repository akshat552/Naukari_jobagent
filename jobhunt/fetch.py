"""Fetch jobs from public ATS APIs. No auth, no scraping, no ToS risk."""
from __future__ import annotations

import html
import re
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

import requests

UA = {"User-Agent": "jobhunt/1.0 (personal job search agent)"}
TIMEOUT = 20

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t\r\f\v]+")
_NL = re.compile(r"\n{3,}")


def strip_html(raw: str | None) -> str:
    if not raw:
        return ""
    text = html.unescape(raw)
    text = re.sub(r"<\s*(br|/p|/div|/li|/h[1-6])\s*/?>", "\n", text, flags=re.I)
    text = _TAG.sub(" ", text)
    text = html.unescape(text)
    text = _WS.sub(" ", text)
    text = _NL.sub("\n\n", text)
    return text.strip()


@dataclass
class Job:
    job_id: str          # stable global id for dedupe: "<ats>:<slug>:<id>"
    ats: str
    company: str
    title: str
    location: str
    url: str
    description: str
    posted_at: str | None = None
    salary: str | None = None
    applicants: int | None = None
    # filled in later by the pipeline
    score: float | None = None
    reason: str | None = None
    draft: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def enrich_job_description(job: Job, session: requests.Session | None = None) -> Job:
    """Enrich job with full JD text on-demand for shortlisted roles."""
    if len(job.description) > 500:
        return job
    sess = session or requests
    if job.ats == "linkedin":
        m = re.search(r'(\d{8,})', job.url) or re.search(r'(\d{8,})', job.job_id)
        if m:
            job_num = m.group(1)
            try:
                r = sess.get(f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_num}",
                             headers={**UA, "Accept-Language": "en-US,en;q=0.9"}, timeout=TIMEOUT)
                if r.status_code == 200 and r.text:
                    full_desc = strip_html(r.text)
                    if len(full_desc) > len(job.description):
                        job.description = full_desc
            except Exception:
                pass
    return job


# --------------------------------------------------------------------------
# Adapters. Each takes the raw JSON body and returns list[Job].
# Keeping parse separate from HTTP is what makes offline testing possible.
# --------------------------------------------------------------------------

def parse_greenhouse(slug: str, company: str, body: Any) -> list[Job]:
    out = []
    for j in (body or {}).get("jobs", []):
        loc = (j.get("location") or {}).get("name") or ""
        out.append(Job(
            job_id=f"greenhouse:{slug}:{j.get('id')}",
            ats="greenhouse",
            company=company,
            title=(j.get("title") or "").strip(),
            location=loc.strip(),
            url=j.get("absolute_url") or "",
            description=strip_html(j.get("content")),
            posted_at=j.get("updated_at") or j.get("first_published"),
        ))
    return out


def parse_lever(slug: str, company: str, body: Any) -> list[Job]:
    out = []
    for j in (body or []):
        cats = j.get("categories") or {}
        # Lever splits the JD across descriptionPlain + a `lists` array.
        chunks = [j.get("descriptionPlain") or strip_html(j.get("description"))]
        for lst in (j.get("lists") or []):
            chunks.append(str(lst.get("text") or ""))
            chunks.append(strip_html(lst.get("content")))
        chunks.append(j.get("additionalPlain") or strip_html(j.get("additional")))
        ts = j.get("createdAt")
        posted = None
        if isinstance(ts, (int, float)):
            posted = time.strftime("%Y-%m-%d", time.gmtime(ts / 1000))
        out.append(Job(
            job_id=f"lever:{slug}:{j.get('id')}",
            ats="lever",
            company=company,
            title=(j.get("text") or "").strip(),
            location=(cats.get("location") or "").strip(),
            url=j.get("hostedUrl") or j.get("applyUrl") or "",
            description="\n\n".join(c for c in chunks if c).strip(),
            posted_at=posted,
            salary=cats.get("commitment"),
        ))
    return out


def parse_ashby(slug: str, company: str, body: Any) -> list[Job]:
    out = []
    for j in (body or {}).get("jobs", []):
        if j.get("isListed") is False:
            continue
        comp = j.get("compensation") or {}
        salary = None
        summary = comp.get("compensationTierSummary") or comp.get("summaryComponents")
        if isinstance(summary, str):
            salary = summary
        out.append(Job(
            job_id=f"ashby:{slug}:{j.get('id')}",
            ats="ashby",
            company=company,
            title=(j.get("title") or "").strip(),
            location=(j.get("location") or "").strip(),
            url=j.get("jobUrl") or j.get("applyUrl") or "",
            description=(j.get("descriptionPlain") or strip_html(j.get("descriptionHtml")) or "").strip(),
            posted_at=j.get("publishedAt"),
            salary=salary,
        ))
    return out


def parse_smartrecruiters(slug: str, company: str, body: Any) -> list[Job]:
    out = []
    postings = (body or {}).get("content", []) if isinstance(body, dict) else (body or [])
    for j in postings:
        loc_data = j.get("location") or {}
        loc_parts = [loc_data.get("city"), loc_data.get("region"), loc_data.get("country")]
        loc_str = ", ".join(p for p in loc_parts if p)
        job_id_num = str(j.get("id") or "")
        job_url = f"https://jobs.smartrecruiters.com/{slug}/{job_id_num}" if job_id_num else ""
        
        desc_chunks = []
        if j.get("jobAd"):
            for section in (j.get("jobAd", {}).get("sections") or {}).values():
                desc_chunks.append(strip_html(section.get("text") or ""))
        
        posted = j.get("releasedDate") or j.get("createdOn")
        if posted and "T" in posted:
            posted = posted.split("T")[0]
            
        out.append(Job(
            job_id=f"smartrecruiters:{slug}:{job_id_num}",
            ats="smartrecruiters",
            company=company,
            title=(j.get("name") or "").strip(),
            location=loc_str.strip(),
            url=job_url,
            description="\n\n".join(c for c in desc_chunks if c).strip() or (j.get("name") or ""),
            posted_at=posted,
        ))
    return out


def parse_workday(slug: str, company: str, body: Any, base_host: str = "", site: str = "") -> list[Job]:
    out = []
    postings = (body or {}).get("jobPostings", []) if isinstance(body, dict) else []
    for j in postings:
        title = (j.get("title") or "").strip()
        loc = (j.get("locationsText") or "").strip()
        ext_path = j.get("externalPath") or ""
        
        if ext_path.startswith("http"):
            job_url = ext_path
        elif base_host and site:
            job_url = f"https://{base_host}/en-US/{site}{ext_path}"
        elif base_host:
            job_url = f"https://{base_host}{ext_path}"
        else:
            job_url = f"https://{slug}.myworkdayjobs.com{ext_path}"
            
        raw_id = ext_path.split("_")[-1] if "_" in ext_path else (ext_path.split("/")[-1] or title)
        posted_raw = j.get("postedOn") or ""
        
        bullets = " | ".join(str(b) for b in j.get("bulletFields", []) if b)
        desc = f"{title}\n{bullets}" if bullets else title

        out.append(Job(
            job_id=f"workday:{slug}:{raw_id}",
            ats="workday",
            company=company,
            title=title,
            location=loc,
            url=job_url,
            description=desc,
            posted_at=posted_raw if re.match(r"^\d{4}-\d{2}-\d{2}", posted_raw) else None,
        ))
    return out


def parse_linkedin(slug: str, company: str, html_text: str) -> list[Job]:
    jobs = []
    if not html_text:
        return []
    cards = re.findall(r'<li[^>]*>(.*?)</li>', html_text, flags=re.DOTALL)
    for card in cards:
        applicants = None
        app_m = re.search(r'([0-9]+)\s+applicants|Over\s+100\s+applicants', card, flags=re.I)
        if app_m:
            app_text = app_m.group(0).lower()
            if "over 100" in app_text:
                applicants = 101
            else:
                digits = re.findall(r'\d+', app_text)
                if digits:
                    applicants = int(digits[0])

        title_m = re.search(r'<h3 class="base-search-card__title"[^>]*>(.*?)</h3>', card, flags=re.DOTALL)
        if not title_m:
            continue
        title = html.unescape(re.sub(r'<[^>]+>', '', title_m.group(1))).strip()

        comp_m = re.search(r'<h4 class="base-search-card__subtitle"[^>]*>(.*?)</h4>', card, flags=re.DOTALL)
        comp = html.unescape(re.sub(r'<[^>]+>', '', comp_m.group(1))).strip() if comp_m else company

        loc_m = re.search(r'<span class="job-search-card__location"[^>]*>(.*?)</span>', card, flags=re.DOTALL)
        loc = html.unescape(re.sub(r'<[^>]+>', '', loc_m.group(1))).strip() if loc_m else ""

        link_m = re.search(r'href="(https://[^"]+)"', card)
        url = link_m.group(1).split('?')[0] if link_m else ""

        # Parse posted date from datetime attribute or relative text (e.g. "12 hours ago", "2 days ago")
        time_tag_m = re.search(r'<time[^>]*datetime="([^"]+)"[^>]*>(.*?)</time>', card, flags=re.DOTALL)
        if time_tag_m:
            dt_attr = time_tag_m.group(1).strip()
            inner_text = time_tag_m.group(2).strip().lower()
            if "hour" in inner_text or "minute" in inner_text or "just now" in inner_text or "today" in inner_text:
                posted_at = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            elif "day" in inner_text:
                days_m = re.findall(r'\d+', inner_text)
                days_ago = int(days_m[0]) if days_m else 1
                posted_at = (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%d")
            elif "week" in inner_text:
                weeks_m = re.findall(r'\d+', inner_text)
                weeks_ago = int(weeks_m[0]) if weeks_m else 1
                posted_at = (datetime.now(timezone.utc) - timedelta(days=weeks_ago * 7)).strftime("%Y-%m-%d")
            elif "month" in inner_text:
                months_m = re.findall(r'\d+', inner_text)
                months_ago = int(months_m[0]) if months_m else 1
                posted_at = (datetime.now(timezone.utc) - timedelta(days=months_ago * 30)).strftime("%Y-%m-%d")
            else:
                posted_at = dt_attr
        else:
            time_m = re.search(r'datetime="([^"]+)"', card)
            posted_at = time_m.group(1) if time_m else None

        job_id_m = re.search(r'jobPosting:(\d+)|view/([^/?]+)', card)
        job_id_val = job_id_m.group(1) or job_id_m.group(2) if job_id_m else str(abs(hash(url or title)))

        jobs.append(Job(
            job_id=f"linkedin:{slug}:{job_id_val}",
            ats="linkedin",
            company=comp,
            title=title,
            location=loc,
            url=url,
            description=f"{title} at {comp} ({loc})",
            posted_at=posted_at,
            applicants=applicants,
        ))
    return jobs


ENDPOINTS = {
    "greenhouse": ("https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true", parse_greenhouse),
    "lever":      ("https://api.lever.co/v0/postings/{slug}?mode=json", parse_lever),
    "ashby":      ("https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true", parse_ashby),
    "smartrecruiters": ("https://api.smartrecruiters.com/v1/companies/{slug}/postings", parse_smartrecruiters),
}


def ensure_chrome_running(port: int = 9222) -> bool:
    """Ensure Chrome is running on port 9222 with CDP enabled, or auto-launch it."""
    import os
    import subprocess
    try:
        requests.get(f"http://localhost:{port}/json/version", timeout=1)
        return True
    except Exception:
        pass

    chrome_paths = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expanduser(r"~\AppData\Local\Google\Chrome\Application\chrome.exe")
    ]
    exe = next((p for p in chrome_paths if os.path.exists(p)), None)
    if exe:
        user_data = os.path.expanduser(r"~\chrome-naukri-profile")
        cmd = [
            exe,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={user_data}",
            "--no-first-run",
            "--no-default-browser-check"
        ]
        try:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(3)
            return True
        except Exception:
            pass
    return False


def fetch_naukri_cdp(query: str = "dot-net-developer", job_age: int = 1, experience: int = 3, pages: int = 2, port: int = 9222) -> list[Job]:
    """Connect to user's real open Chrome session or auto-launch background Chrome via CDP."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  ! naukri -> playwright not installed")
        return []

    ensure_chrome_running(port)

    jobs = []
    seen_ids = set()
    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.connect_over_cdp(f"http://localhost:{port}", timeout=8000)
            except Exception:
                print(f"  ! naukri -> Chrome could not be attached on port {port}")
                return []

            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.new_page()

            clean_query = query.strip().replace(" ", "-")
            raw_k = query.strip().replace("-", " ")
            encoded_k = requests.utils.quote(raw_k)

            for pg in range(1, pages + 1):
                if pg == 1:
                    url = f"https://www.naukri.com/jobs-in-india?k={encoded_k}&jobAge={job_age}&experience={experience}"
                else:
                    url = f"https://www.naukri.com/jobs-in-india-{pg}?k={encoded_k}&jobAge={job_age}&experience={experience}"

                page.goto(url, wait_until="domcontentloaded", timeout=20000)
                try:
                    page.wait_for_selector(".srp-jobtuple-wrapper, .cust-job-tuple, article", state="attached", timeout=4000)
                except Exception:
                    pass

                cards = page.query_selector_all(".srp-jobtuple-wrapper, .cust-job-tuple, article")
                if not cards:
                    break

                for card in cards:
                    title_el = card.query_selector("a.title")
                    if not title_el:
                        continue
                    title = title_el.inner_text().strip()
                    url_raw = title_el.get_attribute("href") or ""
                    url_clean = url_raw.split("?")[0] if url_raw else ""

                    comp_el = card.query_selector("a.comp-name, .comp-name")
                    comp = comp_el.inner_text().strip() if comp_el else "Naukri Employer"

                    loc_el = card.query_selector(".loc-wrap, .locWdth, .location")
                    loc = loc_el.inner_text().strip() if loc_el else ""

                    desc_el = card.query_selector(".job-desc, .row6")
                    desc = desc_el.inner_text().strip() if desc_el else f"{title} at {comp}"

                    card_text = card.inner_text()
                    app_m = re.search(r'(\d+)\s+Applicants|Over\s+100\s+Applicants', card_text, flags=re.I)
                    apps = int(app_m.group(1)) if (app_m and app_m.group(1)) else (101 if (app_m and "over 100" in app_m.group(0).lower()) else None)

                    job_id_val = str(abs(hash(url_clean or title)))
                    job_id_m = re.search(r'-(\d{5,})\?', card_text)
                    if job_id_m:
                        job_id_val = job_id_m.group(1)

                    full_id = f"naukri:{clean_query}:{job_id_val}"
                    if full_id not in seen_ids:
                        seen_ids.add(full_id)
                        jobs.append(Job(
                            job_id=full_id,
                            ats="naukri",
                            company=comp,
                            title=title,
                            location=loc,
                            url=url_clean,
                            description=desc,
                            posted_at=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                            applicants=apps,
                        ))
            page.close()
    except Exception as e:
        print(f"  ! naukri -> CDP fetch failed ({type(e).__name__}: {e})")
        return []
    return jobs


def fetch_linkedin_cdp(slug: str, company: str, query: str = "", location: str = "India", port: int = 9222) -> list[Job]:
    """Scrape LinkedIn job listings using authentic Chrome session via Playwright CDP."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  ! linkedin -> playwright not installed")
        return []

    ensure_chrome_running(port)

    jobs = []
    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.connect_over_cdp(f"http://localhost:{port}", timeout=8000)
            except Exception:
                print(f"  ! linkedin/{slug} -> Chrome could not be attached on port {port}")
                return []

            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.new_page()

            search_query = query or f"{company} developer"
            encoded_q = requests.utils.quote(search_query)
            encoded_loc = requests.utils.quote(location)
            url = f"https://www.linkedin.com/jobs/search?keywords={encoded_q}&location={encoded_loc}&f_TPR=r86400&sortBy=DD"

            page.goto(url, wait_until="domcontentloaded", timeout=25000)
            time.sleep(3)

            cards = page.query_selector_all(".base-card, .job-search-card, .jobs-search__results-list li, div[data-entity-urn], div.job-card-container")
            for card in cards:
                title_el = card.query_selector(".base-search-card__title, h3, a.job-card-list__title, a.job-card-container__link strong, a.job-card-list__title strong")
                if not title_el:
                    continue
                title = title_el.inner_text().strip()

                link_el = card.query_selector("a.base-card__full-link, a.job-card-list__title, a[href*='/jobs/view/'], a.job-card-container__link")
                raw_url = link_el.get_attribute("href") if link_el else ""
                clean_url = raw_url.split("?")[0] if raw_url else ""

                comp_el = card.query_selector(".base-search-card__subtitle, .job-card-container__company-name, h4")
                comp = comp_el.inner_text().strip() if comp_el else company

                loc_el = card.query_selector(".job-search-card__location, .job-card-container__metadata-item")
                loc = loc_el.inner_text().strip() if loc_el else location

                # Parse relative date
                time_el = card.query_selector("time")
                posted_at = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                if time_el:
                    time_text = time_el.inner_text().lower()
                    if "day" in time_text:
                        days_m = re.findall(r'\d+', time_text)
                        days = int(days_m[0]) if days_m else 1
                        posted_at = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
                    elif "week" in time_text:
                        weeks_m = re.findall(r'\d+', time_text)
                        weeks = int(weeks_m[0]) if weeks_m else 1
                        posted_at = (datetime.now(timezone.utc) - timedelta(days=weeks * 7)).strftime("%Y-%m-%d")

                # Parse applicant count
                card_text = card.inner_text()
                app_m = re.search(r'(\d+)\s+applicant|Over\s+(\d+)\s+applicant|Under\s+(\d+)\s+applicant', card_text, flags=re.I)
                applicants = None
                if app_m:
                    digits = [int(g) for g in app_m.groups() if g and g.isdigit()]
                    if digits:
                        applicants = digits[0]

                job_id_m = re.search(r'(\d{8,})', clean_url)
                job_id_val = job_id_m.group(1) if job_id_m else str(abs(hash(clean_url or title)))

                jobs.append(Job(
                    job_id=f"linkedin:{slug}:{job_id_val}",
                    ats="linkedin",
                    company=comp,
                    title=title,
                    location=loc,
                    url=clean_url,
                    description=f"{title} at {comp} ({loc})",
                    posted_at=posted_at,
                    applicants=applicants,
                ))
            page.close()
    except Exception as e:
        print(f"  ! linkedin/{slug} -> CDP fetch failed ({type(e).__name__}: {e})")
        return []
    return jobs


def fetch_board(ats: str, slug: str, company: str | None = None,
                session: requests.Session | None = None,
                **kwargs) -> list[Job]:
    """Hit one company's public board. Returns [] on any failure (never raises)."""
    sess = session or requests
    comp_name = company or slug
    try:
        if ats in ENDPOINTS:
            url_tpl, parser = ENDPOINTS[ats]
            r = sess.get(url_tpl.format(slug=slug), headers=UA, timeout=TIMEOUT)
            if r.status_code != 200:
                print(f"  ! {ats}/{slug} -> HTTP {r.status_code}")
                return []
            return parser(slug, comp_name, r.json())

        elif ats == "workday":
            host = kwargs.get("host") or f"{slug}.myworkdayjobs.com"
            site = kwargs.get("site") or f"{slug}_Careers"
            tenant = kwargs.get("tenant") or slug
            cxs_url = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
            payload = {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": ""}
            headers = {**UA, "Content-Type": "application/json", "Accept": "application/json"}
            r = sess.post(cxs_url, json=payload, headers=headers, timeout=TIMEOUT)
            if r.status_code != 200:
                print(f"  ! {ats}/{slug} -> HTTP {r.status_code}")
                return []
            return parse_workday(slug, comp_name, r.json(), base_host=host, site=site)

        elif ats in ("linkedin", "linkedin_cdp", "linkedin_both"):
            query = kwargs.get("query") or f"{comp_name} developer"
            location = kwargs.get("location") or "India"
            use_cdp_only = kwargs.get("cdp", False) or (ats == "linkedin_cdp")
            double_check = kwargs.get("double_check", True)
            port = kwargs.get("port") or 9222
            pages = int(kwargs.get("pages") or 5)

            if use_cdp_only:
                cdp_jobs = fetch_linkedin_cdp(slug=slug, company=comp_name, query=query, location=location, port=port)
                return cdp_jobs, f"linkedin-cdp: {len(cdp_jobs)}"

            encoded_q = requests.utils.quote(query)
            encoded_loc = requests.utils.quote(location)
            all_jobs = []
            seen_ids = set()
            http_count = 0
            cdp_count = 0

            # 1. Fast HTTP API Check (up to 3 pages)
            for p in range(pages):
                start = p * 10
                url = f"https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?keywords={encoded_q}&location={encoded_loc}&f_TPR=r86400&sortBy=DD&start={start}"
                r = sess.get(url, headers={**UA, "Accept-Language": "en-US,en;q=0.9"}, timeout=TIMEOUT)
                if r.status_code == 200:
                    page_jobs = parse_linkedin(slug, comp_name, r.text)
                    if not page_jobs:
                        break
                    for pj in page_jobs:
                        http_count += 1
                        if pj.job_id not in seen_ids:
                            seen_ids.add(pj.job_id)
                            all_jobs.append(pj)
                else:
                    break
                if p < pages - 1:
                    time.sleep(0.3)

            # 2. Playwright CDP Browser Check (double-check & merge anything unique)
            if double_check or not all_jobs:
                cdp_jobs = fetch_linkedin_cdp(slug=slug, company=comp_name, query=query, location=location, port=port)
                cdp_count = len(cdp_jobs)
                for cj in cdp_jobs:
                    if cj.job_id not in seen_ids:
                        seen_ids.add(cj.job_id)
                        all_jobs.append(cj)

            detail = f"linkedin [http: {http_count}, cdp: {cdp_count} -> unique: {len(all_jobs)}]"
            return all_jobs, detail

        elif ats in ("naukri", "naukri_cdp"):
            query = kwargs.get("query") or "dot-net-developer"
            job_age = kwargs.get("job_age") or 1
            experience = kwargs.get("experience") or 3
            port = kwargs.get("port") or 9222
            pages = int(kwargs.get("pages") or 2)
            naukri_jobs = fetch_naukri_cdp(query=query, job_age=job_age, experience=experience, pages=pages, port=port)
            return naukri_jobs, "naukri-cdp"

        elif ats in ("custom", "direct"):
            endpoint_url = kwargs.get("endpoint_url") or kwargs.get("url")
            if not endpoint_url:
                return [], ats
            r = sess.get(endpoint_url, headers=UA, timeout=TIMEOUT)
            if r.status_code != 200:
                print(f"  ! {ats}/{slug} -> HTTP {r.status_code}")
                return [], ats
            return [], ats

        else:
            print(f"  ! unknown ATS: {ats} for {comp_name}")
            return [], ats

    except Exception as e:  # dead slug, rate limit, network blip
        print(f"  ! {ats}/{slug} -> {type(e).__name__}: {e}")
        return [], ats


def fetch_all(companies: Iterable[dict], sleep: float = 0.25) -> list[Job]:
    jobs: list[Job] = []
    session = requests.Session()
    for c in companies:
        ats = c.get("ats", "greenhouse")
        slug = c.get("slug", "")
        name = c.get("name") or slug
        extra = {k: v for k, v in c.items() if k not in ("ats", "slug", "name")}
        res = fetch_board(ats, slug, name, session=session, **extra)
        if isinstance(res, tuple) and len(res) == 2:
            got, detail = res
        else:
            got, detail = res, ats
        print(f"  {name:<28} {len(got):>4} jobs  ({detail})")
        jobs.extend(got)
        per_board_sleep = 1.0 if ats == "linkedin" else sleep
        time.sleep(per_board_sleep)
    return jobs

