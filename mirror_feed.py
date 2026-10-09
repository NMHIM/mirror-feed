#!/usr/bin/env python3
"""Mirror · daily official feed.

Every morning this collects student jobs (Werkstudent, working student,
internship, Praktikum, thesis …) in Berlin from companies' OWN career systems,
and student events from universities' OWN calendars. New items go into the
`feed_items` table as "pending"; nothing is shown in Mirror until an admin
approves it in Settings → Admin → Review feed.

  python mirror_feed.py            # normal run (needs SUPABASE_URL + SUPABASE_SERVICE_KEY)
  python mirror_feed.py --dry-run  # only print what it found, write nothing

Only the Python standard library is used.
"""
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

BERLIN = ZoneInfo("Europe/Berlin")
UA = "MirrorFeed/1.0 (+https://vvillol.com; daily, one request per source)"
HERE = os.path.dirname(os.path.abspath(__file__))
MAX_NEW_PER_SOURCE = 30          # never flood the review list from one source
EVENT_DAYS_AHEAD = 60            # only events in the next two months

# ---------------------------------------------------------------------------
# what counts as a student job, and where
# ---------------------------------------------------------------------------
STUDENT = re.compile(
    r"werkstudent|working[\s\-]?student|student(?:ische[rn]?)?[\s\-]*(?:assistant|assistenz|hilfskraft|mitarbeiter|job|worker|aushilfe)"
    r"|studentische|hilfskraft|praktik|internship|\bintern\b|\binterns\b|thesis|abschlussarbeit|bachelorarbeit|masterarbeit"
    r"|duales?\s+studium|dual(?:e|er)?\s+student|studienbegleitend|student\s+(?:position|role)",
    re.I)
NOT_STUDENT = re.compile(r"\bsenior\b|\bhead of\b|\bdirector\b|\bprincipal\b|\bteam lead\b|\bvp\b", re.I)
REMOTE_DE = re.compile(r"remote|hybrid|home\s*office|anywhere", re.I)
GERMANY = re.compile(r"germany|deutschland|\bdach\b|europe|\bemea\b", re.I)


def is_student(title):
    return bool(STUDENT.search(title or "")) and not NOT_STUDENT.search(title or "")


def in_berlin(places, remote=False, extra=()):
    text = " | ".join(p for p in places if p).lower()
    if "berlin" in text or any(x.lower() in text for x in extra):
        return True
    if (remote or REMOTE_DE.search(text)) and GERMANY.search(text):
        return True
    return False


def pick_place(places, fallback=""):
    """The Berlin location if the job has one, else the first one listed."""
    places = [clean(p) for p in places if p and clean(p)]
    return next((p for p in places if "berlin" in p.lower()), places[0] if places else fallback)


def clean(s, n=None):
    s = html.unescape(re.sub(r"<[^>]+>", " ", str(s or "")))
    s = re.sub(r"\s+", " ", s).strip()
    return s[:n].rstrip() if n else s


