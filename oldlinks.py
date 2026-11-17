#!/usr/bin/env python3
"""oldlinks: are the old links in your repo still yours?

Scans the docs of a git repo (README, docs, changelogs...) and, for every link,
checks what can turn an old, harmless link into someone else's:

  1. Short links (bit.ly, tinyurl, ow.ly...): where they point today vs. where
     they pointed when the Wayback Machine first saw them. For bit.ly it also
     reads the link's creation date from its public "+" page.
  2. Domains: the domain's registration date (RDAP, asked to the registry
     itself, or whois) vs. the date of the commit that first added the link.
     A domain registered after you linked it, one that is not registered at
     all, or one that now shows a parking / for-sale page can be serving (or
     be bought to serve) anything.

Python 3.9+, standard library only, plus the `whois` command. Read-only: it
never changes the repo.
Usage: python3 oldlinks.py [repo] [--json out.json] [--html out.html]
                           [--cache file] [--max-age-days 7] [--ignore file]
"""
import argparse, html, json, os, re, subprocess, sys, threading, time
import urllib.error, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

VERSION = "0.2"
UA = f"oldlinks/{VERSION} (+link audit; read-only)"
DOC_GLOBS = ["*.md", "*.mdx", "*.markdown", "*.rst", "*.txt", "*.adoc", "*.html", "*.htm"]
SHORTENERS = {"bit.ly", "bitly.com", "j.mp", "tinyurl.com", "ow.ly", "t.co", "is.gd", "buff.ly",
              "rebrand.ly", "cutt.ly", "shorturl.at", "goo.gl", "git.io", "tiny.cc", "rb.gy", "lnkd.in"}
DEAD_SHORTENERS = {"goo.gl": "goo.gl was shut down by Google in 2025: replace it with the real URL"}
SKIP_HOSTS = re.compile(r"^(localhost|127\.|0\.0\.0\.0|\[|.*\.local$|.*\.test$|.*\.invalid$|.*\.example$|example\.(com|org|net)$)")
URL_RE = re.compile(r"https?://[^\s<>\"'`\]\[(){}|\\^]+")
TWO_LEVEL = {"co", "com", "org", "net", "ac", "gov", "edu", "ne", "or", "go"}
# Where parked and for-sale domains send visitors, and what their pages say.
PARKING_HOSTS = re.compile(r"(^|\.)(forsale\.godaddy\.com|afternic\.com|sedo\.com|sedoparking\.com|dan\.com|"
                           r"hugedomains\.com|parkingcrew\.net|bodis\.com|undeveloped\.com|atom\.com|"
                           r"squadhelp\.com|domainmarket\.com|buydomains\.com|parklogic\.com)$")
PARKING_TEXT = re.compile(r"window\.location\.href\s*=\s*[\"']/lander|this domain (name )?(is|may be) for sale|"
                          r"buy this domain|domain is parked|parked free|sedoparking|parkingcrew|"
                          r"utm_medium=parkedpages", re.I)


# ---------- small helpers ----------
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None

_noredir = urllib.request.build_opener(NoRedirect)


