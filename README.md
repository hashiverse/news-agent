# news-agent

A standalone, open-source, forkable daemon that mirrors RSS feeds into hashiverse.

You point it at:

- a **feeds file** — OPML 2.0, listing RSS sources with category tags
- a **control file** — YAML, listing the identities this daemon hosts and what each one consumes / posts

…and it does the rest.

This repo is intentionally generic. The hashiverse-news project uses [`hashiverse/news-feeds`](https://github.com/hashiverse/news-feeds) as its feeds file, but anyone can fork news-agent and run it for their own domain by pointing at their own feeds file and writing their own control file.

## Status

Phase 1: file-reading, directory-management, and auto-reload plumbing only. No keys, no hashiverse client, no networking yet — those land in later phases.

## Install

```
pip install -e .
```

Requires Python 3.11+.

## Run

```
export NEWS_AGENT_GLOBAL_SALT="<at-least-32-cryptographically-random-chars>"
news-agent run \
  --feeds   example/feeds.opml \
  --control example/control.yaml \
  --create-new
```

The first run needs `--create-new` because the per-daemon data directory under `~/.news-agent/` doesn't exist yet.

## Test

```
pip install -e ".[test]"
pytest
```
