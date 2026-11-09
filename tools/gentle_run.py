import fcntl
import glob
import json
import os
import signal
import sys
import time
import urllib.request
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

SKILL = str(Path(__file__).resolve().parent.parent)
VAULT = "/home/schneiderr/Documents/youtube-subscription-metadata"
SKILL_SCRIPTS = os.path.join(SKILL, "scripts")
PROXY_LIST_FILE = os.environ.get("PROXY_LIST_FILE", os.path.join(VAULT, "tools", "proxies.txt"))
# Single-instance guard (added 2026-11-03: three runners ran in parallel after
# pattern-based kills silently missed their target — parallel runners double
# the proxy burn and write duplicate index rows). flock is released by the
# kernel on ANY exit (crash, reboot included), so there is no stale-lock case
# to clean up. The lock file also holds the live PID: restart with
# `kill $(cat tools/gentle_run.lock)` instead of pattern matching.
LOCK_FILE = os.path.join(VAULT, "tools", "gentle_run.lock")


def acquire_single_instance_guard():
    """Exit(4) if another gentle_run already holds the lock. Called at import
    time — before the slow module imports below — so two near-simultaneous
    starts can't both slip in. The handle must stay referenced for the process
    lifetime; dropping it releases the lock."""
    fh = open(LOCK_FILE, "a+")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.seek(0)
        print(json.dumps({"stage": "abort",
                          "note": "another gentle_run holds the lock — refusing to run in parallel",
                          "lock": LOCK_FILE, "holder_pid": fh.read().strip() or "?"}))
        sys.exit(4)
    fh.truncate(0)
    fh.write(str(os.getpid()))
    fh.flush()
    return fh


# Keep the handle in a module global for the process lifetime.
_guard_fh = acquire_single_instance_guard()
# Semicolon-separated proxy-list APIs, tried in listed order (tier order).
# Tier 1: latency < 1000ms — smaller but fast pool. Tier 2: no latency
# filter — ~9x more candidates; once tier 1 burns out there are still live
# proxies to find (2026-09-13: strict-only pool degraded over ~2 days,
# throughput fell 112/h -> 38/h).
PROXY_API_URL = os.environ.get(
    "PROXY_API_URL",
    "https://api.proxyscrape.com/v4/free-proxy-list/get?request=display_proxies"
    "&proxy_format=protocolipport&format=json&timeout=1000;"
    "https://api.proxyscrape.com/v4/free-proxy-list/get?request=display_proxies"
    "&proxy_format=protocolipport&format=json",
)

# Geonix free-proxy API (https://free.geonix.com) as an additional pool tier
# (added 2026-11-02, after the proxyscrape-only pool proved mostly dead).
# API: POST /api/front/main/proxy/list {"country": "<full name>", "count": N}
# -> [{"ip": "ip:port", "country": "..."}]. The response carries NO protocol
# field, so every candidate is validated as a plain HTTP proxy (concurrently,
# with a hard timeout) before entering the pool. Set GEONIX_ENABLED=0 to skip.
GEONIX_ENABLED = os.environ.get("GEONIX_ENABLED", "1") != "0"
GEONIX_URL = "https://free.geonix.com/api/front/main/proxy/list"
GEONIX_COUNTRIES = [c.strip() for c in os.environ.get(
    "GEONIX_COUNTRIES",
    "United States,Germany,Netherlands,France,United Kingdom,Poland,Ukraine,Brazil",
).split(",") if c.strip()]
GEONIX_COUNT = int(os.environ.get("GEONIX_COUNT", "50"))       # per country
GEONIX_VALIDATE_MAX = int(os.environ.get("GEONIX_VALIDATE_MAX", "200"))
GEONIX_VALIDATE_TIMEOUT_S = float(os.environ.get("GEONIX_VALIDATE_TIMEOUT_S", "5"))

# Plain-text proxy lists (one proxy per line), refetched on every pool
# refresh. Added 2026-11-02: iplocate/free-proxy-list re-validates its files
# every 30 min and passed 18/40 spot checks vs Geonix 5/40 and a mostly-dead
# proxyscrape pool — ranked ahead of both. Semicolon-separated.
PROXY_TEXT_URLS = os.environ.get(
    "PROXY_TEXT_URLS",
    "https://raw.githubusercontent.com/iplocate/free-proxy-list/main/protocols/http.txt",
)