# ---------------------------------------------------------------------------
# network
# ---------------------------------------------------------------------------
def get(url, accept="application/json", tries=2):
    last = None
    for i in range(tries):
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read().decode(r.headers.get_content_charset() or "utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code in (404, 410):
                raise
            last = e
        except Exception as e:  # timeouts, resets
            last = e
        time.sleep(2 + 3 * i)
    raise last


def get_json(url):
    return json.loads(get(url))


# ---------------------------------------------------------------------------
# job sources: each returns a list of dicts
#   {ext_id, title, company, place, link, details}
# ---------------------------------------------------------------------------
def job(c, ext_id, title, place, link, details=""):
    return {"kind": "job", "ext_id": str(ext_id), "title": clean(title, 120), "company": c["name"],
            "place": clean(place, 90), "link": link, "details": ("📍 " + clean(details, 300)) if clean(details) else "", "audience": "all"}


def greenhouse(c):
    d = get_json(f"https://boards-api.greenhouse.io/v1/boards/{c['token']}/jobs")
    out = []
    for j in d.get("jobs", []):
        loc = (j.get("location") or {}).get("name", "")
        if is_student(j.get("title")) and in_berlin([loc], extra=c.get("extra_places", ())):
            shown = "Berlin" if "berlin" not in loc.lower() and any(x in loc.lower() for x in c.get("extra_places", ())) else loc
            out.append(job(c, j["id"], j["title"], shown, j["absolute_url"], shown))
    return out


def lever(c):
    rows = None
    for host in ("api.lever.co", "api.eu.lever.co"):
        try:
            rows = get_json(f"https://{host}/v0/postings/{c['token']}?mode=json")
            if rows:
                break
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
    if rows is None:
        raise RuntimeError("no Lever board with this name")
    out = []
    for j in rows:
        cat = j.get("categories") or {}
        places = [cat.get("location", "")] + list(cat.get("allLocations") or []) + [j.get("country", "")]
        title = j.get("text", "")
        commit = cat.get("commitment", "")
        if (is_student(title) or is_student(commit)) and in_berlin(places, remote=j.get("workplaceType") == "remote"):
            out.append(job(c, j["id"], title, pick_place(places), j.get("hostedUrl") or j.get("applyUrl"),
                           " · ".join(x for x in [cat.get("location", ""), commit, j.get("workplaceType", "")] if x and x != "unspecified")))
    return out


def ashby(c):
    d = get_json(f"https://api.ashbyhq.com/posting-api/job-board/{c['token']}")
    out = []
    for j in d.get("jobs", []):
        if j.get("isListed") is False:
            continue
        places = [j.get("location", "")]
        for s in j.get("secondaryLocations") or []:
            places.append(s.get("location", "") if isinstance(s, dict) else str(s))
        addr = ((j.get("address") or {}).get("postalAddress") or {})
        places += [addr.get("addressLocality", ""), addr.get("addressCountry", "")]
        title = j.get("title", "")
        if (is_student(title) or j.get("employmentType") == "Intern") and in_berlin(places, remote=bool(j.get("isRemote"))):
            out.append(job(c, j.get("id") or j.get("jobUrl"), title, pick_place(places), j.get("jobUrl") or j.get("applyUrl"),
                           " · ".join(x for x in [pick_place(places), j.get("workplaceType", "")] if x)))
    return out


def personio(c):
    domains = [c.get("domain", "de")] + [d for d in ("de", "com") if d != c.get("domain", "de")]
    root, dom = None, None
    for d in domains:
        try:
            root = ET.fromstring(get(f"https://{c['token']}.jobs.personio.{d}/xml", accept="application/xml"))
            dom = d
            break
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
    if root is None:
        raise RuntimeError("no Personio board with this name")
    out = []
    for p in root.findall("position"):
        t = lambda tag: (p.findtext(tag) or "").strip()
        offices = [t("office")] + [(o.text or "").strip() for o in p.findall("additionalOffices/office")]
        title = t("name")
        if (is_student(title) or t("employmentType") in ("intern", "trainee", "working_student")) and in_berlin(offices):
            pid = t("id")
            out.append(job(c, pid, title, pick_place(offices),
                           f"https://{c['token']}.jobs.personio.{dom}/job/{pid}",
                           " · ".join(x for x in [pick_place(offices), t("schedule").replace("-", " ")] if x)))
    return out


def smartrecruiters(c):
    out, offset = [], 0
    while offset < 1000:
        d = get_json(f"https://api.smartrecruiters.com/v1/companies/{c['token']}/postings?limit=100&offset={offset}")
        rows = d.get("content", [])
        for j in rows:
            loc = j.get("location") or {}
            places = [loc.get("city", ""), loc.get("fullLocation", ""), loc.get("country", "")]
            title = j.get("name", "")
            level = ((j.get("experienceLevel") or {}).get("id") or "")
            if (is_student(title) or level == "internship") and in_berlin(places, remote=bool(loc.get("remote"))):
                out.append(job(c, j["id"], title, pick_place(places),
                               f"https://jobs.smartrecruiters.com/{c['token']}/{j['id']}",
                               " · ".join(x for x in [loc.get("city", ""), (j.get("typeOfEmployment") or {}).get("label", "")] if x)))
        offset += 100
        if offset >= d.get("totalFound", 0) or not rows:
            break
    return out


def recruitee(c):
    d = get_json(f"https://{c['token']}.recruitee.com/api/offers/")
    out = []
    for j in d.get("offers", []):
        places = [j.get("city", ""), j.get("location", ""), j.get("country", "")]
        for l in j.get("locations") or []:
            if isinstance(l, dict):
                places += [l.get("city", ""), l.get("name", ""), l.get("country", "")]
        title = j.get("title", "")
        if (is_student(title) or j.get("employment_type_code") in ("internship", "traineeship")) and in_berlin(places, remote=bool(j.get("remote"))):
            out.append(job(c, j.get("id"), title, pick_place(places), j.get("careers_url"),
                           " · ".join(x for x in [pick_place(places), (j.get("employment_type_code") or "").replace("_", " ")] if x)))
    return out


def workable(c):
    d = get_json(f"https://apply.workable.com/api/v1/widget/accounts/{c['token']}")
    out = []
    for j in d.get("jobs", []):
        places = [j.get("city", ""), j.get("country", "")]
        for l in j.get("locations") or []:
            if isinstance(l, dict):
                places += [l.get("city", ""), l.get("country", "")]
        title = j.get("title", "")
        if (is_student(title) or "intern" in (j.get("employment_type") or "").lower()) and in_berlin(places, remote=bool(j.get("telecommuting"))):
            link = j.get("url") or j.get("shortlink") or f"https://apply.workable.com/{c['token']}/j/{j.get('shortcode')}/"
            out.append(job(c, j.get("shortcode") or link, title, pick_place(places), link,
                           " · ".join(x for x in [pick_place(places), j.get("employment_type", "")] if x)))
    return out


ATS = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby, "personio": personio,
       "smartrecruiters": smartrecruiters, "recruitee": recruitee, "workable": workable}


