"""Configuration loading for GovernanceOps.

Config resolution order (first match wins):
  1. GOVOPS_HOME env var (explicit override)
  2. ~/.governance-ops/ (user home directory — default for pip-installed usage)
  3. ./.env in CWD (project-root mode for development)

This ensures the package works after `pip install` without relying on
__file__ paths inside site-packages.
"""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

# ── Workspace resolution ──────────────────────────────────────


def _resolve_workspace() -> Path:
    """Resolve the GovernanceOps workspace directory.

    Priority:
      1. GOVOPS_HOME env var
      2. ~/.governance-ops/
      3. CWD (if .env exists here — dev mode)
    """
    # 1. Explicit override
    env_home = os.getenv("GOVOPS_HOME")
    if env_home:
        p = Path(env_home).expanduser().resolve()
        p.mkdir(parents=True, exist_ok=True)
        return p

    # 2. CWD has .env → dev mode (backward compatibility)
    cwd_env = Path.cwd() / ".env"
    if cwd_env.exists():
        return Path.cwd()

    # 3. Default: user home
    home = Path.home() / ".governance-ops"
    home.mkdir(parents=True, exist_ok=True)
    return home


WORKSPACE: Path = _resolve_workspace()

# Load .env from workspace (if exists)
_ENV_PATH = WORKSPACE / ".env"
load_dotenv(_ENV_PATH)


@dataclass
class LLMConfig:
    """LLM API configuration."""
    api_url: str
    api_key: str
    model: str


@dataclass
class AppConfig:
    """Application server configuration."""
    host: str
    port: int
    debug: bool


@dataclass
class Config:
    """Top-level configuration aggregating all settings."""
    llm: LLMConfig
    app: AppConfig
    workspace: Path = WORKSPACE
    data_dir: str = "data"

    def data_path(self) -> Path:
        """Resolve data directory under workspace."""
        p = Path(self.data_dir)
        if p.is_absolute():
            return p
        return self.workspace / p


_config_cache: Optional[Config] = None


def load_config() -> Config:
    """Load configuration from environment variables.

    Reads from .env in the resolved workspace, with sensible defaults
    that work out-of-the-box after pip install.
    """
    # Default LLM: local Ollama (zero external dependency)
    llm = LLMConfig(
        api_url=os.getenv("LLM_API_URL", "http://localhost:11434/v1/chat/completions"),
        api_key=os.getenv("LLM_API_KEY", "empty"),
        model=os.getenv("LLM_MODEL", "qwen2.5:7b"),
    )
    # Normalize: if URL is base path (ends with /v1), append /chat/completions
    if llm.api_url.endswith("/v1"):
        llm.api_url += "/chat/completions"

    app = AppConfig(
        host=os.getenv("APP_HOST", "0.0.0.0"),
        port=int(os.getenv("APP_PORT", "8000")),
        debug=os.getenv("DEBUG", "false").lower() in ("true", "1", "yes"),
    )

    cfg = Config(llm=llm, app=app)
    # Ensure data directory exists
    cfg.data_path().mkdir(parents=True, exist_ok=True)
    return cfg


def get_config() -> Config:
    """Get cached configuration (singleton pattern)."""
    global _config_cache
    if _config_cache is None:
        _config_cache = load_config()
    return _config_cache


def get_workspace() -> Path:
    """Return the resolved workspace directory."""
    return WORKSPACE