def fetch(url, follow=True, timeout=15):
    """Returns (status, headers, body_text, final_url). Never raises for HTTP errors."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    opener = urllib.request.build_opener() if follow else _noredir
    try:
        with opener.open(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read(400_000).decode("utf8", "replace"), r.geturl()
    except urllib.error.HTTPError as e:
        try:
            body = e.read(400_000).decode("utf8", "replace")
        except Exception:
            body = ""
        return e.code, dict(e.headers or {}), body, getattr(e, "url", url)
    except Exception as e:
        return None, {}, str(e), url


def clean_url(u):
    u = u.rstrip(".,;:!?*_'\"")
    while u.endswith(")") and u.count("(") < u.count(")"):
        u = u[:-1]
    return u


def host_of(u):
    try:
        return (urllib.parse.urlsplit(u).hostname or "").lower()
    except ValueError:
        return ""


def registrable(host):
    parts = host.split(".")
    if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in TWO_LEVEL:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def norm(u):
    """Loose form of a URL for 'same destination?' comparisons."""
    if not u:
        return ""
    s = urllib.parse.urlsplit(u.strip())
    h = (s.hostname or "").lower().removeprefix("www.")
    q = "&".join(p for p in s.query.split("&") if p and not p.startswith("utm_"))
    return f"{h}{s.path.rstrip('/')}" + (f"?{q}" if q else "")


def parse_date(s):
    for f in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%fZ", "%b %d %Y %H:%M UTC", "%Y%m%d%H%M%S"):
        try:
            d = datetime.strptime(s.strip(), f)
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    try:
        d = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def day(d):
    return d.strftime("%Y-%m-%d") if d else "?"


# ---------- the repo side ----------
def git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True,
                          errors="replace", check=True).stdout


def links_in_repo(repo):
    """Current links in tracked doc files -> {url: [files]}."""
    found = {}
    for f in git(repo, "ls-files", "-z", "--", *DOC_GLOBS).split("\0"):
        if not f:
            continue
        try:
            text = open(os.path.join(repo, f), encoding="utf8", errors="replace").read()
        except OSError:
            continue
        for m in URL_RE.finditer(text):
            u = clean_url(m.group(0))
            if host_of(u) and not SKIP_HOSTS.match(host_of(u)):
                found.setdefault(u, set()).add(f)
    return {u: sorted(fs) for u, fs in found.items()}


def first_seen(repo, urls):
    """One pass over the docs history: the commit date when each URL first appeared."""
    seen, date = {}, None
    p = subprocess.Popen(["git", "-C", repo, "log", "--reverse", "--no-color", "--format=@@%cI", "-p",
                          "--unified=0", "--no-ext-diff", "--", *DOC_GLOBS],
                         stdout=subprocess.PIPE, text=True, errors="replace")
    for line in p.stdout:
        if line.startswith("@@") and line[2:3].isdigit():
            date = parse_date(line[2:].strip())
        elif line.startswith("+") and not line.startswith("+++") and "http" in line:
            for m in URL_RE.finditer(line):
                u = clean_url(m.group(0))
                if u in urls and u not in seen:
                    seen[u] = date
    p.wait()
    return seen


def read_ignore(path):
    """One URL or domain per line; '#' starts a comment. Accepted findings stop failing CI."""
    if not path or not os.path.exists(path):
        return set()
    out = set()
    for line in open(path, encoding="utf8"):
        line = line.split("#", 1)[0].strip()
        if line:
            out.add(line.lower().rstrip("/"))
    return out


def ignored(url, ign):
    h = host_of(url)
    return url.lower().rstrip("/") in ign or h in ign or registrable(h) in ign


# ---------- the web side (cached) ----------
class Cache:
    """JSON file of answers with the time they were fetched. Answers older than max_age
    are fetched again, so a domain re-registered next month shows up in CI. 'unknown'
    answers (timeouts, throttling) are never stored."""

    def __init__(self, path, max_age_days=7):
        self.path, self.d, self.max_age = path, {}, max_age_days * 86400
        self.lock = threading.Lock()
        if path and os.path.exists(path):
            try:
                self.d = json.load(open(path))
            except ValueError:
                pass

    def get(self, k, fn):
        with self.lock:
            e = self.d.get(k)
            if isinstance(e, dict) and "at" in e and time.time() - e["at"] < self.max_age:
                return e["v"]
        v = fn()
        if not (isinstance(v, dict) and v.get("state") == "unknown") and v is not None or k.startswith("wb:"):
            with self.lock:
                self.d[k] = {"at": int(time.time()), "v": v}
        return v

    def save(self):
        if self.path:
            with self.lock:
                tmp = self.path + ".tmp"
                json.dump(self.d, open(tmp, "w"), indent=0)
                os.replace(tmp, self.path)


_TLDS = None
_BOOT = None
_host_locks, _host_last = {}, {}


def polite(host, gap):
    """At most one request every `gap` seconds to the same host, across threads."""
    lock = _host_locks.setdefault(host, threading.Lock())
    with lock:
        wait = _host_last.get(host, 0) + gap - time.time()
        if wait > 0:
            time.sleep(wait)
        _host_last[host] = time.time()


def tld_exists(domain):
    global _TLDS
    if _TLDS is None:
        st, _, body, _ = fetch("https://data.iana.org/TLD/tlds-alpha-by-domain.txt")
        _TLDS = {l.strip().lower() for l in body.splitlines() if l and not l.startswith("#")} if st == 200 else set()
    return not _TLDS or domain.rsplit(".", 1)[-1] in _TLDS


def rdap_base(domain):
    """The registry's own RDAP server for this TLD, from IANA's bootstrap file (None if it has none)."""
    global _BOOT
    if _BOOT is None:
        st, _, body, _ = fetch("https://data.iana.org/rdap/dns.json")
        try:
            _BOOT = {t: s[1][0] for s in json.loads(body)["services"] for t in s[0]} if st == 200 else {}
        except (ValueError, KeyError, IndexError):
            _BOOT = {}
    return _BOOT.get(domain.rsplit(".", 1)[-1])


def whois(domain):
    """For registries without RDAP, and to confirm a 'not registered'."""
    polite("whois:" + domain.rsplit(".", 1)[-1], 1.0)
    try:
        out = subprocess.run(["whois", domain], capture_output=True, text=True, errors="replace", timeout=25).stdout
    except (OSError, subprocess.TimeoutExpired):
        return {"state": "unknown"}
    # macOS whois prints IANA's record for the TLD first (with the TLD's own "created:" date),
    # then "# whois.<registry>" and the domain's record. Keep only what comes after IANA.
    parts = re.split(r"(?m)^# whois\.", out)
    out = "# whois.".join(parts[1:]) if len(parts) > 1 else ("" if "IANA" in out[:400] else out)
    low = out.lower()
    m = re.search(r"(?im)^\s*(creation date|created on|created|registered on|registration time|domain registration date)\s*:\s*(\S+)", out)
    if m and parse_date(m.group(2)[:20]):
        return {"state": "registered", "registered": m.group(2), "via": "whois"}
    if re.search(r"no match|not found|no data found|no entries found|status:\s*(free|available)|is available", low):
        return {"state": "unregistered", "via": "whois"}
    return {"state": "registered", "registered": None, "via": "whois"} if "domain" in low and len(out) > 300 else {"state": "unknown"}


def rdap(domain):
    if not tld_exists(domain):
        return {"state": "bad-tld"}
    base = rdap_base(domain)
    url = f"{base.rstrip('/')}/domain/{domain}" if base else f"https://rdap.org/domain/{domain}"
    st = None
    for wait in (0, 5, 15, 30):            # registries throttle too: back off and retry
        time.sleep(wait)
        polite(host_of(url), 0.3 if base else 0.5)
        st, _, body, _ = fetch(url)
        if st not in (429, 500, 502, 503, 504, None):
            break
    if st == 200:
        try:
            j = json.loads(body)
            ev = {e["eventAction"]: e["eventDate"] for e in j.get("events", [])}
            return {"state": "registered", "registered": ev.get("registration"), "expires": ev.get("expiration"),
                    "status": [s.lower() for s in j.get("status", [])], "via": "rdap"}
        except (ValueError, KeyError, TypeError):
            pass
    if st == 404 and base:                 # the registry itself says it does not exist: confirm with whois
        w = whois(domain)
        return w if w["state"] != "unknown" else {"state": "unregistered", "via": "rdap"}
    return whois(domain)                   # no RDAP, a 404 from rdap.org or still throttled: ask whois


def landing(domain):
    """What the domain's home page shows today: a parking / for-sale page or not."""
    polite(domain, 0.5)
    st, _, body, final = fetch(f"http://{domain}/", timeout=12)
    fh = host_of(final)
    if PARKING_HOSTS.search(fh) or PARKING_TEXT.search(final) or PARKING_TEXT.search(body[:60_000]):
        return {"parked": True, "final": final if fh != domain else None, "status": st}
    return {"parked": False, "status": st}


