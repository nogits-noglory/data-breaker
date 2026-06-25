# CLAUDE.md

Operating guide for Claude Code (and any coding agent) working in this repo.

## What this is

An open-source toolkit for removing yourself from data brokers. You are your own
agent: there is no hosted service and no other users. Everything runs locally
against `data/brokers.yaml`, a catalogue of brokers and their verified opt-out
recipes. The same catalogue is also published as `BROKERS.md` for people who want
to opt out by hand.

## The four ways to use it (tiers)

0. **By hand.** Read `BROKERS.md`, open each opt-out page, follow the steps.
1. **No LLM.** `python cli.py remove --profile data/profile.yaml` replays the
   recorded recipes through Playwright. Deterministic, no API key.
2. **Your own LLM via MCP.** Planned: an MCP server exposing the catalogue and actions.
3. **This file.** Let a coding agent run the maintenance and removals.

## Commands

```bash
python cli.py stats                      # registry health snapshot
python cli.py markdown                   # regenerate BROKERS.md from the YAML
python cli.py scrape                      # pull state registries -> candidates
python cli.py crawl                       # OSINT discovery -> candidates
python cli.py discover                    # scrape + crawl
python cli.py scout                       # scout pending candidates -> brokers.yaml
python cli.py remove --profile data/profile.yaml          # DRY RUN (default)
python cli.py remove --profile data/profile.yaml --live   # actually submit
```

Scouting and the crawler's deep classify need network and, for the browser
deep-scout, the `[browser]` extra plus `ANTHROPIC_API_KEY`. Tier-1 removal needs
`pip install playwright && playwright install chromium`.

## Where things live

- `databroker/core/` shared model, stores, queue, recon, classify, config
- `databroker/stages/` registry, crawler, scout (+ browser_scout), remover, drivers
- `databroker/orchestrator.py` wires queues and worker pools
- `data/brokers.yaml` the catalogue (the source of truth; only the stores write it)
- `scripts/` migration and the markdown generator

## Rules for an agent operating here

- **Default to dry runs.** Never pass `--live` unless the human explicitly asks
  this session. A live run submits real legal requests on their behalf.
- **Only the person's own data.** This tool removes the operator's own records.
  Do not run removals against a profile that is not the operator's, and do not
  add anyone else's personal info to a profile.
- **Don't hand-edit `brokers.yaml`.** Go through `BrokerStore` so the field-merge
  and dedup hold. If you must bulk-edit, re-run `cli.py stats` after to sanity check.
- **The YAML is the contract.** If you change `core/models.BrokerRecord`, update
  `scripts/migrate_yaml.py` and regenerate `BROKERS.md`.
- **Blocked != dead.** A `blocked` status means an anti-bot wall, not a gone site;
  re-scout those with a residential proxy rather than deleting them.
- **Tests are the spec.** `python tests/test_pipeline.py` runs without any API key
  or browser. Keep it green; extend it when you add a stage.

## Good first tasks for an agent

- Regenerate `BROKERS.md` after any catalogue change.
- Re-scout entries where `status == blocked` once a proxy is configured.
- **Teach the scout to capture `search_url_template` + `listing_link_pattern`** for
  `requires_listing_url` brokers. The applicability gate
  (`stages/applicability.py`) already resolves listings and runs those removals
  automatically when that data is present; right now it's absent, so those ~78
  brokers still route to a human. Populating it is what unlocks them.
- Build the MCP server (Tier 2) exposing: list/search brokers, get opt-out steps,
  scout one domain, run one removal.