# Parallel pool pre-validation (added 2026-11-03). Before a fresh pool enters
# rotation, every candidate gets one liveness request through it (TLS tunnel
# included) — concurrently, so 1200 candidates cost ~4 min instead of the
# 8-60s dead-proxy penalty each incurs when discovered during fetching.
# Without this, a ~50%-dead pool spends most wall time on proxies that were
# never going to answer. Set POOL_VALIDATE=0 to skip.
POOL_VALIDATE = os.environ.get("POOL_VALIDATE", "1") != "0"
POOL_VALIDATE_WORKERS = int(os.environ.get("POOL_VALIDATE_WORKERS", "32"))
POOL_VALIDATE_TIMEOUT_S = float(os.environ.get("POOL_VALIDATE_TIMEOUT_S", "6"))

# Time-based pool refresh (added 2026-11-03): the exhaustion-triggered refill
# alone can sit on a slowly-poisoned pool for hours while iplocate keeps
# publishing fresher lists every 30 min. The main loop now also re-pulls the
# sources on this interval; refresh_pool's signature check makes a no-op
# (no validation cost) when the sources haven't changed.
POOL_REFRESH_S = float(os.environ.get("POOL_REFRESH_S", "1800"))

# Respect YouTube's rate limits: gentle base throttle, no rapid per-video
# retry hammering. Must be set BEFORE importing fetch_transcript (read at import).
os.environ.setdefault("YT_THROTTLE_SECONDS", "8")
os.environ.setdefault("YT_MAX_RETRIES", "1")
# Persistent cache (survives reboots) instead of the default /tmp.
os.environ.setdefault("YT_TRANSCRIPT_CACHE_DIR", os.path.join(VAULT, ".cache", "yt_transcripts"))

sys.path.insert(0, SKILL_SCRIPTS)
import youtube_subscriptions as ys

ys.apply_config()
from fetch_transcript import fetch_transcript as fetch_one

# Target scope: empty / "all" = every pending video; otherwise a comma-separated
# list of exact channel folder names.
_target = os.environ.get("TARGET_CHANNELS", "").strip()
CHANNELS = None if _target in ("", "all") else {c.strip() for c in _target.split(",") if c.strip()}

# Unattended-run tolerance.
BLOCK_STREAK_TO_HALT = 2        # consecutive unpoolable errors before a long wait
INITIAL_BACKOFF_S = 15 * 60     # first long wait
MAX_BACKOFF_S = 45 * 60         # cap on a single wait
SAME_POOL_COOLDOWN_S = 10 * 60  # cooldown before reusing an identical proxy list
MAX_LONG_BACKOFFS = int(os.environ.get("MAX_LONG_BACKOFFS", "40"))
MAX_TOTAL_HOURS = float(os.environ.get("MAX_TOTAL_HOURS", "240"))  # watchdog

# Error kinds that mean "this exit IP is blocked/dead" -> poison the proxy
# and rotate, rather than hammering the same one. ParseError: a free proxy
# often returns an intercept/error page instead of the YouTube response —
# the library fails to parse it and surfaces the class name. A genuinely
# unparseable video is caught by the direct fallback once the pool is
# exhausted (2026-09-18: probe wedged in blocked-wait loops on ParseError
# because it wasn't poisoned). requests-transport errors
# (ChunkedEncodingError/ContentDecodingError/TooManyRedirects/InvalidURL)
# are the same family — a corrupting proxy, not a YouTube/exit-IP problem
# (2026-09-19: probe burned the full 2h budget re-hitting one
# ChunkedEncodingError proxy). The _FetchHardTimeout
# variant (leading underscore) is how the hard cap surfaces when it fires
# INSIDE fetch_one: the signal handler raises there, fetch_one's generic
# `except Exception` swallows it and returns kind=type(e).__name__ — which
# is the class name WITH the underscore. Both spellings must poison,
# otherwise a hung proxy is never rotated off (observed 2026-09-14: probe
# looped forever on one 60s-hanging proxy).
POISON_KINDS = {"IpBlocked", "RequestBlocked", "ProxyError", "ConnectionError",
                "Timeout", "ConnectTimeout", "ReadTimeout", "SSLError", "InvalidSchema",
                "ParseError", "ChunkedEncodingError", "ContentDecodingError",
                "TooManyRedirects", "InvalidURL", "FetchHardTimeout", "_FetchHardTimeout"}