# ---------------------------------------------------------------------------
# event sources: each returns {ext_id, title, company, place, starts_at, link, details}
# ---------------------------------------------------------------------------
def event(src, ext_id, title, start, place, link, details=""):
    return {"kind": "event", "ext_id": str(ext_id)[:300], "title": clean(title, 90), "company": src["name"],
            "place": clean(place, 90), "starts_at": start.astimezone(timezone.utc).isoformat(),
            "link": link, "details": clean(details, 300), "audience": src.get("uni", "all")}


def ics_dt(value, params):
    v = value.strip()
    if re.fullmatch(r"\d{8}", v):
        return datetime.strptime(v, "%Y%m%d").replace(hour=9, tzinfo=BERLIN)   # all-day: morning
    m = re.fullmatch(r"(\d{8}T\d{4,6})(Z?)", v)
    if not m:
        return None
    raw = m.group(1).ljust(15, "0")
    dt = datetime.strptime(raw, "%Y%m%dT%H%M%S")
    if m.group(2) == "Z":
        return dt.replace(tzinfo=timezone.utc)
    tz = params.get("TZID")
    try:
        return dt.replace(tzinfo=ZoneInfo(tz)) if tz else dt.replace(tzinfo=BERLIN)
    except Exception:
        return dt.replace(tzinfo=BERLIN)


def ics_unescape(s):
    return s.replace("\\n", " ").replace("\\N", " ").replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")


def parse_ics(text):
    lines = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw.startswith((" ", "\t")) and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    out, cur = [], None
    for line in lines:
        if line == "BEGIN:VEVENT":
            cur = {}
        elif line == "END:VEVENT":
            if cur is not None:
                out.append(cur)
            cur = None
        elif cur is not None and ":" in line:
            head, val = line.split(":", 1)
            name, *ps = head.split(";")
            params = dict(p.split("=", 1) for p in ps if "=" in p)
            cur[name.upper()] = (val, params)
    return out


