"""databroker.core.config -- one place for paths, models, and runtime knobs."""
from __future__ import annotations
import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(os.environ.get("DATABROKER_ROOT", Path(__file__).resolve().parents[2]))
DATA = ROOT / "data"


@dataclass
class Config:
    # data files
    brokers_yaml: Path = DATA / "brokers.yaml"
    candidates_yaml: Path = DATA / "candidates.yaml"
    osint_log: Path = DATA / "osint_findings.jsonl"
    screenshots_dir: Path = DATA / "screenshots"
    queue_db: Path = DATA / "queue.sqlite"

    # models
    nav_model: str = os.environ.get("NAV_MODEL", "claude-sonnet-4-20250514")
    synth_model: str = os.environ.get("SYNTH_MODEL", "claude-sonnet-4-20250514")
    anthropic_key: str = os.environ.get("ANTHROPIC_API_KEY", "")

    # behavior
    rescout_ttl_days: int = int(os.environ.get("RESCOUT_TTL_DAYS", "90"))
    scout_concurrency: int = int(os.environ.get("SCOUT_CONCURRENCY", "3"))
    remove_concurrency: int = int(os.environ.get("REMOVE_CONCURRENCY", "5"))
    per_domain_concurrency: int = 1  # never hammer one broker

    # proxies (for blocked/anti-bot sites) -- list of proxy URLs, rotated
    proxies: list = field(default_factory=lambda: [
        p for p in os.environ.get("PROXIES", "").split(",") if p.strip()])

    # optional OSINT keys (free sources work without these)
    securitytrails_key: str = os.environ.get("SECURITYTRAILS_KEY", "")

    # set True to use Claude Code as the browser navigator + synthesizer
    claude_code: bool = False

    def ensure_dirs(self):
        self.screenshots_dir.mkdir(parents=True, exist_ok=True)
        DATA.mkdir(parents=True, exist_ok=True)


CONFIG = Config()