# Hard wall-clock cap on a single fetch_one() call, in seconds. The library's
# per-READ timeout (30s) is defeated by a black-hole proxy that holds an
# ESTABLISHED socket open with keepalive dribble — seen live 2026-09-10: one
# such proxy wedged the run for ~26h. SIGALRM imposes an absolute deadline the
# proxy cannot dodge. Normal fetch is ~8s throttle + a few seconds; 120s is a
# generous ceiling that bounds any single bad proxy at 2 min.
PER_FETCH_HARD_S = float(os.environ.get("PER_FETCH_HARD_S", "60"))

# Max proxies per pool draw. NOT "bigger is better": in a bad window where an
# entire list is dead/blocked, one video burns the whole pool before a
# refresh — 2084 proxies = 3.7h burned per video (observed 2026-09-13, pace
# fell 112/h -> 11/h). A 600 cap bounds a full bad burn at ~65 min while
# still offering plenty of diversity; refreshes happen often, so fresh IPs
# keep coming.
POOL_MAX_SIZE = int(os.environ.get("POOL_MAX_SIZE", "1200"))
# Every N-th poisoned proxy, also try the bare exit IP once (2026-09-19:
# the full-pool burn on a mostly-dead 600-proxy list takes ~1.5h; the
# direct IP works occasionally even while nominally blocked, so finding
# that out early is worth one attempt per 10 poisons at ~10s cost).
DIRECT_PROBE_EVERY = 10


class _FetchHardTimeout(Exception):
    pass


def _on_alarm(signum, frame):
    raise _FetchHardTimeout()


def _fetch_one_hard(vid, url):
    """fetch_one() under an absolute wall-clock cap. Runs in the main thread
    (the runner spawns no threads), so a SIGALRM handler can raise into the
    blocking socket read and unwind it. Timer is always cleared in finally."""
    prev = signal.signal(signal.SIGALRM, _on_alarm)
    signal.setitimer(signal.ITIMER_REAL, PER_FETCH_HARD_S)
    try:
        return fetch_one(vid, proxy_url=url, max_retries=1)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, prev)


def log(msg):
    print(json.dumps(msg, ensure_ascii=False, default=str), flush=True)


def host_of(url):
    if not url:
        return "direct"
    p = urlparse(url)
    return f"{p.hostname}:{p.port}" if p.hostname else "direct"


