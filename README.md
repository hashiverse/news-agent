# news-agent

A standalone, open-source, forkable daemon that mirrors RSS feeds into hashiverse posts.

## Quick start

### Install

```
pip install -e .
```

Requires Python 3.11+.

### Run

```
export NEWS_AGENT_GLOBAL_SALT="<at-least-32-cryptographically-random-chars>"
news-agent run \
  --feeds   example/feeds.opml \
  --control example/control.yaml \
  --create-new
```

The first run needs `--create-new` because the per-daemon data directory under `~/.news-agent/` doesn't exist yet.

### Test

```
pip install -e ".[test]"
pytest
```
