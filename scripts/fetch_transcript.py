#!/usr/bin/env python3
"""
fetch_transcript.py — single-video YouTube transcript fetch, cached and
throttled. Built from the spec Jeffrey brought back from Ryan Cunningham
(AI Automation Society), 2026-08-14 — see decisions/log.md.

Pulls YouTube's own caption track directly via the `youtube_transcript_api`
package (v1.x) — no audio download, no Whisper/transcription involved. A
straight caption scrape: if a video has captions disabled or is region-blocked,
there is no fallback to audio transcription — this just reports "unavailable".
Language-agnostic: fetches the video's NATIVE language track — the
auto-generated (asr) track identifies the spoken language, and a manually
created track in that language is preferred over generated (2026-09-07 —
previously English-only, which misreported non-English videos as unavailable).

## Usage (standalone CLI)
    python3 fetch_transcript.py <video-id-or-URL> [--print]

`--print` also dumps the transcript text to stdout. Without it, only the
RESULT line prints — for a long transcript, read the cache file instead of
stuffing 10K+ tokens into one tool call.

## Usage (importable)
    from fetch_transcript import fetch_transcript
    result = fetch_transcript("dQw4w9WgXcQ")
    # {"status": "ok"|"cached"|"unavailable"|"error", "video_id": ...,
    #  "text": ... (status in ok/cached), "word_count": ..., "path": ...,
    #  "kind": ... (status in unavailable/error), "message": ...}

## Env vars (optional, set in .env — same file every other script here uses)
    YT_THROTTLE_SECONDS     Seconds to sleep before each request. Default 1.5.
    YT_MAX_RETRIES          Retries on IpBlocked/RequestBlocked. Default 3.
    YT_TRANSCRIPT_CACHE_DIR Override the cache directory. Default: C:\\tmp on
                             Windows (matches the original tool exactly —
                             other AI Automation Society scripts may share
                             that cache location), a temp subfolder
                             elsewhere (Windows-only literal path isn't
                             portable to the public repo copy of this
                             skill — see SKILL.md's port note).
    YT_WEBSHARE_USER        Webshare residential proxy credentials — if set
    YT_WEBSHARE_PASS        (with YT_WEBSHARE_PASS), routes requests through
                              Webshare to dodge IP blocks. Off by default.
    YT_PROXY_HTTP           Generic HTTP/HTTPS proxy URLs, alternative to
    YT_PROXY_HTTPS          Webshare — only used if the Webshare pair above
                              isn't set.
    YT_TRANSCRIPT_LANGUAGES Comma-separated allowed languages (YouTube
                              language codes, e.g. "de,en"). If the video's
                              native language is in the list it is fetched;
                              otherwise the preferred language is used.
                              Unset = no restriction (always native).
    YT_TRANSCRIPT_LANGUAGE_PREFERRED
                              Fallback language when the native one isn't in
                              YT_TRANSCRIPT_LANGUAGES. Should itself be in
                              the allowed list.

Deliberately does NOT do chapter-based slicing for long videos or bulk
concurrent fetch (ThreadPoolExecutor) — that's Phase 2, once this core path
is proven against real videos. See decisions/log.md, 2026-08-14.
"""
import argparse
import os
import re
import sys
import time

# Windows' console defaults to cp1252, which can't encode a lot of real
# caption text (music notes, curly quotes, non-English names). Force UTF-8
# on stdout so --print doesn't crash on ordinary transcript content.
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
except ImportError:
    env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
    if os.path.exists(env_path):
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())

try:
    from youtube_transcript_api import (
        YouTubeTranscriptApi,
        IpBlocked,
        RequestBlocked,
        TranscriptsDisabled,
        NoTranscriptFound,
        VideoUnavailable,
        VideoUnplayable,
        AgeRestricted,
        InvalidVideoId,
    )
    from youtube_transcript_api.proxies import WebshareProxyConfig, GenericProxyConfig
    import requests
    from requests import Session
except ImportError:
    print("RESULT: error | kind=MissingDependency | Run: pip install youtube-transcript-api")
    sys.exit(1)

# The library's default Session() issues requests with NO timeout — one dead
# connection hangs the whole run forever (seen in the wild, 2026-09-07: a
# run of 277 videos stalled indefinitely at #26). Inject connect/read
# timeouts so a hung socket surfaces as a retryable error instead.
# Connect is 6s (not 10s) because the proxy-pool runner burns through many
# dead free proxies; every second shaved off a dead connect multiplies
# across hundreds of them (2026-09-13).
REQUEST_TIMEOUT = (6, 30)  # (connect, read) seconds


class _TimeoutSession(Session):
    def get(self, url, **kwargs):
        kwargs.setdefault("timeout", REQUEST_TIMEOUT)
        return super().get(url, **kwargs)

