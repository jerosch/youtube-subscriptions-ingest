# YouTube Subscriptions → Second-Brain Ingest
# By Jeffrey Smith - efficientstreet.com

Pulls video metadata from your real YouTube subscriptions and turns it into a genuine, cross-linked knowledge graph in your Obsidian-style second brain — not just a flat list of videos.

Every video becomes its own note, with a full description, clickable links to any timestamps mentioned ("2:15 — the pricing breakdown" jumps straight to that moment), and real `[[wikilinks]]` to the topics and named tools/products it touches. Ask about a topic later and it opens instantly, showing you every video that's ever mentioned it — no re-searching, no re-watching.

**Read [GUIDE.md](GUIDE.md) for a full walkthrough of exactly what this does and how it behaves, prompt by prompt.**

![Workflow overview: Fetch Latest / Ingest / Show Local paths, what each prompt asks, and what gets written where](docs/workflow-infographic.jpeg)

---

## What you get

- **`fetch`** — pulls verbatim video metadata (title, description, views, publish date) from every channel you're subscribed to, into a local `raw/` folder. No interpretation, just the facts.
- **`transcript`** — pulls each video's caption track straight from YouTube (in the video's native language, or a configured fallback language — see "The transcript phase") into a sidecar file next to the raw metadata. Cached, throttled, and retrying on rate blocks — safe to re-run. Videos with captions off are marked "unavailable", not silently dropped.
- **`ingest`** — processes `raw/` into your actual vault: one real note per video, plus auto-created/updated "concept" pages (topics like *Automation*, *CRM*, *Sales*) and "entity" pages (named things like *Claude*, *GoHighLevel*) that every relevant video links back to.
- **`summarize`** *(optional)* — generates a short AI summary of each video's transcript and writes it onto the video's source page. Runs through the [`pi` CLI](https://github.com/badlogic/pi-mono), one stateless ephemeral session per video, so summaries can never influence each other. Idempotent and resumable (see "The summarize phase").
- **`reformat`** *(optional)* — inserts paragraphs and `###` topic headings into existing transcripts on the source pages, so they read as structured text instead of one flat block. Also a `pi` pass per video; the model returns insertion instructions (heading + verbatim anchor) instead of the full text, so word preservation is exact by construction. Raw sidecars stay verbatim (see "The reformat phase").
- **`feed`** — regenerates a chronological feed at the vault root: every ingested video, newest first. Split for readability into `feed.md` (index: last 7 days inline + links to every month) and per-month files `feed/<YYYY>/<YYYY-MM>.md` (one table per day: `Thumbnail | Video | Hashtags | Channel`). `ingest` refreshes it automatically at the end of every run.
- **`thumbnails`** — downloads each ingested video's YouTube thumbnail (`hqdefault`, 480×360) into the vault, embeds it on the video's source page and shows it in the feed table. Index-deduped and resumable like every other phase.
- **Channel exclusion** — keep chosen channels out of the pipeline entirely: `configure --exclude-channel "Channel Name"` (repeatable). Exclusions apply to `fetch` and `transcript`; anything already ingested stays in your vault untouched.
- **No artificial caps** — a channel that posted 500 videos in your chosen window gets all 500, not an arbitrary top-10.
- **Safe to re-run anytime** — both phases dedupe by YouTube's own Video ID, so running `fetch`/`ingest` again never creates duplicates.
- **Portable** — not hardcoded to any one project or vault. First run asks where you want the raw files and the wiki output to go.

## Requirements

- Python 3.9+
- A YouTube/Google account (the one whose subscriptions you want to pull)
- Your own Google Cloud OAuth credentials (free, ~5 minutes to set up — see below)
- (Optional but recommended) An Obsidian vault, or willingness to let this scaffold a minimal one
- (Optional) The [`pi` CLI](https://github.com/badlogic/pi-mono) on your PATH — only needed for the `summarize` command

## Install

```bash
git clone https://github.com/SomewhereSimulated/youtube-subscriptions-ingest.git
cd youtube-subscriptions-ingest
pip install -r requirements.txt
```

## Set up your own Google OAuth credentials

**This project ships with zero credentials of any kind.** Every user needs their own — it takes about 5 minutes and costs nothing.

1. Go to [console.cloud.google.com](https://console.cloud.google.com) and create (or select) a project.
2. **APIs & Services → Library** → search **"YouTube Data API v3"** → **Enable**.
3. **APIs & Services → Credentials** → **+ Create Credentials** → **OAuth client ID**.
   - If prompted, configure the OAuth consent screen first (choose "External," fill in the required fields — this is fine for personal use, you don't need to publish/verify the app).
   - Application type: **Desktop app**. Name it anything.
   - Click **Create** — a popup shows your **Client ID** and **Client Secret**. Copy both (or find them again anytime under Credentials).
4. In this project folder:
   ```bash
   cp .env.example .env
   ```
   Paste your Client ID and Client Secret into `.env`.

## Configure and authenticate

```bash
# One-time: tell it where to put things
python scripts/youtube_subscriptions.py configure --raw-dir "./raw" --wiki-root "./my-vault"

# One-time: sign in with your Google account
python scripts/youtube_subscriptions.py auth

# Confirm it worked
python scripts/youtube_subscriptions.py test
```

`configure` is safe to run against an existing Obsidian vault — it only creates `index.md`/`log.md` if they don't already exist, and never overwrites real content. Point `--wiki-root` at your actual vault if you have one.

## Use it

```bash
# Pull new videos from your subscriptions (add --days N to backfill further, e.g. --days 90)
python scripts/youtube_subscriptions.py fetch

# Optional middle step: pull each video's caption track (see below)
python scripts/youtube_subscriptions.py transcript

# Process what's been pulled into real wiki pages
python scripts/youtube_subscriptions.py ingest

# Optional: AI summary of each video's transcript, written onto its source page (see below)
python scripts/youtube_subscriptions.py summarize

# Optional: paragraphs + ### headings in the transcript on each source page (see below)
python scripts/youtube_subscriptions.py reformat

# Regenerate the chronological feed page (ingest already does this automatically)
python scripts/youtube_subscriptions.py feed

# Download thumbnails for ingested videos (source pages + feed), resumable
python scripts/youtube_subscriptions.py thumbnails
```

That's it for the basics. `fetch` alone doesn't make anything searchable — `ingest` is the step that builds the actual knowledge graph.

### The transcript phase

`transcript` sits between `fetch` and `ingest`. It scrapes each video's caption track directly from YouTube's own captions endpoint via `youtube_transcript_api` — no audio download, no Whisper, no paid API. Per video it:

- Fetches the video's **native-language** track. The original track is identified by `isTranslatable=true` (translated tracks — including the auto-generated machine translations YouTube serves in dozens of languages on popular videos — are never themselves translatable), with a manually created original preferred over a generated one.
- Writes the text to a `.transcript.txt` sidecar next to the raw metadata file and appends a Transcript section into the raw file itself, so `ingest` folds it into the wiki note.
- **Caches** results to disk (system temp dir by default; override with `YT_TRANSCRIPT_CACHE_DIR`), **throttles** between requests (`YT_THROTTLE_SECONDS`, default 1.5s), and **retries** transient IP/rate blocks with backoff (`YT_MAX_RETRIES`, default 3).
- Records every outcome in `raw/_transcript-index.tsv`. A video with captions disabled lands there as `unavailable` — a clean end-state, not an error. Re-runs never re-fetch cached videos and pick up anything that previously failed.

**Language policy** (optional): by default every video is fetched in its native language. To restrict which languages are ever loaded, set `YT_TRANSCRIPT_LANGUAGES` in `.env` to a comma-separated list of YouTube language codes (e.g. `de,en`). If the video's native language is in the list it is used; otherwise the `YT_TRANSCRIPT_LANGUAGE_PREFERRED` language (e.g. `en`) is fetched as a translation; otherwise the first available allowed track. A video whose captions exist in none of the allowed languages is recorded as `unavailable` (`NoAllowedLanguage`). Leave both unset for the native-language default.

All HTTP requests go through a session with connect/read timeouts (6s/30s) — a dead or hung connection surfaces as a retryable error instead of stalling the run indefinitely.

**Proxying** (optional, for when YouTube rate-limits your IP): set `YT_WEBSHARE_USER` + `YT_WEBSHARE_PASS` in `.env` for a Webshare residential proxy, or `YT_PROXY_HTTP`/`YT_PROXY_HTTPS` for a standard proxy URL. For programmatic use, `fetch_transcript()` also accepts per-call `proxy_url=...` and `max_retries=...` overrides — the hook rotating multi-proxy runners build on.

### The summarize phase (optional)

`summarize` runs after `ingest` and writes a `## Summary` section (a few sentences distilling the transcript) onto each video's source page, right after `## Description`. It deliberately does **not** ship its own LLM API binding: every summary is a single stateless call to the [`pi` coding-agent CLI](https://github.com/badlogic/pi-mono) (`pi -p --no-session --no-tools`, transcript passed via a temp file), so **pi owns provider, model, and auth** — whatever you have configured there is what gets used. One ephemeral process per video means summaries can never influence each other.

- **Idempotent and resumable** — outcomes are recorded in `raw/_summary-index.tsv`; re-runs only process videos not summarized yet. Videos without a transcript are skipped until `transcript` has caught up.
- **Language** — `YT_SUMMARY_LANGUAGE` (falls back to `YT_TRANSCRIPT_LANGUAGE_PREFERRED`, then German).
- **Tuning** (all optional, in `.env`): `YT_SUMMARY_PI_BIN`, `YT_SUMMARY_PI_MODEL`, `YT_SUMMARY_MAX_CHARS` (transcript truncation ceiling), `YT_SUMMARY_TIMEOUT_S`, `YT_SUMMARY_RETRIES`, `YT_SUMMARY_THROTTLE_S`.

### The reformat phase (optional)

`reformat` runs after `ingest` and rewrites the `## Transcript` section on each video's **source page** with a readable version: paragraph breaks plus short `###` headings where the topic changes. Same pi mechanics as `summarize` (one stateless call per video) — but since 2026-11-05 the model does **not** echo the transcript back. It outputs only insertion instructions (`HEADING|<heading>|<anchor>` / `BREAK|<anchor>`, anchor = first 5–8 words of the new paragraph quoted verbatim), and the script splices them in locally. Output tokens drop ~50x (generation was the bottleneck on a shared LLM engine), and word preservation is exact **by construction** — the spoken words are never regenerated. An anchor the model paraphrased is skipped; a video is retried when fewer than half the anchors resolve. The `raw/` sidecars are deliberately **not** touched: they remain the verbatim archive.

- **Idempotent and resumable** — outcomes in `raw/_reformat-index.tsv` (`ok` / `no_captions` / `too_long` terminal; errors retried). Transcripts longer than `YT_REFORMAT_MAX_CHARS` (default 120000) are marked `too_long` instead of silently truncated — reformatting must never drop content.
- **New fetches need no reformat for paragraphs**: `transcript` already breaks paragraphs at real speaking pauses (≥ `YT_PARAGRAPH_GAP_S` seconds, default 1.0) using the caption snippets' own timing — verbatim, no LLM. `reformat` is what brings the existing archive up and adds headings on top.
- **Tuning** (all optional, in `.env`): `YT_REFORMAT_PI_BIN`, `YT_REFORMAT_PI_MODEL` (both default to the `YT_SUMMARY_*` values), `YT_REFORMAT_MAX_CHARS`, `YT_REFORMAT_TIMEOUT_S`, `YT_REFORMAT_RETRIES`, `YT_REFORMAT_THROTTLE_S`.

### Channel exclusion

```bash
python scripts/youtube_subscriptions.py configure --exclude-channel "Channel A" --exclude-channel "Channel B"
```

The list is stored in the config file and survives later `configure` runs (re-run without `--exclude-channel` to keep it; pass a new list to replace it). Matching is case-insensitive on the channel title. Excluded channels are skipped by `fetch` and `transcript` — already-ingested pages are never removed.

### The feed page

The feed is fully rebuilt from the fetch + ingest indexes on every `ingest`/`feed` run, so it can never drift from the actual archive. Since 2026-11-04 it is split by time instead of living in one file (a single `feed.md` past ~9k thumbnail-bearing table rows renders sluggishly in Obsidian):

- **`feed.md`** — the entry point: the last 7 days inline as day tables, then a `## Monate` index linking every month file with its entry count.
- **`feed/<YYYY>/<YYYY-MM>.md`** — one file per month (year subfolders), day tables newest-first inside.

Video cells link the source pages using the link mechanism described below, and show the hashtags found in the original video title (`—` when the title has none). Month files no longer backed by the indexes are deleted on the next run.

### How links work in this vault

All links the tool writes into page *bodies* (feed tables, concept/entity backlink bullets, `index.md`, `log.md`) use one shared mechanism: a percent-encoded Markdown link where **only spaces are encoded** — exactly what Obsidian's link resolver handles. `[[wikilinks]]` are avoided in bodies on purpose: source page filenames end in ` [Video ID]` (a nested-bracket wikilink truncates at the first `]`), and Obsidian cannot link to filenames containing `#` or `^` at all (they read as heading/block-ref markers, with no working escape) — so those characters are stripped from wiki page filenames. Frontmatter `sources: ["[[...]]"]` is the one exception; Obsidian's frontmatter reader handles it correctly and it stays as wikilinks.

## Using this with an AI coding assistant (Claude Code, etc.)

`SKILL.md` in this repo is a full behavioral spec — copy it into your assistant's skills folder (for Claude Code: `.claude/skills/subscription-videos-metadata/SKILL.md`) and it'll drive the whole workflow conversationally: asking how far back to fetch, walking you through first-time setup, generating full reports on request, and so on, instead of you typing raw commands.

## Tuning it to your own subscriptions

The topic/entity detection (`CONCEPT_TAXONOMY` and `ENTITY_TAXONOMY` near the top of `scripts/youtube_subscriptions.py`) is a plain keyword match — deterministic, free, and fast enough to run on a full archive, but it's tuned to one particular mix of AI/automation/business channels. If your subscriptions are about something else entirely (cooking, gaming, history — anything), extend or replace these dictionaries with keywords relevant to what you actually watch.

## License

MIT — see [LICENSE](LICENSE).

## Credits

**Jeffrey Smith**
Email: jeffrey@efficientstreet.com
YouTube: https://youtube.com/@JeffreyEntrepreneur
Website: https://efficientstreet.com

**Thanks:** I would like to thank Nate Herk and KJ Rainey for their wonderful classroom videos to help me jump into the world of AI automation and skill building. I would also like to thank their respective Skool communities for their feedback, support and answering my questions.

Special thanks to Ryan Cunningham for providing the code that powers the transcript-fetch phase, including cache-first logic, retry behavior with backoff, and graceful handling of unavailable captions.

Hopefully, this is the first file of many to come to my GitHub.
