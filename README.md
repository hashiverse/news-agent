# news-agent

A standalone, open-source, **forkable** Python daemon that mirrors RSS feeds into hashiverse. One process hosts **multiple hashiverse identities**; each identity has its own keys and its own `sources:` list of RSS feeds; the daemon paces posts per identity and dedupes across the whole daemon.

The daemon is generic — anyone can fork news-agent and point it at their own control YAML for their own domain. It depends on a sibling Rust+Python project (`hashiverse-client`) for the actual hashiverse network operations.

---

## 1. How to run / develop / test

### One-time venv setup

```powershell
# from the news-agent repo root
python -m venv .venv
.\.venv\Scripts\Activate.ps1          # PowerShell on Windows
# source .venv/bin/activate           # bash/zsh on macOS/Linux
pip install -e ".[test]"
```

### Running the daemon (smoke test)

```powershell
.\.venv\Scripts\Activate.ps1
$env:NEWS_AGENT_GLOBAL_SALT = "<at-least-32-cryptographically-random-chars>"
news-agent run --control example/control.yaml --test
```

**Dry-run is the default.** Without `--production`, the daemon logs `[DRY-RUN] would post: ...` lines instead of actually calling the network. Dry-run posts ARE recorded in the SQLite history (with `is_dry_run=1`) so the scheduler still respects per-identity caps and cross-identity dedupe.

`--test` uses an ephemeral home directory under the system temp dir (auto-deleted on exit) and is **mutually exclusive with `--production`** — passing both refuses to start. Smoke runs therefore can't accidentally post to hashiverse.

`--production` is the explicit opt-in for posting for real. The daemon logs `PRODUCTION mode: real posts will be made to hashiverse` at startup so the operator sees what they got.

### Running the tests

```powershell
.\.venv\Scripts\python.exe -m pytest                # full suite (~30s, currently ~300 tests)
.\.venv\Scripts\python.exe -m pytest -k runner      # one test file
.\.venv\Scripts\python.exe -m pytest --lf           # only previously-failing tests
.\.venv\Scripts\python.exe -m pytest -x             # stop at first failure
```

**Always invoke pytest via the venv's `python.exe`** (or activate the venv first). A bare `pytest` resolves to whichever pytest is first on PATH — usually the system Python's. If a previous editable install of news-agent at a different path is still registered there, you'll get confusing `ModuleNotFoundError: No module named 'news_agent'` errors. Fix by uninstalling the system-Python copy: `python -m pip uninstall -y news-agent` (run with the system Python, not the venv).

### Working with a local hashiverse-client dev wheel

The project depends on `hashiverse-client>=0.1` from PyPI. If you're developing `hashiverse-client` in tandem (it lives in a separate workspace and is built with maturin from a PyO3 Rust crate), you can override the PyPI install with your local build.

```powershell
# install a freshly-built wheel from the hashiverse-client workspace
.\.venv\Scripts\pip install --force-reinstall --no-deps "<path-to>/hashiverse_client-<ver>-cp39-abi3-<platform>.whl"
```

For tighter tandem development, use `maturin develop` from the hashiverse-client-python source directory pointing at this venv — that builds and installs editable.

**Gotcha:** any subsequent `pip install -e ".[test]"` from the news-agent repo will re-resolve `hashiverse-client>=0.1` and clobber your dev wheel with the PyPI version. When adding a new news-agent dependency, prefer a targeted `pip install <new-dep>` over a full project reinstall. If you do reinstall, restore the dev wheel afterwards with `pip install --force-reinstall --no-deps <wheel-path>`.

If your dev wheel reports a version below 0.1 (e.g. `0.0.0`), pip prints a `dependency conflicts` warning. **Ignore it** — install succeeded, imports work, the warning is post-install consistency-check noise.

### Editable install

The `-e` flag on `pip install -e .` means source-code edits are picked up on the next test run with no reinstall needed. Reinstall only when `pyproject.toml` changes.

---

## 2. Repo layout