class _Links(HTMLParser):
    """Flattens a page into text, remembering where each link starts and ends."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text, self.links, self._open, self._skip = [], [], None, 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self._skip += 1
        if tag == "a":
            self._open = (dict(attrs).get("href") or "", len("".join(self.text)))
        if tag in ("br", "p", "div", "li", "article", "h1", "h2", "h3", "h4", "time", "span", "td"):
            self.text.append(" ")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript") and self._skip:
            self._skip -= 1
        if tag == "a" and self._open:
            href, start = self._open
            self.links.append((href, start, len("".join(self.text))))
            self._open = None

    def handle_data(self, data):
        if not self._skip:
            self.text.append(data)


def in_window(start):
    now = datetime.now(timezone.utc)
    return start and now - timedelta(hours=2) <= start <= now + timedelta(days=EVENT_DAYS_AHEAD)


def ics_items(src, text, page=None):
    """Events from iCal text. Trusted: the organiser published the date themselves."""
    out = []
    for ev in parse_ics(text):
        if "DTSTART" not in ev:
            continue
        start = ics_dt(*ev["DTSTART"])
        if not in_window(start):
            continue
        title = ics_unescape(ev.get("SUMMARY", ("", {}))[0])
        title = re.sub(r"^(Vortrag|Workshop|Veranstaltung|Event|Lecture|Talk)\s*\|\s*", "", title).strip()
        link = ics_unescape(ev.get("URL", ("", {}))[0]).strip() or page or src.get("page") or \
            re.sub(r"index\.ics(\?format=ics)?$", "index.html", src["url"])
        uid = ev.get("UID", (link + start.isoformat(), {}))[0]
        it = event(src, uid, title, start, ics_unescape(ev.get("LOCATION", ("", {}))[0]), link,
                   ics_unescape(ev.get("DESCRIPTION", ("", {}))[0]))
        it["_trusted"] = True
        out.append(it)
    return out


def ics_source(src):
    return ics_items(src, get(src["url"], accept="text/calendar"))


def parse_when(v):
    """An ISO date/time from JSON-LD ('2026-10-12', '2026-10-12T18:00', '…+02:00', '…Z')."""
    if not isinstance(v, str) or not v.strip():
        return None
    v = v.strip().replace("Z", "+00:00")
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
            return datetime.fromisoformat(v).replace(hour=9, tzinfo=BERLIN)
        dt = datetime.fromisoformat(v)
        return dt if dt.tzinfo else dt.replace(tzinfo=BERLIN)
    except ValueError:
        return None


def jsonld_items(src, page, base):
    """schema.org Events embedded in the page (many university sites have them). Trusted."""
    found = []

    def walk(x):
        if isinstance(x, list):
            for y in x:
                walk(y)
        elif isinstance(x, dict):
            t = x.get("@type")
            types = t if isinstance(t, list) else [t]
            if any(isinstance(tt, str) and tt.endswith("Event") for tt in types):
                found.append(x)
            for k in ("@graph", "itemListElement", "item", "subEvent", "event", "events"):
                if k in x:
                    walk(x[k])
    for blk in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', page, re.S | re.I):
        try:
            walk(json.loads(html.unescape(blk.strip())))
        except Exception:
            continue
    out = []
    for e in found:
        start = parse_when(e.get("startDate"))
        if not in_window(start):
            continue
        loc = e.get("location")
        if isinstance(loc, list):
            loc = loc[0] if loc else ""
        if isinstance(loc, dict):
            addr = loc.get("address")
            loc = loc.get("name") or (addr.get("streetAddress") if isinstance(addr, dict) else addr) or ""
        link = e.get("url") or base
        if isinstance(link, list):
            link = link[0]
        link = urllib.parse.urljoin(base, str(link))
        it = event(src, e.get("@id") or (link + start.isoformat()), e.get("name") or "", start, loc or "", link,
                   e.get("description") or "")
        it["_trusted"] = True
        out.append(it)
    return out


def rss_items(src, text):
    """News feeds rarely carry the event date; only items with a clear date in their text count."""
    out = []
    try:
        root = ET.fromstring(text.encode("utf-8") if isinstance(text, str) else text)
    except ET.ParseError:
        return out
    ns = {"a": "http://www.w3.org/2005/Atom"}
    entries = root.findall(".//item") or root.findall(".//a:entry", ns)
    now = datetime.now(BERLIN)
    for it in entries:
        title = (it.findtext("title") or it.findtext("a:title", namespaces=ns) or "").strip()
        link = (it.findtext("link") or "").strip()
        if not link:
            le = it.find("a:link", ns)
            link = le.get("href") if le is not None else ""
        desc = clean(it.findtext("description") or it.findtext("a:summary", namespaces=ns) or "")
        m = DATE_RE.search(title) or DATE_RE.search(desc)
        start = date_from_match(m, now) if m else None
        if not start or not in_window(start) or not link:
            continue
        out.append(event(src, link, DATE_RE.sub(" ", title).strip(" -–|·,"), start, "", link, desc))
    return out


def date_from_match(m, now):
    d, mo = int(m.group(1)), int(m.group(2))
    y = m.group(3)
    y = int(y.strip()) if y else now.year
    if y < 100:
        y += 2000
    hh, mm = (int(m.group(4)), int(m.group(5))) if m.group(4) else (9, 0)
    try:
        start = datetime(y, mo, d, min(hh, 23), min(mm, 59), tzinfo=BERLIN)
    except ValueError:
        return None
    if not m.group(3) and start < now - timedelta(days=60):   # "10.01." seen in December means next year
        start = start.replace(year=y + 1)
    return start


_robots = {}
def robots_ok(url):
    """Respect robots.txt: if a site asks bots not to read a page, we don't."""
    import urllib.robotparser
    p = urllib.parse.urlparse(url)
    root = f"{p.scheme}://{p.netloc}"
    if root not in _robots:
        rp = urllib.robotparser.RobotFileParser()
        try:
            rp.parse(get(root + "/robots.txt", accept="text/plain", tries=1).splitlines())
        except Exception:
            rp = None                                  # no robots.txt: allowed
        _robots[root] = rp
    rp = _robots[root]
    return rp is None or rp.can_fetch(UA, url)