def load_static_proxies():
    if not os.path.exists(PROXY_LIST_FILE):
        return []
    out = []
    with open(PROXY_LIST_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                out.append(line)
    return out


def fetch_text_list_proxies():
    """Plain-text lists: one proxy per line, `ip:port` or `scheme://ip:port`.
    Bare ip:port gets http:// prepended — requests (and GenericProxyConfig)
    need a scheme."""
    out = []
    for url in PROXY_TEXT_URLS.split(";"):
        url = url.strip()
        if not url:
            continue
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                text = resp.read().decode("utf-8", "replace")
        except Exception as e:
            log({"stage": "text-list-error", "url": url[:80], "error": str(e)[:120]})
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            out.append(line if "://" in line else "http://" + line)
    return out


def _geonix_candidates():
    """ip:port candidates from the Geonix API, one POST per configured
    country (the API requires a full country name; empty -> HTTP 500)."""
    out = []
    for country in GEONIX_COUNTRIES:
        body = json.dumps({"country": country, "count": GEONIX_COUNT}).encode()
        req = urllib.request.Request(
            GEONIX_URL, data=body,
            headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                items = json.load(resp)
        except Exception as e:
            log({"stage": "geonix-api-error", "country": country, "error": str(e)[:120]})
            continue
        if isinstance(items, list):
            out.extend(i["ip"] for i in items
                       if isinstance(i, dict) and i.get("ip") and ":" in i["ip"])
    return out


def _validate_http_proxy(ipport):
    """Geonix doesn't say which protocol an ip:port speaks — prove it works
    as a plain HTTP proxy by fetching through it. Returns scheme'd URL or None."""
    url = "http://" + ipport
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": url, "https": url}))
    try:
        req = urllib.request.Request("http://example.com", headers={"User-Agent": "Mozilla/5.0"})
        with opener.open(req, timeout=GEONIX_VALIDATE_TIMEOUT_S) as resp:
            if 200 <= resp.status < 400:
                return url
    except Exception:
        pass
    return None


def fetch_geonix_proxies():
    """Geonix tier: fetch candidates, validate concurrently (sequential
    validation of 200+ candidates at 5s each would stall pool refresh)."""
    seen, cands = set(), []
    for ipport in _geonix_candidates():
        if ipport not in seen:
            seen.add(ipport)
            cands.append(ipport)
    cands = cands[:GEONIX_VALIDATE_MAX]
    with ThreadPoolExecutor(max_workers=32) as ex:
        good = [u for u in ex.map(_validate_http_proxy, cands) if u]
    log({"stage": "geonix", "candidates": len(cands), "validated_http": len(good)})
    return good


def _proxy_alive(proxy_url):
    """One liveness request through a proxy (any scheme requests understands:
    http/https/socks4/socks5 — PySocks is installed). Validated against
    YouTube's own oembed endpoint, not a generic site: what decides pool
    quality is whether YouTube lets this exit IP through — proxies that
    happily tunnel example.com yet get RequestBlocked on YouTube clogged the
    pool (observed 2026-11-03). oembed is lightweight, and every check exits
    via the proxy's IP, so our own IP is never in the firing line."""
    import requests
    try:
        r = requests.get("https://www.youtube.com/oembed",
                         params={"url": "https://www.youtube.com/watch?v=jNQXAC9IVRw",
                                 "format": "json"},
                         proxies={"http": proxy_url, "https": proxy_url},
                         timeout=POOL_VALIDATE_TIMEOUT_S)
        return r.status_code == 200
    except Exception:
        return False


def validate_pool_parallel(proxies):
    """Filter a fresh pool down to live proxies, concurrently."""
    if not POOL_VALIDATE:
        return proxies
    cands = [p for p in proxies if p]
    with ThreadPoolExecutor(max_workers=POOL_VALIDATE_WORKERS) as ex:
        alive = [p for p, ok in zip(cands, ex.map(_proxy_alive, cands)) if ok]
    log({"stage": "pool-validate", "candidates": len(cands), "alive": len(alive)})
    return alive


def fetch_api_proxies():
    """Pull free-proxy lists. Tier order = quality order (spot-checked
    2026-11-02): iplocate text lists (~45% live, re-validated every 30 min)
    first, then proxyscrape JSON tiers (PROXY_API_URL, mostly dead lately),
    then Geonix (~12% live after HTTP validation). A proxy seen in an earlier
    tier wins dedupe. Within the proxyscrape tier, non-hosting (residential)
    proxies rank first — they survive longer before YouTube flags them."""
    ranked, seen = [], set()
    for u in fetch_text_list_proxies():
        if u not in seen:
            seen.add(u)
            ranked.append(u)
    for url in PROXY_API_URL.split(";"):
        url = url.strip()
        if not url:
            continue
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                d = json.load(resp)
        except Exception as e:
            log({"stage": "proxy-api-error", "error": str(e)[:160]})
            continue
        tier = []
        for i, p in enumerate(d.get("proxies", [])):
            if p.get("protocol") not in ("http", "socks5", "socks4"):
                continue
            u = p.get("proxy")
            if not u or u in seen:
                continue
            seen.add(u)
            residential = 1 if not p.get("ip_data", {}).get("hosting") else 0
            tier.append((residential, i, u))
        tier.sort(key=lambda t: (-t[0], t[1]))
        ranked.extend(u for _, _, u in tier)
    if GEONIX_ENABLED:
        try:
            for u in fetch_geonix_proxies():
                if u not in seen:
                    seen.add(u)
                    ranked.append(u)
        except Exception as e:
            log({"stage": "geonix-error", "error": str(e)[:160]})
    return ranked[:POOL_MAX_SIZE]


class ProxyPool:
    """Round-robin proxy list with per-proxy poisoning. Each entry is one
    proxy URL (direct connection when the pool is empty)."""

    def __init__(self, proxies):
        self.set_proxies(proxies)

    def set_proxies(self, proxies):
        self.proxies = proxies if proxies else [None]
        self.idx = 0
        self.poisoned = set()

    def current(self):
        return self.proxies[self.idx]

    def poison(self, url):
        self.poisoned.add(url)

    def advance(self):
        n = len(self.proxies)
        for _ in range(n):
            self.idx = (self.idx + 1) % n
            if self.proxies[self.idx] not in self.poisoned:
                return
        self.poisoned.clear()
        self.idx = 0

    def all_poisoned(self):
        return len(self.poisoned) >= len(self.proxies)


_last_pool_sig = None
_last_pool_refresh = 0.0


def refresh_pool(pool):
    """Refill the pool. Static file wins over the API. Returns (count, source).
    Also stamps _last_pool_refresh (drives the scheduled refresh in main)."""
    global _last_pool_sig, _last_pool_refresh
    _last_pool_refresh = time.time()
    static = load_static_proxies()
    src_list, source = (static, "static") if static else (fetch_api_proxies(), "api")
    if not src_list:
        return 0, "none"
    sig = tuple(src_list)
    if sig == _last_pool_sig:
        return 0, "unchanged"
    _last_pool_sig = sig
    src_list = validate_pool_parallel(src_list)
    if not src_list:
        return 0, source
    pool.set_proxies(src_list)
    return len(src_list), source


POOL = ProxyPool([None])


def fetch_with_pool(vid):
    """Fetch via the pool: 1 attempt per proxy, rotate on block/dead proxy,
    refill from the API when the whole pool is poisoned."""
    long_waits = 0
    tried_direct = False
    while True:
        url = POOL.current()
        try:
            r = _fetch_one_hard(vid, url)
        except _FetchHardTimeout:
            r = {"status": "error", "kind": "FetchHardTimeout", "video_id": vid}
        if r["status"] in ("ok", "cached", "unavailable"):
            return r
        if r.get("kind") in POISON_KINDS or r.get("kind") == "FetchHardTimeout":
            POOL.poison(url)
            if not POOL.all_poisoned():
                POOL.advance()
                log({"stage": "proxy-poisoned", "proxy": host_of(url), "kind": r.get("kind"),
                     "next": host_of(POOL.current())})
                continue
            # Entire pool poisoned -> try a fresh list from the API.
            n, source = refresh_pool(POOL)
            if n:
                log({"stage": "pool-refreshed", "proxies": n, "source": source})
                continue
            # No fresh list (identical list, or API down/empty). Before
            # waiting: one DIRECT attempt on the bare exit IP — even while
            # blocked it squeezes a few videos through (observed ~2/45min
            # pre-proxy era), at zero proxy-burn cost. Once per exhaustion.
            if not tried_direct:
                tried_direct = True
                try:
                    r = _fetch_one_hard(vid, None)
                except _FetchHardTimeout:
                    r = {"status": "error", "kind": "FetchHardTimeout", "video_id": vid}
                if r["status"] in ("ok", "cached", "unavailable"):
                    return r
                log({"stage": "direct-fallback", "kind": r.get("kind")})
            long_waits += 1
            if long_waits > MAX_LONG_BACKOFFS:
                log({"stage": "abort", "note": "proxy pools exhausted — resume later (progress saved)"})
                return {"status": "error", "kind": "PoolExhausted", "video_id": vid}
            wait_s = min(INITIAL_BACKOFF_S * (1.5 ** (long_waits - 1)), MAX_BACKOFF_S)
            log({"stage": "pool-cooldown" if source == "unchanged" else "proxy-api-wait",
                 "wait_s": round(wait_s), "wait_no": long_waits})
            time.sleep(wait_s)
            continue
        return r  # non-block error -> let caller handle


def pending_videos():
    out = []
    for raw_path, fields in ys.find_raw_files_needing_transcript(limit=None):
        if CHANNELS is not None:
            top = os.path.relpath(raw_path, ys.RAW_DIR).split(os.sep)[0]
            if top not in CHANNELS:
                continue
        out.append((raw_path, fields))
    return out


def cached_ids():
    d = os.environ.get("YT_TRANSCRIPT_CACHE_DIR", "")
    return {os.path.basename(p)[3:-4] for p in glob.glob(os.path.join(d, "yt_*.txt"))}


_probe_idx = 0


def probe():
    """Single-request check: is the current exit path able to fetch? Uses an
    UNCACHED video so it really hits the network (a cache hit proves nothing).
    Rotates through pending videos each call (2026-09-19: one persistently
    failing video wedged the whole probe loop in blocked-waits for 2h)."""
    global _probe_idx
    pending = [(p, f) for p, f in pending_videos() if f["video_id"] not in cached_ids()]
    if not pending:
        return True  # nothing pending -> nothing to do
    _probe_idx = (_probe_idx + 1) % len(pending)
    r = fetch_with_pool(pending[_probe_idx][1]["video_id"])
    log({"stage": "probe", "video": pending[_probe_idx][1]["video_id"], "status": r["status"],
         "kind": r.get("kind", ""), "via": host_of(POOL.current())})
    return r["status"] in ("ok", "cached", "unavailable")


def process_one(raw_path, fields, fetched_at):
    vid = fields["video_id"]
    r = fetch_with_pool(vid)
    st = r["status"]
    if st in ("ok", "cached"):
        sidecar = ys.transcript_sidecar_path(raw_path)
        with open(sidecar, "w", encoding="utf-8") as f:
            f.write(r["text"])
        ys.write_transcript_into_raw_file(raw_path, r["text"])
        ys.append_transcript_index_rows([{"video_id": vid, "status": st, "path": sidecar,
                                          "word_count": r["word_count"], "fetched": fetched_at}])
        return st
    elif st == "unavailable":
        ys.write_transcript_into_raw_file(raw_path, ys.TRANSCRIPT_UNAVAILABLE)
        ys.append_transcript_index_rows([{"video_id": vid, "status": "unavailable", "path": "",
                                          "word_count": 0, "fetched": fetched_at}])
        return st
    return st  # error -> not indexed, retried later


def main():
    t_start = time.time()
    scope = "all channels" if CHANNELS is None else ", ".join(sorted(CHANNELS))
    n, source = refresh_pool(POOL)
    log({"stage": "init", "scope": scope, "throttle_s": os.environ["YT_THROTTLE_SECONDS"],
         "proxies": n, "proxy_source": source, "max_long_backoffs": MAX_LONG_BACKOFFS,
         "max_total_hours": MAX_TOTAL_HOURS})

    def over_total():
        return (time.time() - t_start) > MAX_TOTAL_HOURS * 3600

    # Phase 1: wait until the current exit path can fetch at all.
    waits = 0
    while not probe():
        if over_total():
            log({"stage": "abort", "note": "total time budget exceeded during probe waits"})
            return 3
        waits += 1
        if waits > MAX_LONG_BACKOFFS:
            log({"stage": "abort", "note": "blocked for too long during probe — add proxies or try later"})
            return 1
        wait_s = min(INITIAL_BACKOFF_S * (1.5 ** (waits - 1)), MAX_BACKOFF_S)
        log({"stage": "blocked-wait", "wait_s": round(wait_s), "wait_no": waits})
        time.sleep(wait_s)

    # Phase 2: process everything pending.
    fetched = cached = unavailable = errors = 0
    consec = 0
    long_backoffs = 0
    t0 = time.time()

    selected = pending_videos()
    log({"stage": "start", "pending": len(selected)})
    i = 0
    while i < len(selected):
        if time.time() - _last_pool_refresh >= POOL_REFRESH_S:
            # Scheduled top-up: sources publish fresher lists than a slowly-
            # poisoned pool's lifetime. No-op (cheap sig check, no validation)
            # when the sources haven't changed since the last pull.
            n, source = refresh_pool(POOL)
            if n:
                log({"stage": "pool-scheduled-refresh", "proxies": n, "source": source})
        if over_total():
            log({"stage": "abort", "note": "total time budget exceeded — resume later (progress saved)",
                 "done": i, "of": len(selected)})
            return 3
        raw_path, fields = selected[i]
        fetched_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        st = process_one(raw_path, fields, fetched_at)
        if st in ("ok", "cached", "unavailable"):
            if st == "ok":
                fetched += 1
            elif st == "cached":
                cached += 1
            else:
                unavailable += 1
            consec = 0
            i += 1
            if i % 25 == 0 or i == len(selected):
                log({"stage": "progress", "done": i, "of": len(selected),
                     "fetched": fetched, "cached": cached, "unavailable": unavailable,
                     "errors": errors, "via": host_of(POOL.current()),
                     "elapsed_min": round((time.time() - t0) / 60, 1)})
        else:
            errors += 1
            consec += 1
            if consec >= BLOCK_STREAK_TO_HALT:
                long_backoffs += 1
                if long_backoffs > MAX_LONG_BACKOFFS:
                    log({"stage": "abort", "note": "repeated errors — give up, resume later (progress saved)"})
                    return 2
                wait_s = min(INITIAL_BACKOFF_S * (1.5 ** (long_backoffs - 1)), MAX_BACKOFF_S)
                log({"stage": "error-halt", "wait_s": round(wait_s), "backoff_no": long_backoffs})
                time.sleep(wait_s)
                consec = 0
                selected = pending_videos()  # recompute: drops indexed, keeps errored
                i = 0
                log({"stage": "resume", "remaining": len(selected)})

    log({"stage": "done", "fetched": fetched, "cached": cached, "unavailable": unavailable,
         "errors_last_run": errors, "elapsed_min": round((time.time() - t0) / 60, 1)})
    return 0


if __name__ == "__main__":
    sys.exit(main())
