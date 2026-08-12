# AGENTS.md

Guidance for AI agents working in this repo. Read before editing. Verify commands against the repo if anything looks stale.

## What this is

MOSS — a Python package (`moss`) implementing an OWL 2 DL + HermiT decision-governance runtime. It is the reference implementation of the [OntologyOps](https://github.com/georgewangchn/OntologyOps) methodology. Prototype, not a shipped product.

Architectural invariant (do not break): **LLM only extracts; the OWL reasoner judges.** LLM never enters the reasoning chain. Priority (P0–P4) and `GovernedDecision` classification are computed by rules/OWL, never set by humans or LLMs.

## Setup

```bash
python -m venv venv && source venv/bin/activate
pip install -e ".[agent,dev]"   # full dev install
# minimal (no LLM deps): pip install -e .
```

- Python 3.10+ required.
- Optional, not installed by pip: JDK 11+ (auto-detected at runtime for HermiT reasoning), Ollama or any OpenAI-compatible API for LLM extraction.
- Entry points (aliases of the same function): `moss` and `moss` → `moss.cli.main:main`.
- Default LLM target is local Ollama (`qwen2.5:7b` at `localhost:11434`). Override via `.env` (see `.env.example`): `LLM_API_URL`, `LLM_API_KEY`, `LLM_MODEL`, `APP_HOST`, `APP_PORT`, `DEBUG`.

## Workspace resolution (non-obvious — read this)

`moss/config.py` resolves the workspace in this priority order; first match wins:

1. `MOSS_HOME` env var (explicit override)
2. `~/.moss/` (default for pip-installed usage)
3. CWD if it contains a `.env` (dev mode)

All runtime data (SQLite DB, `events.jsonl`, ontology build artifacts) lives under `<workspace>/data/`. Never use `Path(__file__).parent.parent` to locate data — always go through `get_workspace()` / `get_config().data_path()`. The repo's own `data/` is gitignored and is only used because the repo root has a `.env`.

## Commands

```bash
pytest                                            # all tests
pytest tests/test_ontology_repo.py                # the only test module
pytest tests/test_ontology_repo.py::test_dsl_validates   # single test
pytest --cov=moss                        # coverage

bash demo/run.sh                                  # interactive demo (~10 min)
python demo/scenario.py all                       # non-interactive: init → week1-4 → final

moss init        # interactive workspace wizard
moss demo        # load seed data
moss serve       # web dashboard on APP_PORT (default 8000)
moss onto build  # compile ontology DSL → OWL/Turtle
moss reset       # wipe governance data (destructive — confirm with user)
```

There is **no lint, typecheck, or formatter config** in this repo (no ruff/mypy config files; pyproject `[dev]` only pulls pytest + pytest-cov). CONTRIBUTING.md mentions ruff/mypy aspirationally — they are not wired up. Do not claim a lint/typecheck gate exists.

There is **no CI** (`.github/workflows/` absent). Verification = running `pytest` locally.

## Project layout

```
src/moss/        # sole package (src layout)
  config.py               # workspace resolution — see above
  storage.py              # SQLite (WAL) backend
  models.py               # Decision / Issue / Update / GovernanceGraph; compute_priority
  compiler.py             # completeness checks (ERROR if no owner/deadline)
  runtime.py              # heartbeat / alerts / auto-escalation
  engine.py               # GovernanceEngine: CRUD + dependency graph + EventBus
  extractor.py            # baseline LLM extractor (httpx direct)
  agent_extractor.py      # LangGraph agent (optional, lazy import)
  owl_ontology.py         # OWL 2 DL + HermiT; falls back to pure-Python rules
  owl_compiler.py         # DSL → owlready2 TBox
  intent_agent.py llm_client.py prompts.py skills.py
  cli/                    # main.py + onto_cmd.py
  web/                    # FastAPI server + SSE event bus + static/
  ontorepo/               # Ontology Repo pillar (OntologyOps reference impl)
  sources/                # data source adapters (base / file / email)
  commands/               # CLI subcommands (init wizard + demo seed)
  resources/ontology/     # DSL + schema + PROTOCOL.md (shipped as package data)
tests/                    # pytest — currently a single module
demo/                     # scripted demo (simulated timeline, not real dates)
```

## Editing rules

- **Engine layer is frozen**: `compiler.py`, `runtime.py`, `engine.py`, `owl_ontology.py`, `owl_compiler.py`, `models.py` are the product core. Do not modify them to accommodate open-sourcing. Extend instead:
  - new data source → add a `Source` subclass under `sources/`
  - new extraction scene → add `skills/<name>/SKILL.md` (YAML frontmatter + System Prompt + Extraction Rules; auto-loaded on restart)
  - new LLM backend → adapt `llm_client.py`'s `chat()` interface
- **Degradation is mandatory**: every optional dependency must degrade gracefully — no langchain → `Extractor`; no Java → pure-Python reasoning; no Ollama → demo preset data; no LLM → no extraction, compile only. Any new optional dep must keep this property. To test degradation: `pip uninstall langchain langgraph -y && pytest tests/test_extractor.py` then reinstall.
- **Ontology is code, not hand-written OWL**: the DSL `src/moss/resources/ontology/moss.onto.yaml` is the single source of truth. Edit the DSL, run `moss onto build`. Do not hand-edit generated `.owl`/`.ttl`.
- **Cross-platform**: no `fcntl` (Unix-only; SQLite WAL handles concurrency); no `Path(__file__).parent.parent` (breaks pip-installed usage); use `pathlib.Path`, not string concatenation.
- **Two reasoning paths must not drift**: the HermiT path and the pure-Python fallback must classify identically. `test_hermit_matches_fallback` guards this (skipped without Java). If you change OWL defined classes, update the fallback in `owl_ontology.py` in the same change.

## Test quirks

- Only `tests/test_ontology_repo.py` exists. It is the regression guard for the Ontology Repo pillar: DSL validity, protocol conformance (must be all-green), and HermiT↔fallback semantic equivalence.
- `test_fallback_matches_defined_semantics` works by monkeypatching module globals on `moss.owl_ontology`: `_java_checked=True`, `_java_available=False`, `_onto_instance=None`. If you refactor those module-level names, update the test in the same change.
- `test_hermit_matches_fallback` is `skipif` on `shutil.which("java")`. CI without Java will silently skip it.

## Commit + release

Commit format: `<type>: <description>` where type ∈ `feat | fix | refactor | docs | test | chore`.

Release (per CONTRIBUTING): bump `pyproject.toml` version → update CHANGELOG → tag `vX.Y.Z` → `python -m build && twine upload dist/*`. Confirm with the user before doing any of this.

## Licensing (respect when adding files)

Layered: code is MIT (`src/moss/` code, `tests/`, `demo/`); protocol specs under `src/moss/resources/ontology/` are CC BY 4.0, and their authoritative source is the OntologyOps repo. Do not move MIT code into the `resources/ontology/` directory or vice versa.