GENERIC_LINK = r"(veranstalt|event|termin|kalend|calend|detail|programm)"
DATE_RE = re.compile(r"(\d{1,2})\.\s?(\d{1,2})\.(\s?\d{4}|\s?\d{2}(?!\d))?(?:[^\d]{0,25}?(\d{1,2})[:.](\d{2})\s*(?:Uhr|h)?)?")


def html_items(src, page, base, pattern):
    """Careful, simple reading of an event list page: every link whose address matches
    the pattern is an event; its date is taken from the link text or the text right
    around it. Anything without a clear future date is skipped. Not trusted: these
    wait for your approval unless the source says "auto": true."""
    p = _Links()
    p.feed(page)
    text = "".join(p.text)
    pat = re.compile(pattern, re.I)
    host = urllib.parse.urlparse(base).netloc
    now = datetime.now(BERLIN)
    seen, out = set(), []
    for href, a, b in p.links:
        if not pat.search(href):
            continue
        link = urllib.parse.urljoin(base, href)
        if link in seen or urllib.parse.urlparse(link).netloc != host or link.rstrip("/") == base.rstrip("/"):
            continue
        title = re.sub(r"\s+", " ", text[a:b]).strip()
        m = DATE_RE.search(text[a:b]) or DATE_RE.search(text[b:b + 220])
        if not m:
            before = list(DATE_RE.finditer(text[max(0, a - 220):a]))
            m = before[-1] if before else None
        start = date_from_match(m, now) if m else None
        if not start or not in_window(start):
            continue
        title = DATE_RE.sub(" ", title)
        title = re.sub(r"\b(Mo|Di|Mi|Do|Fr|Sa|So|Mon|Tue|Wed|Thu|Fri|Sat|Sun)\b\.?,?", " ", title)
        title = re.sub(r"\s+", " ", title).strip(" -–|·,")
        if len(title) < 6 or title.lower() in ("mehr", "more", "details", "weiterlesen", "read more", "mehr erfahren"):
            continue
        seen.add(link)
        out.append(event(src, link, title, start, "", link))
    return out