```
news-agent/
├── pyproject.toml                # deps: click, pyyaml, blake3, watchdog, argon2-cffi, hashiverse-client, feedparser
├── README.md                     # this file
├── example/
│   └── control.yaml              # operator-edit-able example config
├── src/news_agent/
│   ├── __init__.py
│   ├── cli.py                    # click entrypoint + main() function
│   ├── config.py                 # YAML control-file loader + per-identity validation
│   ├── data_dir.py               # daemon dir + per-identity dir + cache dir creation
│   ├── feed_cache_db.py          # feed_cache table access
│   ├── global_salt.py            # NEWS_AGENT_GLOBAL_SALT handling + daemon-dir naming
│   ├── hashiverse_setup.py       # per-identity hashiverse client construction (cached vs argon2 paths)
│   ├── keyphrase.py              # blake3 + argon2id keyphrase derivation
│   ├── logging_helpers.py        # friendly-cranky log lines + random salt suggestions
│   ├── picker.py                 # pick eligible article from a pool (recency + dedupe + keyword filters)
│   ├── poller.py                 # background thread for periodic remote-URL refresh
│   ├── posting.py                # post_or_dry_run: real or dry-run, records history
│   ├── posts_db.py               # posts table access (record + recent-24h queries)
│   ├── remote_source.py          # GitHub-URL detection, blob→raw rewrite, conditional GET
│   ├── rss_fetcher.py            # RSS body fetcher with feed_cache table backing
│   ├── rss_parser.py             # feedparser wrapper → list[Article]
│   ├── runner.py                 # the main scheduling-and-posting loop
│   ├── runtime_state.py          # in-memory snapshot of parsed control config
│   ├── scheduler.py              # pure: compute_next_post_time per identity
│   ├── state_db.py               # SQLite open + schema bootstrap
│   ├── text_utils.py             # strip_html / first_sentence / strip_hashtags helpers
│   ├── url_canonicalize.py       # pure URL canonicalisation (dedupe key)
│   ├── url_preview.py            # local OpenGraph fetcher (urllib + html.parser, no new deps)
│   └── watcher.py                # watchdog wrapper for control-file changes
├── tests/
└── .github/workflows/test.yml
```

~300 tests, all green, ~30 seconds wall-clock.

---

## 3. CLI surface

```
news-agent run [OPTIONS]
```

| Flag | Type | Purpose |
|---|---|---|
| `--control PATH-or-URL` | required | YAML control file. Local path OR HTTPS URL. GitHub `blob/` URLs are auto-rewritten to `raw.githubusercontent.com`. |
| `--create-new` | flag | Required if the per-daemon directory doesn't already exist (typo guard for `NEWS_AGENT_GLOBAL_SALT`). |
| `--remote-control-poll-minutes INT` | default 60 | When `--control` is a URL, how often to re-fetch it. Ignored for local-path control files. |
| `--test` | flag | Use an ephemeral home dir (auto-deleted on exit), implies `--create-new`, runs in dry-run with cheap argon2. Mutually exclusive with `--production`. |
| `--production` | flag | Post for real to hashiverse. Without this flag, dry-run is the default — logs what would have been posted instead. Mutually exclusive with `--test`. |
| `--verbose-hashiverse` | flag | Bridge Rust hashiverse-client `log::*` output into Python's logging. Off by default — the Rust stack is chatty. See §10. |
| `--verbose-filtering` | flag | Log every article rejected by the keyword filter at INFO level. Off by default — useful when tuning `keywords_required` / `keywords_optional`; each rejection logs the missing keywords and the haystack the picker compared against. |

The required env var is `NEWS_AGENT_GLOBAL_SALT`. Missing → daemon refuses to start. Below 32 chars → friendly-cranky warning at startup, daemon continues running.

---

## 4. Control file (YAML)

Top-level: a list of identities under an `identities:` key.

```yaml
identities:
  - salt: "8f4c2a1e9d7b6f3e5a8c2d1b4e7f9a3c6d8b1e4a7c2f5d9b8e1a4c7f2d5b8e1a"
    nickname: "BBC Mirror"
    status: "Auto-mirrored from BBC RSS feeds. Not affiliated with the BBC."
    selfie: "data:image/png;base64,iVBORw0..."   # optional
    enabled: true                                 # optional, default true
    max_posts_per_day: 30
    sources:
      - https://feeds.bbci.co.uk/news/world/africa/rss.xml
      - https://feeds.bbci.co.uk/news/science_and_environment/rss.xml
    keywords_required: ["rust"]                   # optional — ALL must match
    keywords_optional: ["async", "wasm"]          # optional — ANY must match
```

