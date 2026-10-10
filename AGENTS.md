# AGENTS.md

Instructions for coding agents (OpenAI Codex, Claude Code, and others) working in this repository.

## What this project is

A daily academic paper tracker. `python -m paper_agent` searches journal databases for new papers on the
topics in a config file, downloads open-access PDFs, has an LLM classify and analyze them, and writes a
daily digest to `library/digests/`.

There are two engines, chosen by `engine:` in the config file:

| Config file | Engine | LLM calls go through | Browser fallback |
|---|---|---|---|
| `config.yaml` | `claude` (default) | Anthropic API (`ANTHROPIC_API_KEY`) | Claude browser use toolset + Playwright driver (`paper_agent/browser/driver.py`) |
| `config.codex.yaml` | `codex` | `codex exec` subprocess (Codex login or `CODEX_API_KEY`) | `codex exec` + Playwright MCP (`paper_agent/browser/codex_agent.py`) |

Shared code: sources (`paper_agent/sources/`), journal tiers (`journals.yaml`, `paper_agent/journals.py`),
prompts (`paper_agent/prompts.py`), pipeline (`paper_agent/pipeline.py`). Keep both engines working when you
change shared code; prompt changes go in `prompts.py`, not in an engine.

## Running

```bash
pip install -r requirements.txt
python -m paper_agent --config config.codex.yaml --dry-run --days 14   # search only, no LLM calls
python -m paper_agent --config config.codex.yaml --list-journals       # check resolved ISSNs
python -m paper_agent --config config.codex.yaml                       # full run
```

The program needs outbound network access to the academic APIs (api.clarivate.com, api.elsevier.com,
api.crossref.org, api.openalex.org, api.semanticscholar.org, export.arxiv.org) and to publisher sites for PDFs.
Inside a Codex sandbox with network off (the `workspace-write` default, or Codex cloud's agent phase), these
calls fail and every source logs "API 無法使用". Ask the user to run it outside the sandbox or to enable network
access; do not work around the sandbox.

`--dry-run` is the safe way to check search behavior: it makes no LLM calls and never starts a browser.

## Conventions

- User-facing text (logs, comments, README, generated articles) is Traditional Chinese; identifiers are English.
- Each source module exposes `search(keywords, since, limit, cfg, journals=None) -> list[Paper]` and raises
  `SourceUnavailable` when its API cannot be used, which triggers the fallback chain in
  `paper_agent/browser/__init__.py`: browser search, then journal RSS (`sources/rss.py`) when the browser is
  blocked, fails, or is off. A site that blocks automation is not retried through other means (stealth
  scraping services, proxy rotation, CAPTCHA solvers); RSS is the sanctioned path.
- Do not commit `library/` from an agent session (only the daily workflows commit it), API keys, or browser
  profiles (`~/.paper-agent/`).
- Do not add scraping of publisher sites outside the browser fallback, and do not add anything that hides
  automation from websites or bypasses logins or CAPTCHAs.

## Testing

```bash
pip install -r requirements-dev.txt
python -m pytest            # all tests use mocks: no network, no API keys
```

- `tests/test_pipeline.py`: dedup (DOI and title), per-topic records, per-paper error handling, retries
  (`store.MAX_ATTEMPTS`), and the circuit breaker that stops on systemic AI failures without recording them.
- `tests/test_cost.py`: `classify_model`, the Message Batches path in `llm.py` (ordering, errors, refusals, timeout →
  cancel → normal calls) against a fake client, and per-tier search windows (`tier_every_days`, backfill after gaps).
- `tests/test_sources.py`, `tests/test_fallback.py`: source parsing and the API → browser → RSS fallback chain.
- `tests/test_browser.py`: the Playwright driver in real Chromium against `tests/fixtures/site/`, and the Claude
  browser agent against a local fake Messages API. Skipped without Chromium (`PAPER_AGENT_TEST_CHROMIUM` can point
  to an executable).
- `tests/test_codex.py`: real `codex exec` against a local fake Responses API via a throwaway `CODEX_HOME`.
  Skipped without the `codex` CLI.

Add a test with every behavior change; CI (`.github/workflows/tests.yml`) runs the suite on each push.