def html_source(src):
    if not robots_ok(src["url"]):
        raise RuntimeError("the site's robots.txt asks bots not to read this page, so we don't")
    return html_items(src, get(src["url"], accept="text/html"), src["url"], src.get("link_pattern") or GENERIC_LINK)


def auto_source(src):
    """Reads whatever the page offers, best first: a calendar file, embedded event data,
    calendar links on the page (up to 20), a news feed, and finally the page itself."""
    url = src["url"]
    if not robots_ok(url):
        raise RuntimeError("the site's robots.txt asks bots not to read this page, so we don't")
    body = get(url, accept="text/html,application/xhtml+xml,text/calendar,application/rss+xml;q=0.9,*/*;q=0.8")
    head = body.lstrip()[:3000]
    if "BEGIN:VCALENDAR" in head:
        return ics_items(src, body)
    if re.search(r"<(rss|feed)[\s>]", head):
        return rss_items(src, body)
    items = jsonld_items(src, body, url)
    if items:
        return items
    host = urllib.parse.urlparse(url).netloc
    ics, rss = [], []
    for tag in re.findall(r"<(?:a|link)\b[^>]*>", body, re.I):
        href = re.search(r'href\s*=\s*["\']([^"\']+)["\']', tag, re.I)
        if not href:
            continue
        h = urllib.parse.urljoin(url, html.unescape(href.group(1)))
        if urllib.parse.urlparse(h).netloc != host:
            continue
        low = tag.lower()
        if re.search(r"\.ics(\b|/|\?|$)|[?&](ical|ics)=|format=ics|ics_view|downloadics|\.ical", h, re.I) or "text/calendar" in low:
            if h not in ics:
                ics.append(h)
        elif "application/rss+xml" in low or "application/atom+xml" in low:
            if h not in rss:
                rss.append(h)
    out = []
    for h in ics[:20]:
        try:
            out += ics_items(src, get(h, accept="text/calendar", tries=1), page=url if len(ics) > 3 else None)
        except Exception:
            pass
        time.sleep(0.3)
    if out:
        return out
    for h in rss[:2]:
        try:
            out += rss_items(src, get(h, accept="application/rss+xml", tries=1))
        except Exception:
            pass
    if out:
        return out
    return html_items(src, body, url, src.get("link_pattern") or GENERIC_LINK)