def wayback_first(short):
    """Earliest archived redirect of a short link -> (timestamp, destination)."""
    polite("web.archive.org", 0.6)
    q = urllib.parse.quote(short.split("://", 1)[-1], safe="")
    st, _, body, _ = fetch(f"https://web.archive.org/cdx/search/cdx?url={q}&output=json&limit=1&fl=timestamp&filter=statuscode:30.")
    try:
        rows = json.loads(body) if st == 200 and body.strip() else []
    except ValueError:
        rows = []
    if len(rows) < 2:
        return None
    ts = rows[1][0]
    polite("web.archive.org", 0.6)
    st, hd, _, _ = fetch(f"https://web.archive.org/web/{ts}id_/{short}", follow=False)
    loc = {k.lower(): v for k, v in hd.items()}.get("location", "")
    loc = re.sub(r"^https?://web\.archive\.org/web/\d+id_/", "", loc)
    return {"ts": ts, "dest": loc} if loc else None


def archived_copy(url, when):
    """An archived copy of the page from around the day the link was added, to replace a dead link."""
    polite("archive.org", 0.6)
    ts = when.strftime("%Y%m%d") if when else "2010"
    st, _, body, _ = fetch(f"https://archive.org/wayback/available?url={urllib.parse.quote(url, safe='')}&timestamp={ts}")
    try:
        snap = json.loads(body)["archived_snapshots"]["closest"]
        if not snap.get("available"):
            return None
        return {"url": snap["url"].replace("http://web.archive.org", "https://web.archive.org"), "ts": snap["timestamp"]}
    except (ValueError, KeyError, TypeError):
        return None