YT_THROTTLE_SECONDS = float(os.environ.get("YT_THROTTLE_SECONDS", "1.5"))
YT_MAX_RETRIES = int(os.environ.get("YT_MAX_RETRIES", "3"))
# Pause length (s) between caption snippets that starts a new paragraph.
# Added 2026-11-02: transcripts used to be one flat text block; the caption
# snippets carry start/duration timing, so a real speaking pause can be turned
# into a paragraph break for free, verbatim, no LLM. Auto-generated tracks are
# near-continuous, so only genuine pauses exceed this.
YT_PARAGRAPH_GAP_S = float(os.environ.get("YT_PARAGRAPH_GAP_S", "1.0"))


def _parse_lang_list(raw):
    return {l.strip().lower() for l in raw.split(",") if l.strip()}


def transcript_language_policy():
    """(allowed_set, preferred_code) from env. Empty allowed = unrestricted."""
    allowed = _parse_lang_list(os.environ.get("YT_TRANSCRIPT_LANGUAGES", ""))
    preferred = os.environ.get("YT_TRANSCRIPT_LANGUAGE_PREFERRED", "").strip().lower()
    return allowed, preferred


def select_transcript(transcripts):
    """Pick a caption track per the configured language policy.

    Returns (track, policy) where policy is one of "native", "preferred",
    "allowed" — or (None, "no_allowed_language") when captions exist but in
    none of the allowed languages.

    Policy: native language if allowed, else the preferred language, else
    the first available allowed track (manually created before generated).
    The native track is identified by isTranslatable=true (the library
    exposes it as non-empty translation_languages) — translated tracks,
    including auto-generated translations in dozens of languages on popular
    videos, are never themselves translatable. "First generated track" is
    NOT a reliable native marker (arbitrary, region-dependent order — picked
    an Arabic ASR translation for an English video, 92sCQzn-wG8, 2026-09-15).
    With YT_TRANSCRIPT_LANGUAGES unset the policy is simply "native".
    """
    allowed, preferred = transcript_language_policy()
    generated = [t for t in transcripts if t.is_generated]
    manuals = [t for t in transcripts if not t.is_generated]
    natives = [t for t in transcripts if t.translation_languages]
    if natives:
        native = ([t for t in natives if not t.is_generated] or natives)[0]
    else:
        # No translatable track (e.g. single-track video): any track is the
        # only/native one.
        native = (generated or manuals or transcripts)[0]
    if not allowed or native.language_code.lower() in allowed:
        return native, "native"
    if preferred:
        cands = [t for t in transcripts if t.language_code.lower() == preferred]
        if cands:
            cands.sort(key=lambda t: t.is_generated)
            return cands[0], "preferred"
    for t in sorted(transcripts, key=lambda t: t.is_generated):
        if t.language_code.lower() in allowed:
            return t, "allowed"
    return None, "no_allowed_language"

# Exceptions worth retrying with backoff — transient IP/rate blocks and
# network-level failures (hung/dead connections, DNS hiccups).
RETRYABLE = (IpBlocked, RequestBlocked, requests.Timeout, requests.ConnectionError)
# Exceptions meaning "this video genuinely has no transcript to get" — no
# amount of retrying fixes these, report cleanly as unavailable.
UNAVAILABLE = (TranscriptsDisabled, NoTranscriptFound, VideoUnavailable, VideoUnplayable, AgeRestricted, InvalidVideoId)

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
URL_ID_RE = re.compile(r"(?:v=|/(?:embed|shorts)/|youtu\.be/)([A-Za-z0-9_-]{11})")


def resolve_video_id(id_or_url):
    """Bare 11-char ID, or any of youtu.be/…, watch?v=…, /shorts/…,
    /embed/… — pulled out with a regex, not full URL parsing."""
    id_or_url = id_or_url.strip()
    if VIDEO_ID_RE.match(id_or_url):
        return id_or_url
    m = URL_ID_RE.search(id_or_url)
    if m:
        return m.group(1)
    return None


def default_cache_dir():
    override = os.environ.get("YT_TRANSCRIPT_CACHE_DIR")
    if override:
        return override
    if os.name == "nt":
        # Matches the original AI Automation Society tool's literal path —
        # keep this exact on Windows in case other tools from the same
        # source share this cache location.
        return r"C:\tmp"
    import tempfile
    return os.path.join(tempfile.gettempdir(), "yt_transcripts")


def cache_path(video_id, cache_dir=None):
    cache_dir = cache_dir or default_cache_dir()
    return os.path.join(cache_dir, f"yt_{video_id}.txt")


def build_proxy_config(proxy_url=None):
    if proxy_url:
        return GenericProxyConfig(http_url=proxy_url, https_url=proxy_url)
    user = os.environ.get("YT_WEBSHARE_USER")
    password = os.environ.get("YT_WEBSHARE_PASS")
    if user and password:
        return WebshareProxyConfig(proxy_username=user, proxy_password=password)
    http_url = os.environ.get("YT_PROXY_HTTP")
    https_url = os.environ.get("YT_PROXY_HTTPS")
    if http_url or https_url:
        return GenericProxyConfig(http_url=http_url, https_url=https_url)
    return None


