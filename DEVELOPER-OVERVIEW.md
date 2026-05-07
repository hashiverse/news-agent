# news-agent — developer overview

This document is the entry point for anyone starting fresh on this codebase. It summarises what's been built, the key design decisions, where the moving parts live, and what's pending.

---

## 1. What news-agent is

A standalone, open-source, **forkable** Python daemon that mirrors RSS feeds into hashiverse. One process hosts **multiple hashiverse identities**; each identity has its own keys and its own `sources:` list of RSS feeds; the daemon paces posts per identity and dedupes across the whole daemon.

The daemon is generic — anyone can fork news-agent and point it at their own control YAML for their own domain. It depends on a sibling Rust+Python project (`hashiverse-client`) for the actual hashiverse network operations.

---

## 2. How to run / develop / test

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
.\.venv\Scripts\python.exe -m pytest                # full suite (~22s, currently ~181 tests)
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

## 3. Repo layout

```
news-agent/
├── pyproject.toml                # deps: click, pyyaml, blake3, watchdog, argon2-cffi, hashiverse-client, feedparser
├── README.md                     # short operator-facing intro
├── DEVELOPER_OVERVIEW.md         # this file
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
│   ├── picker.py                 # pure: pick eligible article from a pool
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
│   ├── url_canonicalize.py       # pure URL canonicalisation (dedupe key)
│   └── watcher.py                # watchdog wrapper for control-file changes
├── tests/
│   ├── fixtures/
│   │   ├── control_minimal.yaml
│   │   └── control_invalid_salt.yaml
│   ├── test_cli_reload.py
│   ├── test_config.py
│   ├── test_data_dir.py
│   ├── test_feed_cache_db.py
│   ├── test_global_salt.py
│   ├── test_hashiverse_setup.py
│   ├── test_keyphrase.py
│   ├── test_logging_helpers.py
│   ├── test_picker.py
│   ├── test_poller.py
│   ├── test_posting.py
│   ├── test_posts_db.py
│   ├── test_remote_source.py
│   ├── test_rss_fetcher.py
│   ├── test_rss_parser.py
│   ├── test_runner.py
│   ├── test_scheduler.py
│   ├── test_state_db.py
│   ├── test_url_canonicalize.py
│   └── test_watcher.py
└── .github/workflows/test.yml
```

181 tests, all green, ~22 seconds wall-clock.

---

## 4. CLI surface

```
news-agent run [OPTIONS]
```

| Flag | Type | Purpose |
|---|---|---|
| `--control PATH-or-URL` | required | YAML control file. Local path OR HTTPS URL. GitHub `blob/` URLs are auto-rewritten to `raw.githubusercontent.com`. |
| `--create-new` | flag | Required if the per-daemon directory doesn't already exist (typo guard for `NEWS_AGENT_GLOBAL_SALT`). |
| `--remote-poll-minutes INT` | default 60 | When `--control` is a URL, how often to re-fetch it. Ignored for local-path control files. |
| `--test` | flag | Use an ephemeral home dir (auto-deleted on exit), implies `--create-new`, runs in dry-run with cheap argon2. Mutually exclusive with `--production`. |
| `--production` | flag | Post for real to hashiverse. Without this flag, dry-run is the default — logs what would have been posted instead. Mutually exclusive with `--test`. |

The required env var is `NEWS_AGENT_GLOBAL_SALT`. Missing → daemon refuses to start. Below 32 chars → friendly-cranky warning at startup, daemon continues running.

---

## 5. Control file (YAML)

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

Deliberately **not** in the schema:
- No `name` field (salt is unique).
- No `description` field (covered by nickname + status; YAML comments above the entry handle operator notes).
- No `include_selectors` / `exclude_selectors` / `exclude_urls`. The `sources:` list IS the full scope. If two identities want the same feed, the URL appears in both lists.

A **cross-identity duplicate-source warning** fires at load time if the same RSS URL appears in 2+ identities. Soft warning (legitimate uses exist), not a failure.

