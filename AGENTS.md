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
- Entry point: `moss` → `moss.cli.main:main` (single script; `moss onto` is a subcommand registered from `cli/onto_cmd.py`).
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
pytest tests/test_ontology_repo.py                # one of two test modules
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

The CLI surface is wider than the list above — run `moss --help` before assuming a command doesn't exist. Notable others: `migrate`, `runtime`, `meeting`, `show`, `compile`, `checkpoint`, `alert`, `status`, `brief`, `attention`, `hermit`; command groups `connect` (email source), `skills` (list/load/remove), and `org` (list/show the layered organizational vocabulary — universal positions + industry archetypes). `moss onto` also has `validate | show | commit | log | diff | tag | rollback | conformance` — prefer `moss onto validate` after editing the DSL.

**Company archetype**: set `MOSS_ARCHETYPE` (workspace `.env`; see `moss org list` for ids: pharma/hospital/battery/internet-med) to activate industry-specialized defined classes. Without it MOSS runs in universal-governance mode — industry classes stay silent, role-alignment still works against the universal position layer. Switching company = switching this one variable.

There is **no lint, typecheck, or formatter config** in this repo (no ruff/mypy config files; pyproject `[dev]` only pulls pytest + pytest-cov). CONTRIBUTING.md mentions ruff/mypy aspirationally — they are not wired up. Do not claim a lint/typecheck gate exists.

CI: `.github/workflows/ci.yml` — matrix (Python 3.10/3.12 × java installed/none × langchain installed/none, 6 jobs) + DSL static-check job. The `java=installed` jobs are the only place `test_hermit_matches_fallback` actually runs; `java=none` jobs guard the degradation matrix. Local verification remains `pytest`.

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
tests/                    # pytest — two modules (see Test quirks)
demo/                     # scripted demo (simulated timeline, not real dates)
```

Untracked local dirs `bisai/`, `.omo/`, `.workbuddy/`, `.zcode/` are gitignored work areas — not part of the `moss` package. Exclude them from searches. `data/` is the dev-mode runtime workspace (repo root has a `.env`) — its contents are regenerable.

## Editing rules

- **Engine layer is frozen**: `compiler.py`, `runtime.py`, `engine.py`, `owl_ontology.py`, `owl_compiler.py`, `models.py` are the product core. Do not modify them to accommodate open-sourcing. Extend instead:
  - new data source → add a `Source` subclass under `sources/`
  - new extraction scene → add `SKILL.md` in `src/moss/skills/<name>/` (bundled) or `<workspace>/data/skills/<name>/` (user-local) — the only two paths `skills.py:load_skills_from_disk()` scans. YAML frontmatter + System Prompt + Extraction Rules; auto-loaded on restart. Registration filter: must carry the governance-extraction type marker and a system prompt > 50 chars.
  - new LLM backend → adapt `llm_client.py`'s `chat()` interface
- **Degradation is mandatory**: every optional dependency must degrade gracefully — no langchain → `Extractor`; no Java → pure-Python reasoning; no Ollama → demo preset data; no LLM → no extraction, compile only. Any new optional dep must keep this property. To test degradation: `pip uninstall langchain langgraph -y && pytest` then reinstall.
- **Ontology is code, not hand-written OWL**: the DSL `src/moss/resources/ontology/moss.onto.yaml` is the single source of truth. Edit the DSL, run `moss onto build`. Do not hand-edit generated `.owl`/`.ttl`.
- **Cross-platform**: no `fcntl` (Unix-only; SQLite WAL handles concurrency); no `Path(__file__).parent.parent` (breaks pip-installed usage); use `pathlib.Path`, not string concatenation.
- **Two reasoning paths must not drift**: the HermiT path and the pure-Python fallback must classify identically. `test_hermit_matches_fallback` guards this (skipped without Java). If you change OWL defined classes, update the fallback in `owl_ontology.py` in the same change.

## Test quirks

- Three test modules:
  - `tests/test_ontology_repo.py` — regression guard for the Ontology Repo pillar: DSL validity, protocol conformance (must be all-green), HermiT↔fallback semantic equivalence.
  - `tests/test_compute_priority.py` — branch/boundary tests for `compute_priority` (P0–P4). Time is frozen via a module-level `NOW` constant; priority is computed from governance state, never set directly.
  - `tests/test_scenarios.py` — scenario simulations: 12 governance scenarios × 5-8 rounds of meeting/email interactions, each round = a source document (comment) + its deterministic extraction (ParsedResult/add_update) driven through the real engine→reasoner→compiler→runtime pipeline. Forces the Python-fallback reasoner via an autouse fixture; time is day-scale offsets from real `now`, and "passage of time" is simulated by backdating `last_update_at`/`created_at` then `save()`.
- `test_fallback_matches_defined_semantics` works by monkeypatching module globals on `moss.owl_ontology`: `_java_checked=True`, `_java_available=False`, `_onto_instance=None`. If you refactor those module-level names, update the test in the same change.
- `test_hermit_matches_fallback` is `skipif` on `shutil.which("java")`. CI without Java will silently skip it.

## Commit + release

Commit format: `<type>: <description>` where type ∈ `feat | fix | refactor | docs | test | chore`.

Package version lives ONLY in `src/moss/__init__.py` (`__version__`); `pyproject.toml` reads it dynamically and the CLI `--version` imports it. Bump one place, not three.

Release (per CONTRIBUTING): bump `__version__` → update CHANGELOG (referenced by CONTRIBUTING but not yet in repo — create it) → tag `vX.Y.Z` → `python -m build && twine upload dist/*`. Confirm with the user before doing any of this.

## Licensing (respect when adding files)

Layered: code is MIT (`src/moss/` code, `tests/`, `demo/`); protocol specs under `src/moss/resources/ontology/` are CC BY 4.0, and their authoritative source is the OntologyOps repo. Do not move MIT code into the `resources/ontology/` directory or vice versa.