def snippets_to_text(snippets, gap_s=None):
    """Join caption snippets into readable text. Same word sequence as the
    old flat `" ".join(...)`, but a pause of >= gap_s seconds between the end
    of one snippet and the start of the next becomes a paragraph break
    (`\\n\\n`). Snippets without usable timing fall back to plain spaces, so
    the output can never lose words."""
    gap = YT_PARAGRAPH_GAP_S if gap_s is None else gap_s
    parts = []
    prev_end = None
    for sn in snippets:
        text = (sn.text or "").strip()
        if not text:
            continue
        start = getattr(sn, "start", None)
        duration = getattr(sn, "duration", None)
        if parts:
            if (prev_end is not None and start is not None
                    and start - prev_end >= gap):
                parts.append("\n\n")
            else:
                parts.append(" ")
        parts.append(text)
        if start is not None and duration is not None:
            prev_end = start + duration
        else:
            prev_end = None
    return "".join(parts)


def fetch_transcript(id_or_url, cache_dir=None, print_output=False, proxy_url=None,
                     max_retries=None):
    """Core entry point — importable by other scripts (e.g.
    youtube_subscriptions.py's `transcript` command) instead of shelling
    out. Returns a dict, never raises for expected failure modes (blocked/
    unavailable) — only for a genuinely unresolvable video ID.

    proxy_url: explicit proxy for this call (overrides .env proxy settings)
               — used by proxy-pool runners. max_retries: override the
               per-call retry count (pool runners pass 1, they retry by
               rotating proxies instead)."""
    video_id = resolve_video_id(id_or_url)
    if not video_id:
        return {"status": "error", "kind": "InvalidVideoId", "video_id": id_or_url,
                "message": f"Could not parse a video ID out of {id_or_url!r}."}

    path = cache_path(video_id, cache_dir)

    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            text = f.read()
        if print_output:
            print(text)
        return {"status": "cached", "video_id": video_id, "text": text,
                "word_count": len(text.split()), "path": path}

    proxy_config = build_proxy_config(proxy_url)
    api = YouTubeTranscriptApi(http_client=_TimeoutSession(), proxy_config=proxy_config)
    retries = YT_MAX_RETRIES if max_retries is None else max_retries

    attempt = 0
    while True:
        attempt += 1
        time.sleep(YT_THROTTLE_SECONDS)
        try:
            # Language selection per configured policy (native if allowed,
            # else preferred, else first allowed; see select_transcript).
            transcript_list = api.list(video_id)
            transcripts = list(transcript_list)
            if not transcripts:
                raise NoTranscriptFound(video_id, [None], transcript_list)
            track, policy = select_transcript(transcripts)
            if track is None:
                return {"status": "unavailable", "kind": "NoAllowedLanguage",
                        "video_id": video_id,
                        "message": ("captions exist but in none of the "
                                    "configured YT_TRANSCRIPT_LANGUAGES")}
            fetched = track.fetch()
            break
        except RETRYABLE as e:
            if attempt >= retries:
                return {
                    "status": "error", "kind": type(e).__name__, "video_id": video_id,
                    "message": (
                        f"Blocked after {attempt} attempts. Recovery: wait a few minutes "
                        "and retry, or set YT_WEBSHARE_USER/YT_WEBSHARE_PASS in .env to "
                        "route through a rotating residential proxy."
                    ),
                }
            time.sleep(5 * attempt)
        except UNAVAILABLE as e:
            return {"status": "unavailable", "kind": type(e).__name__, "video_id": video_id,
                    "message": str(e)}
        except Exception as e:
            return {"status": "error", "kind": type(e).__name__, "video_id": video_id,
                    "message": str(e)}

    text = snippets_to_text(fetched.snippets)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)

    if print_output:
        print(text)

    return {"status": "ok", "video_id": video_id, "text": text,
            "word_count": len(text.split()), "path": path,
            "language": track.language_code, "language_policy": policy}


def main():
    parser = argparse.ArgumentParser(description="Fetch a single YouTube video's transcript (cached, throttled)")
    parser.add_argument("video", help="Video ID or any YouTube URL form")
    parser.add_argument("--print", dest="print_output", action="store_true",
                         help="Also dump the transcript text to stdout")
    args = parser.parse_args()

    result = fetch_transcript(args.video, print_output=args.print_output)

    if result["status"] in ("ok", "cached"):
        print(f"RESULT: {result['status']} | words={result['word_count']} | path={result['path']}")
    else:
        print(f"RESULT: {result['status']} | kind={result['kind']} | video_id={result['video_id']} | {result['message']}")
        if result["status"] == "error":
            sys.exit(1)


if __name__ == "__main__":
    main()