---

## 6. On-disk layout

```
~/.news-agent/                                     # daemon home root
└── <blake3(NEWS_AGENT_GLOBAL_SALT)>/             # per-daemon dir
    ├── state.sqlite                              # daemon-wide DB (posts + feed_cache)
    ├── cache/                                    # only used if --control is a URL
    │   ├── control.yaml                          # cached body
    │   └── control.yaml.meta.json                # {url, etag, last_modified, fetched_at}
    ├── <local_salt_A>/                           # per-identity dir (named after the salt)
    │   ├── client_id.hex                         # cached client_id; skipping argon2 on restart
    │   └── (hashiverse client data dir — encrypted with NEWS_AGENT_GLOBAL_SALT)
    └── <local_salt_B>/
        └── ...
```

Hashing the global salt to namespace the per-daemon dir lets multiple daemons (different global salts) coexist in the same home directory.

`NEWS_AGENT_GLOBAL_SALT` plays three roles: (a) half of the keyphrase derivation input, (b) passphrase for the hashiverse client's on-disk key locker, (c) namespace for the daemon dir.

---

## 7. SQLite schema (`state.sqlite`)

Bootstrapped on every `state_db.open_state_db()` call (`CREATE TABLE IF NOT EXISTS`). No migration system yet — when we change a column we'll add one.

```sql
CREATE TABLE posts (
    posted_at_unix      INTEGER NOT NULL,
    identity_salt       TEXT NOT NULL,
    canonical_url       TEXT NOT NULL,    -- the cross-identity dedupe key
    source_url          TEXT NOT NULL,
    title               TEXT NOT NULL,
    item_guid           TEXT,
    hashiverse_post_id  TEXT,             -- NULL when is_dry_run = 1
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

## 8. Identity / key model

For each identity:

```
keyphrase = argon2id(blake3(global_salt, local_salt),
                     m=1 GiB, t=4, p=1, output=64 bytes)
