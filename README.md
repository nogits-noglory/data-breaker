# databroker-pipeline

An open-source toolkit for getting yourself out of the data broker industry. You
are your own agent: it runs locally, against a catalogue of brokers and their
verified opt-out recipes that the project builds and keeps fresh automatically.

Use it at whatever level of automation you want:

- **Tier 0, by hand.** Read [`BROKERS.md`](BROKERS.md), a browsable directory of
  every broker and how to leave it. No code.
- **Tier 1, no LLM.** `python cli.py remove --profile data/profile.yaml` replays
  the recorded recipes through a real browser. Deterministic, no API key, no cost.
- **Tier 2, your own LLM.** An MCP server (planned) exposing the catalogue and
  actions to any MCP client.
- **Tier 3, agentic.** Let a coding agent operate the repo; see [`CLAUDE.md`](CLAUDE.md).

Under the hood it's one async pipeline: discovery (registry scrape + OSINT crawl)
feeds a candidate store, the scout turns candidates into verified opt-out recipes,
and the removal agent replays those recipes. Stages share one data model and talk
through durable queues so they can run at once.

```
registry ─┐
          ├─> CandidateStore ──> scout_q ──> [scout workers] ──> BrokerStore ──> BROKERS.md
crawler  ─┘                                                          │
                                                                     v
your profile ──> actionable brokers ──> remove_q ──> [remove workers] ──> submitted / needs-human
```

## Layout

```
databroker/
  core/
    models.py     BrokerRecord, Candidate, User, RemovalJob + status/method/job enums
    domains.py    canonical_domain (one source of truth)
    recon.py      cheap HTTP recon, fingerprinting, crt.sh siblings, screenshots
    classify.py   "is this a broker?" gate (keeps junk out of the scout)
    store.py      BrokerStore / CandidateStore -- the only writers of YAML
    queue.py      async Queue: InMemoryQueue + durable SqliteQueue
    config.py     paths, models, proxies, keys (env-driven)
  stages/
    registry.py   1. registry scraper (CA auto, VT/TX/OR via file)
    crawler.py    2. OSINT discovery (crt.sh now; passive-DNS pluggable)
    scout.py      3. recon-first scout; browser deep-scout behind a lazy seam
    remover.py    5. removal agent: resolve recipe -> triage -> replay
  orchestrator.py wires queues, runs worker pools concurrently
cli.py            one entry point (scrape/crawl/discover/scout/remove/migrate/stats)
scripts/
  migrate_yaml.py legacy brokers_scouted.yaml -> new schema (repairs recipes)
data/
  brokers.yaml    4. the registry (the YAML)
  candidates.yaml discovered, pre-scout
```

## Quick start

```bash
pip install -e .                  # or: pip install pyyaml tldextract httpx mmh3
python cli.py stats               # snapshot of the catalogue
python cli.py markdown            # (re)generate BROKERS.md

# opt out yourself (Tier 1, no LLM)
cp data/profile.yaml.example data/profile.yaml   # fill in your info
pip install playwright && playwright install chromium
python cli.py remove --profile data/profile.yaml          # dry run: fills, doesn't submit
python cli.py remove --profile data/profile.yaml --live   # actually submit

# keep the catalogue growing (maintainers)
python cli.py discover            # registries + crawler -> candidates
python cli.py scout               # scout pending candidates -> brokers.yaml
```

Your `data/profile.yaml` stays on your machine. Removals dry-run by default so you
can watch the recipe replay before letting it submit.

## What's wired vs what you plug in

Built and unit-tested here (no API key, no browser): the data model and migration,
both stores, both queues, the classifier, recon (probe + fingerprint + crt.sh +
status model), and the removal agent's resolve/triage/replay logic. `python
tests/test_pipeline.py` exercises a full candidate -> scout -> store path.

Seams to fill in your environment (interfaces are defined, drivers are no-ops):

- **Browser deep-scout** (`stages/scout.py::_deep_scout`): lazy-imports
  `databroker.stages.browser_scout`. Drop your existing `broker_scout.py` in as
  that module and expose a `scout_url(url, name, cfg) -> findings` adapter.
- **Removal drivers** (`stages/remover.py`): `NullBrowserDriver` / `NullMailDriver`
  do dry runs. Implement Playwright replay (drive the resolved steps) and SMTP send.
- **Proxies** (`core/config.py::proxies`): pass a proxy-aware fetcher to recon for
  `blocked` sites; rotate residential IPs there.
- **Passive DNS** (`stages/crawler.py::SecurityTrailsSource`): add the API call.
- **Users / auth / IDV**: `get_user` in the orchestrator is a callable you back
  with your DB. `User.authorization_ref` / `idv_status` point at the signed LPOA
  and the IDV vendor token; raw ID material never enters this code.

## Suggested build order

1. Migrate, run `stats`, eyeball `data/brokers.yaml`.
2. Drop in the browser deep-scout adapter; run `scout` on a small slice.
3. Implement the Playwright + SMTP removal drivers; dry-run, then live on yourself.
4. Add residential proxies; re-scout the 40 `blocked` brokers.
5. Turn on the crawler with `--osint`; add passive DNS when you have a key.
```