| Field | Required | Notes |
|---|---|---|
| `salt` | yes | ≥32 chars, path-safe encoding (hex / URL-safe base64). Skipped with friendly-cranky log if too short. Whole-load failure on duplicate salt across identities. |
| `nickname` | yes | Public hashiverse display name. Also used in log lines: `'BBC Mirror' (salt=8f4c2a1e…)`. |
| `status` | yes | Hashiverse bio line. |
| `selfie` | no | `data:image/png;base64,…` URL. Embedded in YAML, not a file path. |
| `enabled` | no (true) | Soft-pause without removing the identity. |
| `max_posts_per_day` | yes | Per-identity cap. |
| `sources` | yes | RSS URLs, non-empty list. The complete scope of what this identity mirrors. |
| `keywords_required` | no (empty) | Case-insensitive substring filter against the article's title + summary. **All** entries must appear in the cleaned haystack for the article to be eligible. The haystack has HTML stripped (so `<p>` markup doesn't break things) and `#tag` tokens removed (so SEO/hashtag dumps like `#tesla #robotaxi #FSD` can't cause false positives). Empty/absent → no filter. |
| `keywords_optional` | no (empty) | Same shape as `keywords_required`, but **at least one** entry must appear in the cleaned haystack. Empty/absent → no filter. Combine with `keywords_required` to AND together (e.g. require `rust`, *and* require any of `async`/`wasm`). |

Deliberately **not** in the schema:
- No `name` field (salt is unique).
- No `description` field (covered by nickname + status; YAML comments above the entry handle operator notes).
- No `include_selectors` / `exclude_selectors` / `exclude_urls`. The `sources:` list IS the full scope. If two identities want the same feed, the URL appears in both lists.

A **cross-identity duplicate-source warning** fires at load time if the same RSS URL appears in 2+ identities. Soft warning (legitimate uses exist), not a failure.

---

## 5. On-disk layout

```
~/.news-agent/                                     # daemon home root
└── <blake3(NEWS_AGENT_GLOBAL_SALT)>/             # per-daemon dir
    ├── state.sqlite                              # daemon-wide DB (posts + feed_cache)
    ├── cache/                                    # only used if --control is a URL
    │   ├── control.yaml                          # cached body
    │   └── control.yaml.meta.json                # {url, etag, last_modified, fetched_at}
    ├── <local_salt_A>/                           # per-identity dir (named after the salt)
    │   ├── client_id.hex                         # cached client_id; skipping argon2 on restart
    │   ├── last_bio.json                         # last bio sent to hashiverse — gates set_bio (production only writes here)
    │   └── (hashiverse client data dir — encrypted with NEWS_AGENT_GLOBAL_SALT)
    └── <local_salt_B>/
        └── ...
```

Hashing the global salt to namespace the per-daemon dir lets multiple daemons (different global salts) coexist in the same home directory.

`NEWS_AGENT_GLOBAL_SALT` plays three roles: (a) half of the keyphrase derivation input, (b) passphrase for the hashiverse client's on-disk key locker, (c) namespace for the daemon dir.

---

## 6. SQLite schema (`state.sqlite`)

Bootstrapped on every `state_db.open_state_db()` call (`CREATE TABLE IF NOT EXISTS`). No migration system yet — when we change a column we'll add one.

```sql
CREATE TABLE posts (
    posted_at_unix      INTEGER NOT NULL,
    identity_salt       TEXT NOT NULL,
    canonical_url       TEXT NOT NULL,    -- the cross-identity dedupe key
    source_url          TEXT NOT NULL,
    title               TEXT NOT NULL,
    item_guid           TEXT,
    is_dry_run          INTEGER NOT NULL  -- 0 or 1
);
CREATE INDEX idx_posts_posted_at ON posts(posted_at_unix);
CREATE INDEX idx_posts_canonical ON posts(canonical_url);
CREATE INDEX idx_posts_identity  ON posts(identity_salt);

CREATE TABLE feed_cache (
    source_url      TEXT PRIMARY KEY,
    body            BLOB NOT NULL,
    etag            TEXT,
    last_modified   TEXT,
    fetched_at_unix INTEGER NOT NULL
);
```

Connection is opened with `check_same_thread=False` because the runner is on a different thread from where the connection is created. We discipline ourselves to single-writer (only the runner writes).

---

## 7. Identity / key model

For each identity:

```
keyphrase = argon2id(blake3(global_salt, local_salt),
                     m=1 GiB, t=4, p=1, output=64 bytes)
hashiverse_client = HashiverseClient.create_from_keyphrase(keyphrase, ...)
```

After first creation, the resulting `client_id` is written to `<identity_dir>/client_id.hex`. On subsequent restarts, `HashiverseClient.create_from_stored_key(client_id_hex=...)` is used — **no argon2**. So argon2 is paid at most once per identity over the daemon's lifetime.

The cached file is named `client_id.hex`, NOT `public_key.hex`. (An older version of the hashiverse-client API called this "public_key"; the current API correctly calls it `client_id_hex`. We renamed accordingly.)

Argon2 parameters are deliberately aggressive — the handover-to-publisher flow leaks derived keyphrases by design, so the operator's `GLOBAL_SALT` must remain expensive to brute-force even when the attacker holds both the control file (with `local_salt`) and a handed-over keyphrase.

**Bio cache.** Each identity dir also contains `last_bio.json` capturing the most recent bio (`nickname` / `status` / `selfie` / `avatar`) actually sent to hashiverse. On client startup `hashiverse_setup.update_bio_if_changed` compares the current `IdentityConfig` bio fields against this cache and only calls `client.set_bio(...)` when something differs — hashiverse emits a fresh meta-post on every `set_bio` call regardless of payload, so de-duping at the daemon layer prevents redundant meta-posts on every restart and reload. Two further gates: (a) **dry-run mode does not call `set_bio`** at all (it just logs a `[DRY-RUN] would send bio update` line) and **does not update the cache** — that way switching from dry-run to `--production` cleanly resends. (b) Delete `last_bio.json` to force a re-send on next production startup — the manual escape hatch.

---

## 8. Scheduling & posting

The main loop (`runner.run_loop`) on each iteration:

1. Reads the runtime snapshot (mutated by the watcher's reload callback) to get the current identity list.
2. For each enabled identity, computes its **next-allowed-post time** via `scheduler.compute_next_post_time`:
   - `target_interval = 24h / max_posts_per_day`
   - Under cap: `next = max(now, last_post + target_interval) + jitter(±10%)`
   - At cap: `next = oldest_in_24h + 24h + jitter`
   - No history: `now ± 60s`
3. Sorts identities by next-allowed-post time (soonest first).
4. Walks the sorted list, fetching each identity's `sources:` (cache-backed `rss_fetcher.fetch_feed_body`) and parsing them (`rss_parser.parse_feed`).
5. Uses `picker.pick_article` to find an eligible article — published in the last 24h, not in the **cross-identity** dedupe set (canonical URLs posted in the last 24h by ANY identity).
6. First identity with an eligible article wins. Waits until its scheduled time, then posts via `posting.post_or_dry_run`.
7. If no identity has anything eligible, sleeps 60s and retries.

Cross-identity dedupe key: **canonical URL**, after normalisation in `url_canonicalize.canonicalize` (strip `utm_*` / `fbclid` / `gclid` / `mc_cid` / `mc_eid` / `_ga`, lowercase host, sort query params, drop fragment + trailing slash).

Posts (real and dry-run) are written to the `posts` table. Dry-run rows have `is_dry_run=1`. They count toward dedupe and per-identity caps so the scheduler doesn't pick the same article twice across modes.

### 8.1 Post body format

Every post is hashiverse-flavoured HTML composed by the daemon from Rust-built fragments. The shape:

```
<div class="plugin-urlpreview-card">…</div>           # always present
<p/>                                                   # only when hashtags is non-empty
<hashtag …>…</hashtag> <hashtag …>…</hashtag>          # one per identity hashtag
```

There is no separate article-title prefix — the title lives inside the preview card as the link text.

**Single source of truth for the canonical schema lives in Rust**, in `hashiverse-rust/hashiverse-lib/src/tools/plain_text_post.rs`. The PyO3 wrapper exposes free functions that delegate straight to those Rust impls; news-agent calls them and concatenates the output:

| Python free function | Rust impl |
|---|---|
| `convert_text_to_hashiverse_html_x_url_preview(title, description, image_url, url)` | builds `<div class="plugin-urlpreview-card">…</div>`, with-image vs without-image branching, description div omitted when blank, domain extracted from URL |
| `convert_text_to_hashiverse_html_x_hashtag(hashtag)` | builds the `<hashtag hashtag="…"><span class="plugin-hashtag-left">#</span><span class="plugin-hashtag-right">…</span></hashtag>` element |
| `convert_text_to_hashiverse_html_x_mention(client_id)` | builds `<mention client_id="…"></mention>` |
| `convert_text_to_hashiverse_html(text)` | full preprocessor: html-escape + hashtag + mention + newline → `<br>` |

The `plugin-urlpreview-card*` CSS classes are styled by the consuming client at view time — Rust just emits the structural HTML, no inline styles. Tiptap plugins only run while *editing*; view-time consumers see plain HTML, which is why we emit the structural form rather than the editor's `<urlpreview …>` placeholder.

`news-agent` submits the composed body via `client.submit_post(html)` (no preprocessing magic on the wrapper).

**Field resolution** happens Python-side in `posting.format_post_html`:

| Card field passed to Rust | Fallback chain |
|---|---|
| `url` | `preview.url` → `article.raw_url` |
| `title` | `preview.title` → `article.title` → `url` |
| `description` | `preview.description` → `strip_html(article.summary)` (Rust omits div if both blank) |
| `image_url` | `preview.image_url` (Rust omits image branch if blank) |

**Preview fetching.** Before posting we call `news_agent.url_preview.fetch_url_preview(url)`, which fetches the page directly via `urllib.request` (HTTPS-only, 2 MB cap, custom UA — stdlib only, no new runtime dependencies) and parses OG / twitter-card / `<title>` / `<link rel="canonical">` tags via `html.parser.HTMLParser`. The fallback chain matches `hashiverse-lib/src/tools/url_preview.rs` exactly:

| Field | Fallback chain |
|---|---|
| `title` | `meta[property='og:title']` → `meta[name='twitter:title']` → `<title>` |
| `description` | `meta[property='og:description']` → `meta[name='twitter:description']` → `meta[name='description']` |
| `image_url` | `meta[property='og:image']` → `meta[name='twitter:image']` → `meta[name='twitter:image:src']` |
| `url` | `meta[property='og:url']` → `<link rel='canonical' href=…>` (note: `href`, not `content`) |

**Dry-run does the same fetch + HTML construction as a real post** and logs the body — the whole point of dry-run is the operator sees exactly what would have hit the network. If the fetch fails (non-HTTPS URL, network error, parse failure), the post still goes out — `posting._fetch_preview_safely` logs a warning and returns blanks, and the card falls back to `article.title` as the link text with no image / no description.

**Hashtag input handling.** The Rust `_x_hashtag` function is defensive: it accepts either `"rust"` or `"#rust"` (a single leading `#` is stripped before validation). If the remaining text is empty or contains any non-alphanumeric character, it returns the original input *untouched* (an identity no-op) rather than emit a malformed `<hashtag>` element. News-agent therefore doesn't pre-validate identity hashtags — bad ones just render as plain text in the post body, which is a visible-failure-mode the operator can spot in the dry-run output.

### 8.2 RSS fetch caching

`rss_fetcher.fetch_feed_body` is cross-identity (keyed solely on `source_url`), so identities sharing a feed share the cache row. Three-layer freshness logic:

1. **30-minute freshness window** (default `CACHE_FRESHNESS_WINDOW_SECONDS`). If the cached body is younger than this, return it without any network call. Logged at DEBUG (silent at INFO) — this is the steady-state path and would otherwise spam stderr because the runner re-fetches every source on every iteration.
2. **Conditional GET.** Older cache → issue an HTTPS `GET` with `If-None-Match` / `If-Modified-Since` if the server gave us those headers. `200 OK` replaces the cached body and headers; `304 Not Modified` bumps `fetched_at` only.
3. **Stale-cache fallback on errors.** If the network call fails (connect error, 5xx, timeout) and a cache exists, return the stale body with a warning. No cache → `FeedFetchError`.

Real network events (200, 304) log at INFO; the freshness-window short-circuit logs at DEBUG.

---

## 9. Reload behaviour (control-file changes)

Watchdog observes the control file. On change → debounced ~1s → `on_change` callback fires.

The reload is **full rebuild**, not diff: blow away the entire `clients` dict and rebuild from disk. Why:
- One code path serves both startup and reload — no diff bookkeeping.
- The rebuild is cheap because cached `client_id.hex` skips argon2; only brand-new identities pay any meaningful cost.
- Eliminates an entire class of in-memory-vs-disk drift bugs.

If parsing or directory creation fails during a reload, the previous state is left intact and the daemon keeps running.

**In-flight scheduling waits are cancelled.** A `reload_event` (separate from `stop_event`) is set by `on_change` after every reload attempt and watched by `_wait_until` / `_interruptible_sleep`. If the runner is mid-wait when a reload arrives — e.g. sleeping until a scheduled post fires, which can be up to 24h — the wait returns early, the iteration aborts, and the outer `run_loop` re-enters `_one_iteration` against the freshly-loaded state. New identities, different keyword filters, and changed `max_posts_per_day` caps therefore take effect on the *next* post rather than the one after that. The runner clears `reload_event` at the top of each iteration so a single reload triggers exactly one re-evaluation.

---

## 10. Logging style

Plain text to stderr via `logging.basicConfig(...)`. Format: `YYYY-MM-DD HH:MM:SS,ms LEVEL  module: message`.

Identities in log lines show as `'BBC Mirror' (salt=8f4c2a1e…)` — nickname plus 8-char salt prefix for disambiguation when two identities share a nickname.

Salt-too-short warnings have a deliberate friendly-cranky tone:
> identity 'foo' ignored — salt is too short to be safe (8 chars, want at least 32). if you want, you can use sdjhfskdhhfkj3w2h4kj32h4kj342h…

The random suggestion is a fresh URL-safe-base64 string (cryptographically random, generated via `secrets.token_urlsafe`) — different on every load, so the operator can paste it as-is.

**Rust → Python log bridge.** Off by default; opt in with `--verbose-hashiverse`. When the flag is set, `_configure_logging` calls `hashiverse_client.init_logging()` after `basicConfig`, which installs a `pyo3-log` shim so `log::*` records emitted by `hashiverse-client` (and the rest of the Rust stack underneath it) flow through Python's `logging` module — same handler, same format, same stderr stream. Logger names on the Python side are the Rust target (e.g. `hashiverse_lib::client::peer_tracker`). To surface Rust DEBUG/TRACE output, lower the Python root logger level — no Rust rebuild needed. The bridge is process-wide and one-shot; pytest stubs it via `_stub_cli_run_side_effects` so per-test `cli.run` invocations don't hit the deliberate "logger already set" loud-fail.

**Log-level convention.** INFO is reserved for events that *wouldn't* repeat in steady state — real network fetches (200/304), posts, reloads, errors. DEBUG is for repetitive runtime detail that runs every iteration of the scheduling loop: the rss_fetcher's fresh-cache short-circuit (`fetched X (… fresh — skipped network)`), and per-article keyword-filter rejections from the picker (gated additionally behind `--verbose-filtering` so they're silent at the default INFO level even with a lowered root level). The rule of thumb: if you'd see the same line N times per minute in a healthy daemon, it's DEBUG; if you'd see it once per N minutes, it's INFO.

**Color.** Log levels are color-coded on TTY stderr — DEBUG = gray, INFO = default (no color, the most common level so coloring it would just add noise), WARNING = yellow, ERROR = red, CRITICAL = bold bright red. Color is automatically suppressed when stderr isn't a TTY (piped to a file, pager, or systemd journal — escape codes would just clutter the output there). Set `NO_COLOR=1` in the environment to force-disable color even on a TTY (the [no-color.org](https://no-color.org) convention). Implementation is `cli._ColorFormatter`, ~25 lines of `logging.Formatter` subclass — no new runtime dependencies.

---

## 11. Style + conventions

- Python 3.11+. Type hints throughout. `from __future__ import annotations` at the top of every module.
- Strings double-quoted (`"foo"`).
- Long, descriptive variable names. `client_id_hex` not `cid`. `posts_in_last_24h` not `recent`.
- No comments narrating WHAT the code does — let identifiers do that. Comments explain WHY (constraints, surprises, cross-references).
- Tests use `pytest` with `tmp_path` fixture for filesystem isolation. No mocks of `time` or `datetime`; pure functions take `now_unix` and `rng` as parameters so tests can pass deterministic values.

---

## 12. The hashiverse-client Python API (cheat-sheet)

`pip install hashiverse-client`. Current public surface used by news-agent:

```python
from hashiverse_client import (
    HashiverseClient,
    init_logging,
    convert_text_to_hashiverse_html,
    convert_text_to_hashiverse_html_x_hashtag,
    convert_text_to_hashiverse_html_x_mention,
    convert_text_to_hashiverse_html_x_url_preview,
)

# Slow path (does the keyphrase processing).
client = HashiverseClient.create_from_keyphrase(
    key_phrase="<argon2-output-hex>",
    data_dir="/path/to/identity/dir",
    passphrase="<NEWS_AGENT_GLOBAL_SALT>",
    bootstrap_addresses=None,    # None → DnssecBootstrapProvider
)

# Fast path (no argon2, uses the on-disk encrypted key locker).
client = HashiverseClient.create_from_stored_key(
    client_id_hex="<hex from client_id.hex>",
    data_dir="/path/to/identity/dir",
    passphrase="<NEWS_AGENT_GLOBAL_SALT>",
    bootstrap_addresses=None,
)

client.client_id                                # the public client ID (hex)
client.set_bio(nickname, status, selfie, avatar)
client.submit_post(html)                        # submit raw hashiverse-flavoured HTML; no preprocessing
client.fetch_url_preview(url)                   # server-side OG preview (news-agent does this locally instead)
client.list_stored_keys()

# Process-wide one-shot to bridge Rust `log::*` → Python's `logging` (gated by --verbose-hashiverse).
init_logging()

# Pure HTML-fragment builders that share the canonical hashiverse schema with the web client.
# Compose these with str concatenation and submit via client.submit_post.
convert_text_to_hashiverse_html_x_url_preview(title, description, image_url, url)
convert_text_to_hashiverse_html_x_hashtag(hashtag)        # `"rust"` or `"#rust"`; identity no-op on bad input
convert_text_to_hashiverse_html_x_mention(client_id_hex)  # 64-hex
convert_text_to_hashiverse_html(text)                     # full preprocessor (escape + hashtag + mention + newline)
```

The Rust client carries a tokio runtime internally; dropping all references to the client lets that runtime shut down. We `clients.clear()` on shutdown and on reload-rebuild.

---

## 13. For Claude Code: quick pointers when picking up the next chunk

- **Run the test suite** before doing anything to confirm baseline green.
- The user is iterating the daemon block-by-block. Each block is one logical concern; don't extrapolate across blocks unless invited.
- When adding a feature, ask up front for any meaningfully ambiguous decision (1–3 questions max), then implement.
- The `--test` flag is a guarantee: it MUST refuse to run alongside `--production`, and it always implies dry-run + ephemeral home + cheap argon2.
- Module dependencies are small and intentional. Pure functions (`url_canonicalize`, `scheduler`, `picker`, `text_utils`) take `now_unix` / `rng` / inputs as parameters; impure modules (`runner`, `posting`, `rss_fetcher`, `url_preview`) compose them with side-effecting layers (`posts_db`, hashiverse client, network). Keep that separation when adding code.