# ---------------------------------------------------------------------------
# Supabase
# ---------------------------------------------------------------------------
class DB:
    def __init__(self, url, key):
        self.base = url.rstrip("/") + "/rest/v1"
        self.h = {"apikey": key, "Content-Type": "application/json"}
        if key.startswith("eyJ"):                     # legacy service_role JWT
            self.h["Authorization"] = "Bearer " + key

    def req(self, method, path, body=None, prefer=None):
        h = dict(self.h)
        if prefer:
            h["Prefer"] = prefer
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(self.base + path, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(r, timeout=30) as resp:
                raw = resp.read().decode()
                return json.loads(raw) if raw.strip() else None
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"{method} {path} -> {e.code}: {e.read().decode()[:300]}")

    def check(self):
        """One test request first, so a setup problem gives a clear message."""
        try:
            self.req("GET", "/feed_items?select=id&limit=1")
        except RuntimeError as e:
            msg = str(e)
            if "-> 401" in msg or "-> 403" in msg or "Invalid API key" in msg or "JWT" in msg:
                sys.exit("STOP: Supabase refused the key. SUPABASE_SERVICE_KEY must be the SECRET key "
                         "(Supabase → Project Settings → API Keys → Secret keys, starts with sb_secret_ ; "
                         "or the legacy service_role key), not the publishable/anon key.\n" + msg)
            if "-> 404" in msg or "PGRST205" in msg or "42P01" in msg or "feed_items" in msg:
                sys.exit("STOP: the table feed_items doesn't exist yet. Run mirror_feed.sql once in "
                         "Supabase → SQL Editor, then run this again.\n" + msg)
            sys.exit("STOP: Supabase answered with an error.\n" + msg)
        except Exception as e:
            sys.exit(f"STOP: couldn't reach Supabase at {self.base}. Check SUPABASE_URL. ({type(e).__name__}: {e})")

    def existing(self, source):
        q = urllib.parse.quote(source, safe="")
        return {r["ext_id"]: r for r in self.req("GET", f"/feed_items?select=id,ext_id,status,published_id,kind&source=eq.{q}&limit=5000") or []}

    def upsert(self, rows):
        for i in range(0, len(rows), 200):
            self.req("POST", "/feed_items?on_conflict=source,ext_id", rows[i:i + 200],
                     prefer="resolution=merge-duplicates,return=minimal")

    def set_status(self, ids, status):
        if ids:
            self.req("PATCH", f"/feed_items?id=in.({','.join(map(str, ids))})", {"status": status}, prefer="return=minimal")

    def unpublish_jobs(self, post_ids):
        if post_ids:
            self.req("DELETE", f"/posts?author_id=eq.mirror&id=in.({','.join(map(str, post_ids))})", prefer="return=minimal")

    def approved_event_links(self):
        return {r["link"] for r in self.req("GET", "/feed_items?select=link&kind=eq.event&status=eq.approved&limit=10000") or []}

    def publish(self, fid, it):
        """Put one item live (as "Mirror") and mark it approved."""
        if it["kind"] == "job":
            row = {"author_id": "mirror", "author_name": "Mirror", "body": (it.get("details") or "")[:2000],
                   "room": "room:jobs", "kind": "job", "title": it["title"][:120],
                   "company": (it.get("company") or "Company")[:120], "link": it["link"]}
            table = "posts"
        else:
            aud = it.get("audience") or "all"
            row = {"author_id": "mirror", "author_name": "Mirror", "title": it["title"][:90], "starts_at": it["starts_at"],
                   "place": (it.get("place") or "")[:90] or None,
                   "details": " · ".join(x for x in [it.get("company"), it.get("details")] if x)[:400] or None,
                   "audience": aud if re.fullmatch(r"all|[A-Z]{2,6}", aud) else "all", "link": it["link"]}
            table = "events"
        res = self.req("POST", f"/{table}", row, prefer="return=representation")
        pid = res[0]["id"] if res else None
        self.req("PATCH", f"/feed_items?id=eq.{int(fid)}",
                 {"status": "approved", "published_id": pid, "decided_at": datetime.now(timezone.utc).isoformat()},
                 prefer="return=minimal")
        return pid

    def expire_events(self):
        now = datetime.now(timezone.utc).isoformat()
        self.req("PATCH", f"/feed_items?kind=eq.event&status=eq.pending&starts_at=lt.{urllib.parse.quote(now)}",
                 {"status": "gone"}, prefer="return=minimal")