def short_now(short):
    polite(host_of(short), 0.4)
    st, hd, _, _ = fetch(short, follow=False)
    return {"status": st, "dest": {k.lower(): v for k, v in hd.items()}.get("location", "")}


def bitly_info(short):
    """bit.ly's public '+' page: creation date of the back-half as it exists today."""
    polite("bit.ly+", 0.6)
    st, _, body, _ = fetch(short.rstrip("/") + "+")
    if "eactivated" in body:
        return {"deactivated": True}
    m = re.search(r'created-date">\s*([^<]+)<', body)
    return {"created": m.group(1).strip() if m else None}


# ---------- verdicts ----------
def check(repo, cache_path=None, log=print, max_age_days=7, ignore_path=None, workers=8):
    urls = links_in_repo(repo)
    log(f"{len(urls)} links in the docs. Reading history...")
    seen = first_seen(repo, set(urls))
    cache = Cache(cache_path, max_age_days)
    ign = read_ignore(ignore_path if ignore_path else os.path.join(repo, ".oldlinks-ignore"))
    findings, skipped = [], 0

    domains = sorted({registrable(host_of(u)) for u in urls})
    log(f"{len(domains)} domains. Checking registration dates and what each one shows today...")
    done = [0]

    def one(d):
        cache.get(f"rdap:{d}", lambda: rdap(d))
        if d not in SHORTENERS:
            cache.get(f"land:{d}", lambda: landing(d))
        done[0] += 1
        if done[0] % 25 == 0:
            log(f"  {done[0]}/{len(domains)}")
            cache.save()

    with ThreadPoolExecutor(workers) as ex:     # many registries in parallel, each one asked politely
        list(ex.map(one, domains))
    cache.save()

    for u, files in sorted(urls.items()):
        if ignored(u, ign):
            skipped += 1
            continue
        h = host_of(u)
        added = seen.get(u)
        base = {"url": u, "files": files, "added": day(added)}
        dom = registrable(h)
        r = cache.get(f"rdap:{dom}", lambda: rdap(dom))
        reg = parse_date(r["registered"][:25]) if r.get("registered") else None
        exp = parse_date(r["expires"][:25]) if r.get("expires") else None
        land = cache.get(f"land:{dom}", lambda: landing(dom)) if dom not in SHORTENERS else {}
        n = len(findings)

        if r["state"] == "bad-tld":
            findings.append({**base, "level": "yellow", "kind": "domain-typo",
                             "why": f".{dom.rsplit('.', 1)[-1]} is not a real top-level domain: a typo, the link is broken"})
        elif r["state"] == "unregistered":
            findings.append({**base, "level": "red", "kind": "domain-free",
                             "why": f"{dom} is not registered: anyone can buy it and serve anything at this link"})
        elif reg and added and reg > added:
            findings.append({**base, "level": "red", "kind": "domain-reregistered",
                             "why": f"{dom} was registered on {day(reg)}, after this link was added ({day(added)}). The owner may have changed"
                                    + ("; today it shows a parking / for-sale page" if land.get("parked") else "")})
        elif land.get("parked"):
            findings.append({**base, "level": "red", "kind": "domain-parked",
                             "why": f"{dom} shows a parking / for-sale page today: whoever owns it now is not who you linked"})
        elif any("redemption" in s or "pending delete" in s for s in r.get("status", [])):
            findings.append({**base, "level": "yellow", "kind": "domain-expiring",
                             "why": f"{dom} has expired and is about to be released ({', '.join(r['status'])}): anyone will be able to buy it"})
        elif exp and exp < datetime.now(timezone.utc):
            findings.append({**base, "level": "yellow", "kind": "domain-expired",
                             "why": f"{dom} expired on {day(exp)}"})

        if h in SHORTENERS:
            if h in DEAD_SHORTENERS:
                findings.append({**base, "level": "yellow", "kind": "short-dead-service", "why": DEAD_SHORTENERS[h]})
                continue
            now = cache.get(f"now:{u}", lambda: short_now(u))
            past = cache.get(f"wb:{u}", lambda: wayback_first(u))
            info = cache.get(f"bitly:{u}", lambda: bitly_info(u)) if h in ("bit.ly", "bitly.com", "j.mp") else {}
            created = parse_date(info["created"]) if info.get("created") else None
            extra = {"now": now.get("dest"), "then": past and past["dest"],
                     "then_date": past and day(parse_date(past["ts"])), "created": day(created) if created else None}
            if info.get("deactivated") or now.get("status") == 410:
                findings.append({**base, **extra, "level": "yellow", "kind": "short-deactivated",
                                 "why": "the short link was deactivated by the service (it shows a warning page)"})
            elif now.get("status") in (404, None) or not now.get("dest"):
                findings.append({**base, **extra, "level": "yellow", "kind": "short-broken",
                                 "why": f"the short link does not redirect any more (HTTP {now.get('status')})"})
            elif past and norm(past["dest"]) != norm(now["dest"]):
                findings.append({**base, **extra, "level": "red", "kind": "short-changed",
                                 "why": f"it pointed to {past['dest']} in {extra['then_date']} and now points to {now['dest']}"
                                        + (f" (back-half created {day(created)})" if created else "")})
            elif created and added and created > added:
                findings.append({**base, **extra, "level": "yellow", "kind": "short-recreated",
                                 "why": f"bit.ly says this back-half was created on {day(created)}, after you added it ({day(added)}); "
                                        + ("it still points to the same place" if past else "no archived copy to compare the destination with")})
            elif not past:
                findings.append({**base, **extra, "level": "grey", "kind": "short-unverified",
                                 "why": f"points to {now['dest']}; no archived copy to compare with. Replace short links with the real URL"})

        for f in findings[n:]:                  # suggest a replacement: what the link showed before it changed
            if f["kind"] == "short-changed":
                f["fix"] = f["then"]
            elif f["level"] in ("red", "yellow") and f["kind"] not in ("domain-typo", "short-dead-service"):
                snap = cache.get(f"arch:{u}", lambda: archived_copy(u, added))
                took = parse_date(snap["ts"]) if snap else None
                if took and not (reg and took >= reg):   # a copy from after the re-registration shows the new owner
                    f["fix"] = snap["url"]
    cache.save()
    unknown = sorted(d for d in domains if cache.get(f"rdap:{d}", lambda: {"state": "unknown"})["state"] == "unknown")
    return {"repo": os.path.abspath(repo), "version": VERSION, "links": len(urls), "domains": len(domains),
            "ignored": skipped, "unknown_domains": unknown, "findings": findings,
            "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}


# ---------- output ----------
ICON = {"red": "RED   ", "yellow": "YELLOW", "grey": "INFO  "}
ORDER = {"red": 0, "yellow": 1, "grey": 2}


def print_report(rep):
    fs = sorted(rep["findings"], key=lambda f: (ORDER[f["level"]], f["url"]))
    print(f"\noldlinks {rep['version']} · {rep['repo']} · {rep['links']} links, {rep['domains']} domains\n")
    for f in fs:
        print(f"{ICON[f['level']]} {f['url']}\n       {f['why']}\n       in {', '.join(f['files'][:3])} · added {f['added']}")
        if f.get("fix"):
            print(f"       replace with: {f['fix']}")
    n = {k: sum(f["level"] == k for f in fs) for k in ORDER}
    print(f"\n{n['red']} red · {n['yellow']} yellow · {n['grey']} info"
          + (f" · {rep['ignored']} ignored" if rep.get("ignored") else ""))
    if rep["unknown_domains"]:
        print(f"Not checked (no answer from the registry): {', '.join(rep['unknown_domains'][:15])}"
              + (" ..." if len(rep["unknown_domains"]) > 15 else ""))
    if not fs:
        print("Nothing suspicious found. That is not a guarantee: a domain can change hands without being re-registered.")


def html_report(rep):
    e = html.escape
    rows = "".join(
        f"<tr class='{f['level']}'><td>{e(f['level'])}</td><td><code>{e(f['url'])}</code><div>{e(f['why'])}</div>"
        + (f"<div>replace with: <a href='{e(f['fix'])}'>{e(f['fix'])}</a></div>" if f.get("fix") else "")
        + f"</td><td>{e(', '.join(f['files'][:3]))}</td><td>{e(f['added'])}</td></tr>"
        for f in sorted(rep["findings"], key=lambda f: (ORDER[f["level"]], f["url"])))
    return f"""<!doctype html><meta charset=utf-8><title>oldlinks report</title>
<style>body{{font:15px/1.5 system-ui;margin:2rem auto;max-width:1000px;padding:0 1rem}}td{{padding:.5rem;vertical-align:top;border-top:1px solid #ddd}}
code,a{{word-break:break-all}}td div{{color:#555}}.red td:first-child{{color:#c00;font-weight:600}}.yellow td:first-child{{color:#a60}}</style>
<h1>oldlinks</h1><p>{e(rep['repo'])} · {rep['links']} links · {rep['domains']} domains · {e(rep['checked_at'])}</p>
<table>{rows or '<tr><td>Nothing suspicious found.</td></tr>'}</table>"""


def main():
    ap = argparse.ArgumentParser(description="Are the old links in your repo still yours?")
    ap.add_argument("repo", nargs="?", default=".")
    ap.add_argument("--json"), ap.add_argument("--html"), ap.add_argument("--cache")
    ap.add_argument("--max-age-days", type=float, default=7, help="re-check cached answers older than this (default 7)")
    ap.add_argument("--ignore", help="file of accepted URLs or domains (default: .oldlinks-ignore in the repo)")
    ap.add_argument("--version", action="version", version=f"oldlinks {VERSION}")
    a = ap.parse_args()
    rep = check(a.repo, a.cache, log=lambda m: print(m, file=sys.stderr), max_age_days=a.max_age_days, ignore_path=a.ignore)
    print_report(rep)
    if a.json:
        json.dump(rep, open(a.json, "w"), indent=1, ensure_ascii=False)
    if a.html:
        open(a.html, "w").write(html_report(rep))
    sys.exit(1 if any(f["level"] == "red" for f in rep["findings"]) else 0)


if __name__ == "__main__":
    main()