hashiverse_client = HashiverseClient.create_from_keyphrase(keyphrase, ...)
```

After first creation, the resulting `client_id` is written to `<identity_dir>/client_id.hex`. On subsequent restarts, `HashiverseClient.create_from_stored_key(client_id_hex=...)` is used — **no argon2**. So argon2 is paid at most once per identity over the daemon's lifetime.

The cached file is named `client_id.hex`, NOT `public_key.hex`. (An older version of the hashiverse-client API called this "public_key"; the current API correctly calls it `client_id_hex`. We renamed accordingly.)

Argon2 parameters are deliberately aggressive — the handover-to-publisher flow leaks derived keyphrases by design, so the operator's `GLOBAL_SALT` must remain expensive to brute-force even when the attacker holds both the control file (with `local_salt`) and a handed-over keyphrase.

---

## 9. Scheduling & posting

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

Posts (real and dry-run) are written to the `posts` table. Dry-run rows have `is_dry_run=1` and `hashiverse_post_id=NULL`. They count toward dedupe and per-identity caps so the scheduler doesn't pick the same article twice across modes.

---

## 10. Reload behaviour (control-file changes)

Watchdog observes the control file. On change → debounced ~1s → `on_change` callback fires.

The reload is **full rebuild**, not diff: blow away the entire `clients` dict and rebuild from disk. Why:
- One code path serves both startup and reload — no diff bookkeeping.
- The rebuild is cheap because cached `client_id.hex` skips argon2; only brand-new identities pay any meaningful cost.
- Eliminates an entire class of in-memory-vs-disk drift bugs.

If parsing or directory creation fails during a reload, the previous state is left intact and the daemon keeps running.

---

## 11. Logging style

Plain text to stderr via `logging.basicConfig(...)`. Format: `YYYY-MM-DD HH:MM:SS,ms LEVEL  module: message`.

Identities in log lines show as `'BBC Mirror' (salt=8f4c2a1e…)` — nickname plus 8-char salt prefix for disambiguation when two identities share a nickname.

Salt-too-short warnings have a deliberate friendly-cranky tone:
> identity 'foo' ignored — salt is too short to be safe (8 chars, want at least 32). if you want, you can use sdjhfskdhhfkj3w2h4kj32h4kj342h…

The random suggestion is a fresh URL-safe-base64 string (cryptographically random, generated via `secrets.token_urlsafe`) — different on every load, so the operator can paste it as-is.

---

## 12. Pending / known TODOs

### 12.a — `--test` should use cheap argon2 parameters (NOT YET IMPLEMENTED)

Currently `--test` runs production argon2 (m=1 GiB, t=4) — that's ~5 seconds per identity for first-time derivation, defeating the "fast smoke run" intent. The plan:

- Add `derive_keyphrase_cheap(global, local)` to `keyphrase.py` with `m=8 MiB, t=1, p=1, output=32` and `TEST_MODE_*` constants.
- Plumb `derive_fn` through `_start_clients_for_identities` and `_reload_state` in `cli.py` (today they call the production default).
- When `--test` is set in `cli.run`, pass `derive_keyphrase_cheap` as the `derive_fn`.
- Add a unit test verifying `--test` uses the cheap function.

### 12.b — Bio updates

`hashiverse_setup.start_hashiverse_client_for_identity` brings up the client but does NOT call `client.set_bio(nickname, status, selfie, ...)`. The control file's `nickname` / `status` / `selfie` fields are read into the `IdentityConfig` and then ignored beyond identity-label formatting.

To wire this up: after client startup, call `client.set_bio(...)` with the YAML values. The hashiverse client de-dupes identical bio updates so calling it on every restart is fine. Adding to the reload path makes nickname/status edits take effect on the network.

### 12.c — Hashiverse post_id capture

`posting.post_or_dry_run` always records `hashiverse_post_id=None` for real posts. The hashiverse client's `post_with_preprocessing` doesn't currently return the new post's ID synchronously. When the API exposes it, capture and persist.

---

## 13. Style + conventions

- Python 3.11+. Type hints throughout. `from __future__ import annotations` at the top of every module.
- Strings double-quoted (`"foo"`).
- Long, descriptive variable names. `client_id_hex` not `cid`. `posts_in_last_24h` not `recent`.
- No comments narrating WHAT the code does — let identifiers do that. Comments explain WHY (constraints, surprises, cross-references).
- Tests use `pytest` with `tmp_path` fixture for filesystem isolation. No mocks of `time` or `datetime`; pure functions take `now_unix` and `rng` as parameters so tests can pass deterministic values.

---

## 14. The hashiverse-client Python API (cheat-sheet)

`pip install hashiverse-client`. Current public surface:

```python
from hashiverse_client import HashiverseClient

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
client.post_with_preprocessing(text)            # converts plain text to hashiverse HTML
client.post_without_preprocessing(html)         # raw HTML (used for digests with <sequel ...>)
client.fetch_url_preview(url)                   # server-side OG preview
client.list_stored_keys()
```

The Rust client carries a tokio runtime internally; dropping all references to the client lets that runtime shut down. We `clients.clear()` on shutdown and on reload-rebuild.

---

## 15. Quick pointers when picking up the next chunk

- **Run the test suite** before doing anything to confirm baseline green.
- The user is iterating the daemon block-by-block. Each block is one logical concern; don't extrapolate across blocks unless invited.
- When adding a feature, ask up front for any meaningfully ambiguous decision (1–3 questions max), then implement.
- The `--test` flag is a guarantee: it MUST imply `--dry-run` and SHOULD use cheap argon2 parameters (12.a is the implementation TODO).
- Module dependencies are small and intentional. Pure functions (`url_canonicalize`, `scheduler`, `picker`) take `now_unix` / `rng` as parameters; impure modules (`runner`, `posting`) compose them with side-effecting layers (`posts_db`, hashiverse client). Keep that separation when adding code.