# ---------------------------------------------------------------------------
def main():
    dry = "--dry-run" in sys.argv
    only = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--only=")), None)
    cfg = json.load(open(os.path.join(HERE, "sources.json"), encoding="utf-8"))
    db = None
    if not dry:
        url, key = os.environ.get("SUPABASE_URL", "").strip(), os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
        if not url or not key:
            sys.exit("STOP: the secrets SUPABASE_URL and SUPABASE_SERVICE_KEY are missing. Add both in GitHub → "
                     "this repository → Settings → Secrets and variables → Actions → New repository secret "
                     "(names exactly as written here).")
        url = re.sub(r"/(rest/v1)?/*$", "", url)
        if not re.match(r"^https://[a-z0-9-]+\.supabase\.co$", url):
            sys.exit(f"STOP: SUPABASE_URL should look like https://xxxx.supabase.co (it is: {url[:60]}).")
        db = DB(url, key)
        db.check()

    run_at = datetime.now(timezone.utc).isoformat()
    report, totals = [], {"new": 0, "seen": 0, "gone": 0, "failed": 0}
    # Automatic posting (sources.json → "auto_publish"). Jobs from career pages and events
    # from calendar files / embedded event data go live straight away; events read from a
    # plain web page wait in Review feed unless their source says "auto": true.
    auto = {"jobs": True, "events": True, **(cfg.get("auto_publish") or {})}
    live_links = db.approved_event_links() if db else set()
    EV = {"ics": ics_source, "html": html_source, "auto": auto_source}
    sources = [("job", c, f"{c['ats']}:{c['token']}".lower(), lambda c=c: ATS[c["ats"]](c)) for c in cfg["companies"]] + \
              [("event", e, f"{e['type']}:{e['id']}", lambda e=e: EV[e["type"]](e)) for e in cfg["events"]]
    for kind, src, key, fetch in sources:
        if src.get("off") or (only and only.lower() not in key):
            continue
        try:
            items = fetch()
        except Exception as e:
            totals["failed"] += 1
            report.append(f"✗ {src['name']} ({key}): {type(e).__name__}: {str(e)[:120]}")
            continue
        uniq = {}
        for it in items:
            if it.get("link", "").startswith("http") and len(it["title"]) >= 4:
                uniq[it["ext_id"]] = it
        items = list(uniq.values())
        if dry:
            report.append(f"• {src['name']} ({key}): {len(items)}")
            for it in items[:8]:
                report.append(f"    {it['title']} — {it.get('place') or it.get('starts_at', '')} — {it['link']}")
            totals["seen"] += len(items)
            continue
        try:
            have = db.existing(key)
            new = [it for it in items if it["ext_id"] not in have][:MAX_NEW_PER_SOURCE]
            keep = [it for it in items if it["ext_id"] in have]
            rows = [{**{k: v for k, v in it.items() if not k.startswith("_")}, "source": key, "last_seen": run_at} for it in keep + new]
            for r in rows:
                r.setdefault("starts_at", None)
            db.upsert(rows)
            # Jobs that are no longer on the company's page: take them down.
            gone = [r for ext, r in have.items() if ext not in uniq and r["status"] in ("pending", "approved") and r["kind"] == "job"]
            db.unpublish_jobs([r["published_id"] for r in gone if r["status"] == "approved" and r.get("published_id")])
            db.set_status([r["id"] for r in gone], "gone")
            # automatic posting
            published = 0
            ok = (kind == "job" and auto["jobs"]) or (kind == "event" and auto["events"])
            if ok and not src.get("review"):
                now_rows = db.existing(key)
                for ext, it in uniq.items():
                    r = now_rows.get(ext)
                    if not r or r["status"] != "pending":
                        continue
                    if kind == "event" and not (it.get("_trusted") or src.get("auto")):
                        continue
                    if kind == "event" and it["link"] in live_links:
                        db.set_status([r["id"]], "rejected")        # the same event from another calendar
                        continue
                    try:
                        db.publish(r["id"], it)
                        published += 1
                        if kind == "event":
                            live_links.add(it["link"])
                    except Exception as e:
                        report.append(f"✗ {src['name']}: couldn't post “{it['title'][:40]}”: {str(e)[:120]}")
            totals["live"] = totals.get("live", 0) + published
        except Exception as e:
            totals["failed"] += 1
            report.append(f"✗ {src['name']} ({key}): saving failed: {str(e)[:160]}")
            continue
        totals["new"] += len(new); totals["seen"] += len(items); totals["gone"] += len(gone)
        if new or gone or published:
            report.append(f"• {src['name']}: {len(new)} new, {published} posted, {len(gone)} closed")
    if db:
        db.expire_events()

    summary = (f"Mirror feed {datetime.now(BERLIN):%d.%m.%Y %H:%M} — {totals.get('live', 0)} posted automatically, "
               f"{totals['new']} new, {totals['seen']} found, {totals['gone']} closed, {totals['failed']} sources failed")
    print(summary)
    print("\n".join(report))
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(f"### {summary}\n\n" + "\n".join(f"- {r.strip()}" for r in report if r.strip()) + "\n")


if __name__ == "__main__":
    main()
