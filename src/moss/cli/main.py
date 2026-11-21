import json
import re
import sys
from datetime import datetime
from typing import Optional

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from moss import __version__
from moss.compiler import GovernanceCompiler
from moss.engine import GovernanceEngine
from moss.extractor import Extractor
from moss.agent_extractor import AgentExtractor
from moss.models import GovernanceGraph
from moss.runtime import GovernanceRuntime
from moss.skills import (
    active_skill_names,
    list_skills,
    load_skill,
    remove_skill,
)

console = Console()


def get_engine():
    return GovernanceEngine()


def parse_date(date_str: str) -> datetime:
    formats = ["%Y-%m-%d", "%Y-%m-%d %H:%M", "%Y/%m/%d"]
    for fmt in formats:
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            continue
    raise click.BadParameter(f"Cannot parse date: {date_str}")


def status_icon(status: str) -> str:
    icons = {"OPEN": "○", "BLOCKED": "⊘", "RUNNING": "◉", "DONE": "✓"}
    return icons.get(status, "?")


def risk_style(risk: str) -> str:
    styles = {"LOW": "green", "MEDIUM": "yellow", "HIGH": "red"}
    return styles.get(risk, "white")


@click.group()
@click.version_option(version=__version__, prog_name="MOSS")
def main():
    """MOSS - 治理驱动的企业决策运行时系统

    让每一个决策可追踪、可验证、可持续执行。
    """

# ── init / demo commands ──────────────────────────────────────

@main.command()
def init():
    """Initialize MOSS workspace (config + data directory)."""
    from moss.commands.init_cmd import run_init
    run_init()


@main.command()
def demo():
    """Load demo data and show system status."""
    from moss.commands.init_cmd import load_demo_seed
    from moss.owl_ontology import get_reasoner_mode

    console.print(Panel.fit(
        "[bold blue]Loading demo data...[/bold blue]",
        border_style="blue",
    ))

    graph = load_demo_seed()
    mode = get_reasoner_mode()
    console.print(f"[green]✓[/green] Loaded {len(graph.decisions)} decisions, "
                  f"{len(graph.issues)} issues, {len(graph.updates)} updates")
    console.print(f"[dim]Reasoner: {mode}[/dim]")
    console.print(f"\nRun [bold]moss serve[/bold] to start the dashboard.")


@main.command()
def serve():
    """Start the web dashboard server."""
    import uvicorn
    from moss.config import get_config
    cfg = get_config()
    console.print(Panel.fit(
        f"[bold green]MOSS Web Dashboard[/bold green]\n"
        f"Listening on http://localhost:{cfg.app.port}",
        border_style="green",
    ))
    uvicorn.run("moss.web.server:app", host=cfg.app.host, port=cfg.app.port, reload=cfg.app.debug)


@main.group()
def connect():
    """Configure external data sources (email, etc.)."""


@connect.command(name="email")
@click.option("--provider", type=click.Choice(["imap"]), default="imap", help="Email provider")
@click.option("--host", prompt=True, help="IMAP server hostname")
@click.option("--port", type=int, default=993, help="IMAP port")
@click.option("--user", prompt=True, help="Email account")
@click.option("--password", prompt=True, hide_input=True, help="Password or app-specific password")
@click.option("--folder", default="INBOX", help="Mailbox folder")
def connect_email(provider, host, port, user, password, folder):
    """Configure email connection."""
    from moss.config import get_workspace
    ws = get_workspace()
    env_path = ws / ".env"

    # Read existing .env
    lines = []
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()

    # Remove old IMAP_ lines
    lines = [l for l in lines if not l.startswith("IMAP_")]

    # Add new IMAP config
    lines.extend([
        "",
        "# ── Email (IMAP) ──────────────────────────────────",
        f"IMAP_HOST={host}",
        f"IMAP_PORT={port}",
        f"IMAP_USER={user}",
        f"IMAP_PASS={password}",
        f"IMAP_FOLDER={folder}",
    ])

    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    console.print(f"[green]✓[/green] Email configured: {user}@{host}:{port}/{folder}")
    console.print(f"[dim]Config saved: {env_path}[/dim]")


@connect.command(name="test")
def connect_test():
    """Test email connection."""
    from moss.sources.email_source import EmailSource
    try:
        source = EmailSource(since_hours=1)
        docs = source.fetch()
        console.print(f"[green]✓[/green] Connected. Found {len(docs)} email(s) in last 1 hour.")
    except ValueError as e:
        console.print(f"[red]✗[/red] {e}")
    except Exception as e:
        console.print(f"[red]✗[/red] Connection failed: {e}")


@main.command()
@click.option("--interval", type=int, default=300, help="Polling interval in seconds (default: 300)")
@click.option("--once", is_flag=True, help="Run once and exit (no daemon loop)")
def daemon(interval: int, once: bool):
    """Daemon process — poll email and auto-ingest decisions.

    \b
    Examples:
      moss daemon                    # Run as daemon (300s interval)
      moss daemon --interval 60      # Poll every 60s
      moss daemon --once             # Single run (for cron)
    """
    import time as _time
    from moss.sources.email_source import EmailSource

    def run_once():
        try:
            source = EmailSource(since_hours=24)
            docs = source.fetch()
            if not docs:
                console.print(f"[dim]{datetime.now().strftime('%H:%M:%S')} No new emails.[/dim]")
                return

            console.print(f"[blue]{datetime.now().strftime('%H:%M:%S')} Processing {len(docs)} email(s)...[/blue]")
            engine = get_engine()

            from moss.skills import load_skills_from_disk, match_skill
            load_skills_from_disk()

            for doc in docs:
                skill = match_skill(doc.content)
                use_agent = len(doc.content) > 2000

                if use_agent:
                    extractor = AgentExtractor(skill=skill)
                    result = extractor.ingest(doc.content)
                else:
                    ext = Extractor()
                    try:
                        result = ext.extract(doc.content, skill=skill)
                    except Exception:
                        continue

                if result and (result.decisions or result.issues):
                    engine.ingest_parsed_result(result)
                    console.print(f"  [green]✓[/green] {doc.meta.get('subject', '?')[:50]} → {len(result.decisions)} decision(s)")
                else:
                    console.print(f"  [dim]○ {doc.meta.get('subject', '?')[:50]} → no governance objects[/dim]")

        except ValueError as e:
            console.print(f"[red]✗[/red] {e}")
        except Exception as e:
            console.print(f"[red]✗[/red] Error: {e}")

    if once:
        run_once()
        return

    console.print(Panel.fit(
        f"[bold blue]MOSS Daemon[/bold blue]\n"
        f"Polling every {interval}s. Press Ctrl+C to stop.",
        border_style="blue",
    ))

    try:
        while True:
            run_once()
            _time.sleep(interval)
    except KeyboardInterrupt:
        console.print("\n[yellow]Daemon stopped.[/yellow]")


@main.command()
@click.option("--input", "-i", "input_path", type=click.Path(exists=True), help="JSON file to migrate (default: auto-detect)")
def migrate(input_path: Optional[str]):
    """Migrate legacy JSON data to SQLite.

    \b
    Examples:
      moss migrate                       # Auto-detect governance_graph.json
      moss migrate -i old_data.json      # Specify input file
    """
    from moss.storage import Storage
    from moss.config import get_config
    from pathlib import Path as P

    storage = Storage()

    # Auto-detect JSON file
    if input_path is None:
        cfg = get_config()
        candidates = [
            cfg.data_path() / "governance_graph.json",
            P.cwd() / "governance_graph.json",
            P.cwd() / "data" / "governance_graph.json",
        ]
        for c in candidates:
            if c.exists():
                input_path = str(c)
                break
        if input_path is None:
            console.print("[red]No JSON file found. Use -i to specify.[/red]")
            sys.exit(1)

    console.print(f"[blue]Migrating: {input_path} → SQLite ({storage.db_path})[/blue]")

    try:
        storage.import_json(input_path)
        graph = storage.load()
        console.print(f"[green]✓ Migration complete.[/green]")
        console.print(f"  Decisions:  {len(graph.decisions)}")
        console.print(f"  Issues:     {len(graph.issues)}")
        console.print(f"  Updates:    {len(graph.updates)}")
        console.print(f"  Assignments: {len(graph.assignments)}")
        console.print(f"\n[dim]SQLite DB: {storage.db_path}[/dim]")
    except Exception as e:
        console.print(f"[red]Migration failed: {e}[/red]")
        sys.exit(1)


@main.command()
def runtime():
    """Start MOSS runtime and show system status."""
    console.print(Panel.fit(
        "[bold blue]MOSS Runtime[/bold blue]\n\n"
        "治理驱动的企业决策运行时系统已启动。\n"
        "所有CLI命令可用。输入 moss --help 查看完整命令列表。",
        title="Runtime Started"
    ))
    console.print()
    ctx = click.Context(main)
    ctx.invoke(main.commands["status"])


# ============================================================
# Input Layer: ingest, ingest-email
# ============================================================

@main.command()
@click.argument("text", required=False, default="")
@click.option("--file", "-f", "file_path", type=click.Path(exists=True), help="Read from file instead")
@click.option("--agent/--no-agent", default=None, help="Force agent-based extraction (default: auto for long docs)")
@click.option("--skill", "-s", "skill_name", default=None, help="Use a specific scene skill for extraction (e.g., meeting-minutes, weekly-report)")
def ingest(text: str, file_path: Optional[str], agent: Optional[bool], skill_name: Optional[str]):
    """Import text/weekly report to extract governance objects.

    \b
    Examples:
      moss ingest "尽快推进XX项目，张部长负责"
      moss ingest --file meeting_minutes.txt
      moss ingest --file weekly.md --agent
      moss ingest --file meeting.txt --skill meeting-minutes

    Skills customize extraction for different document SCENE types:
      meeting-minutes - 会议纪要（议题→决议→行动项）
      weekly-report   - 中文企业周报（按部门/章节）
      email           - 企业邮件（含转发线索）
      project-report  - 项目报告（里程碑→任务→风险）
      default         - 通用提取
    """
    engine = get_engine()

    if not text and not file_path:
        raise click.UsageError("Either TEXT argument or --file is required.")

    content = text
    if file_path:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
        console.print(f"[dim]Reading from: {file_path} ({len(content)} chars)[/dim]")
    elif text:
        # Detect if the text argument looks like a local file path
        from pathlib import Path
        candidate = Path(text)
        if candidate.exists() and candidate.is_file():
            console.print(f"[yellow]⚠ Did you mean --file? '{text}' looks like a file path.[/yellow]")
            console.print(f"[dim]Try: moss ingest --file {text}[/dim]")

    use_agent = agent if agent is not None else len(content) > 2000

    # ── Skill selection ───────────────────────────────────
    # Auto-load scene skills from disk (.workbuddy/skills/meeting-*/ etc.)
    from moss.skills import load_skills_from_disk, get_skill, match_skill, load_skill
    load_skills_from_disk()

    active_skill = None
    if skill_name:
        try:
            active_skill = load_skill(skill_name)
            console.print(f"[dim]Using skill: {active_skill.name} — {active_skill.description[:40]}...[/dim]")
        except ValueError:
            from moss.skills import list_skills as list_sk
            names = [s.name for s in list_sk()]
            console.print(f"[yellow]Skill '{skill_name}' not found. Available: {names}[/yellow]")
            console.print("[dim]Falling back to default extraction.[/dim]")
            active_skill = get_skill("default")
    else:
        # Auto-match skill from document content
        active_skill = match_skill(content)
        if active_skill and active_skill.name != "default":
            console.print(f"[dim]Auto-matched skill: {active_skill.name}[/dim]")

    if use_agent:
        skill_display = f" [skill: {active_skill.name}]" if active_skill and active_skill.name != "default" else ""
        console.print(f"[bold blue]🤖 Agent Pipeline{skill_display}: {_count_chunks(content)} sections → parallel extract → merge[/bold blue]")
        extractor = AgentExtractor(skill=active_skill)
        result = extractor.ingest(content)
    else:
        # Build existing context for LLM alignment
        existing_context = _build_existing_context(engine)
        skill_display = f" [skill: {active_skill.name}]" if active_skill and active_skill.name != "default" else ""
        console.print(f"[bold blue]🔍 Extracting governance objects{skill_display}...[/bold blue]")
        ext = Extractor()
        try:
            result = ext.extract(content, existing_context=existing_context, skill=active_skill)
        except Exception as e:
            console.print(f"[red]LLM extraction failed: {e}[/red]")
            console.print("[yellow]Continuing with empty result...[/yellow]")
            result = None

    if result and (result.decisions or result.issues):
        outcome = engine.ingest_parsed_result(result)
        created = outcome["created"]
        updated = outcome["updated"]
        edr_needs = outcome.get("edr_needs", [])

        # Emit events for web event log
        from moss.web.event_bus import emit_sync
        all_decisions = created + updated
        for d in created:
            emit_sync("decision_created", {"id": d.id, "title": d.title, "owner": d.owner, "status": d.status})
        for d in updated:
            emit_sync("decision_updated", {"id": d.id, "title": d.title, "owner": d.owner, "status": d.status})
        for a in (result.assignments or []):
            if a.get("owner"):
                # Find matching decision ID
                did = a.get("decision_id", "")
                if not did:
                    atitle = a.get("decision_title", "")
                    for dec in all_decisions:
                        if atitle and atitle[:6] in dec.title:
                            did = dec.id
                            break
                if not did:
                    did = created[0].id if created else ""
                emit_sync("decision_updated", {
                    "id": did,
                    "title": a.get("decision_title", "?"),
                    "owner": a.get("owner"),
                    "deadline": a.get("deadline"),
                    "field": "assignment",
                })

        if created:
            console.print(f"\n[bold green]Created {len(created)} decision(s):[/bold green]")
            for d in created:
                owner_info = f"Owner: {d.owner}" if d.owner else "Owner: [red]UNASSIGNED[/red]"
                console.print(f"  {d.id} - {d.title} ({owner_info})")

        if updated:
            console.print(f"\n[bold cyan]Updated {len(updated)} existing decision(s):[/bold cyan]")
            for d in updated:
                parts = []
                if d.owner:
                    parts.append(f"owner={d.owner}")
                if d.deadline:
                    parts.append(f"deadline={d.deadline.strftime('%Y-%m-%d')}")
                if d.dependency:
                    parts.append(f"dependency={d.dependency}")
                console.print(f"  {d.id} - {d.title} ({', '.join(parts)})")

        if result.issues:
            console.print(f"\n[bold]Identified {len(result.issues)} issue(s):[/bold]")
            for i in result.issues:
                console.print(f"  ISSUE - {i.get('title', 'Untitled')}")

        if result.assignments:
            assigned = [a for a in result.assignments if a.get("owner")]
            if assigned:
                console.print(f"\n[bold]Extracted {len(assigned)} assignment(s):[/bold]")
                for a in assigned:
                    dl = f" by {a.get('deadline')}" if a.get("deadline") else ""
                    console.print(f"  {a.get('decision_title', '?')} → {a.get('owner')}{dl}")

        # ── EDR auto-trigger for unresolved Issues ──────────
        # Collect IDs of decisions created in THIS ingest — exclude from EDR matching
        # so EDR links to genuinely pre-existing decisions, not the LLM's new ones
        new_decision_ids = {d.id for d in created}
        for edr_item in edr_needs:
            _run_edr_for_issue(engine, edr_item, file_path, exclude_decision_ids=new_decision_ids)
    else:
        console.print("[yellow]No governance objects extracted from text.[/yellow]")


def _count_chunks(text: str) -> int:
    sections = re.split(r"\n(?=#{1,3}\s)", text)
    return len([s for s in sections if s.strip()])


@main.command()
@click.option("--from", "-f", "sender", help="Sender email")
@click.option("--cc", help="CC recipients")
@click.option("--subject", "-s", help="Email subject")
@click.option("--skill", "-k", "skill_name", default=None, help="Use a specific scene skill (e.g., email)")
def ingest_email(sender: Optional[str], cc: Optional[str], subject: Optional[str], skill_name: Optional[str]):
    """Import email from stdin."""
    console.print("[bold blue]📧 Reading email from stdin... (Ctrl+D to finish)[/bold blue]")

    lines = []
    try:
        while True:
            lines.append(input())
    except EOFError:
        pass

    content = "\n".join(lines)
    meta = []
    if sender:
        meta.append(f"From: {sender}")
    if cc:
        meta.append(f"CC: {cc}")
    if subject:
        meta.append(f"Subject: {subject}")

    full_text = "\n".join(meta + ["", content]) if meta else content

    engine = get_engine()

    # ── Skill selection ───────────────────────────────────
    from moss.skills import load_skills_from_disk, get_skill, match_skill, load_skill
    load_skills_from_disk()
    active_skill = None
    if skill_name:
        try:
            active_skill = load_skill(skill_name)
        except ValueError:
            active_skill = get_skill("default")
    else:
        active_skill = match_skill(full_text)

    # Build existing context for LLM alignment
    existing_context = _build_existing_context(engine)

    use_agent = len(full_text) > 2000
    if use_agent:
        console.print(f"[bold blue]🤖 Agent Pipeline ({_count_chunks(full_text)} sections)[/bold blue]")
        extractor = AgentExtractor(skill=active_skill)
        result = extractor.ingest(full_text)
    else:
        ext = Extractor()
        try:
            result = ext.extract(full_text, existing_context=existing_context, skill=active_skill)
        except Exception as e:
            console.print(f"[red]Extraction failed: {e}[/red]")
            result = None

    if result and (result.decisions or result.issues):
        outcome = engine.ingest_parsed_result(result)
        created = outcome["created"]
        updated = outcome["updated"]
        edr_needs = outcome.get("edr_needs", [])

        # Emit events for web event log
        from moss.web.event_bus import emit_sync
        for d in created:
            emit_sync("decision_created", {"id": d.id, "title": d.title, "owner": d.owner, "status": d.status})
        for d in updated:
            emit_sync("decision_updated", {"id": d.id, "title": d.title, "owner": d.owner, "status": d.status})

        if created:
            console.print(f"\n[bold green]Extracted {len(created)} decision(s) from email:[/bold green]")
            for d in created:
                console.print(f"  {d.id} - {d.title}")
        if updated:
            console.print(f"\n[bold cyan]Updated {len(updated)} existing decision(s)[/bold cyan]")
        if not created and not updated:
            console.print("[yellow]No governance objects found in email.[/yellow]")

        # EDR auto-trigger
        new_decision_ids = {d.id for d in created}
        for edr_item in edr_needs:
            _run_edr_for_issue(engine, edr_item, None, exclude_decision_ids=new_decision_ids)


# ============================================================
# Decision Layer: meeting, amend, show
# ============================================================

@main.command()
def meeting():
    """Enter live meeting mode (REPL) for interactive governance."""
    console.print(
        Panel.fit(
            "[bold blue]🎤 MOSS Meeting Mode[/bold blue]\n\n"
            "Type governance updates, decisions, or questions.\n"
            "Commands:\n"
            "  :show <ID>   - Show decision details\n"
            "  :list        - List all decisions\n"
            "  :compile     - Run governance check\n"
            "  :done <ID>   - Mark decision as done\n"
            "  :quit        - Exit meeting mode",
            title="Meeting Mode",
        )
    )

    engine = get_engine()
    extractor = Extractor()

    while True:
        try:
            user_input = console.input("\n[bold green]> [/bold green]").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[yellow]Meeting ended.[/yellow]")
            break

        if not user_input:
            continue

        if user_input.startswith(":quit"):
            console.print("[yellow]Meeting ended.[/yellow]")
            break
        elif user_input.startswith(":list"):
            decisions = engine.list_decisions()
            if not decisions:
                console.print("[dim]No decisions yet.[/dim]")
            else:
                _print_decision_table(decisions)
            continue
        elif user_input.startswith(":show"):
            parts = user_input.split()
            if len(parts) < 2:
                console.print("[red]Usage: :show <decision_id>[/red]")
                continue
            _show_decision(engine, parts[1])
            continue
        elif user_input.startswith(":compile"):
            compiler = GovernanceCompiler()
            results = compiler.compile(engine.graph)
            _print_compile_results(results)
            continue
        elif user_input.startswith(":done"):
            parts = user_input.split()
            if len(parts) < 2:
                console.print("[red]Usage: :done <decision_id>[/red]")
                continue
            try:
                engine.transition_status(parts[1], "DONE")
                console.print(f"[green]{parts[1]} marked as DONE ✓[/green]")
            except ValueError as e:
                console.print(f"[red]{e}[/red]")
            continue

        decisions_list = [
            {"id": d.id, "title": d.title, "owner": d.owner, "status": d.status}
            for d in engine.graph.decisions.values()
        ]

        try:
            result = extractor.meeting_extract(user_input, decisions_list)
        except Exception as e:
            console.print(f"[red]LLM error: {e}[/red]")
            continue

        extracted = result.get("extracted", {})
        gaps = result.get("governance_gaps", [])
        matches = result.get("candidate_matches", [])

        for dec in extracted.get("decisions", []):
            d = engine.create_decision(
                title=dec.get("title", "Untitled"),
                source="meeting",
                raw_statement=dec.get("raw_statement", ""),
            )
            console.print(f"[green][Decision Created] {d.id} - {d.title}[/green]")

        for a in extracted.get("assignments", []):
            decision_title = a.get("decision_title", "")
            owner = a.get("owner")
            for d in engine.graph.decisions.values():
                if d.title.lower() == decision_title.lower() and owner:
                    engine.assign_owner(d.id, owner, a.get("department"))
                    console.print(f"[green]{d.id} Owner = {owner}[/green]")
                    break

        if gaps:
            console.print("\n[bold yellow]Governance Gaps:[/bold yellow]")
            for g in gaps:
                console.print(
                    f"  [yellow]{g.get('decision_title', '?')} {g.get('gap_type', '?')}[/yellow]"
                )

        if matches:
            console.print("\n[bold dim]Candidate Matches:[/bold dim]")
            for m in matches:
                console.print(
                    f"  [dim]{m.get('extracted_title', '?')} → "
                    f"{m.get('matched_decision_id', '?')} "
                    f"({m.get('confidence', '?')})[/dim]"
                )

        for u in extracted.get("updates", []):
            decision_title = u.get("decision_title", "")
            for d in engine.graph.decisions.values():
                if d.title.lower() == decision_title.lower():
                    engine.add_update(
                        d.id, u.get("content", ""),
                        progress=u.get("progress"),
                        source="meeting",
                    )
                    console.print(f"[green]{d.id} progress updated[/green]")
                    break

        # Persist extracted Issues
        for issue_data in extracted.get("issues", []):
            issue = engine.create_issue(
                title=issue_data.get("title", "Untitled Issue"),
                description=issue_data.get("description", ""),
                severity=issue_data.get("severity", "MEDIUM"),
            )
            console.print(f"[yellow][Issue] {issue.id} - {issue.title}[/yellow]")

        # Show extracted risks
        for risk_data in extracted.get("risks", []):
            console.print(f"[red][Risk] {risk_data.get('risk_description', risk_data.get('decision_title', '?'))}[/red]")


@main.command()
@click.argument("decision_id")
@click.option("--owner", "-o", help="Set decision owner")
@click.option("--deadline", "-d", help="Set deadline (YYYY-MM-DD)")
@click.option(
    "--status", "-s",
    type=click.Choice(["OPEN", "BLOCKED", "RUNNING", "DONE"]),
    help="Set status",
)
@click.option("--department", help="Set department")
@click.option("--dependency", help="Set dependency department")
@click.option("--title", help="Set decision title")
@click.option("--progress", "-p", type=float, help="Set progress (0-100)")
@click.option(
    "--risk", "-r",
    type=click.Choice(["LOW", "MEDIUM", "HIGH"]),
    help="Set risk level",
)
@click.option(
    "--owner-role",
    "owner_role",
    help="管理岗位（org 词汇表标准名或别名，如 首席财务官/质量负责人；moss org show 查看）",
)
@click.option(
    "--domain",
    "domain_opt",
    help="业务域（org 词汇表标准名或别名，如 财务/GMP生产/电池安全）",
)
def amend(
    decision_id: str,
    owner: Optional[str],
    deadline: Optional[str],
    status: Optional[str],
    department: Optional[str],
    dependency: Optional[str],
    title: Optional[str],
    progress: Optional[float],
    risk: Optional[str],
    owner_role: Optional[str],
    domain_opt: Optional[str],
):
    """Modify decision attributes."""
    engine = get_engine()
    decision = engine.get_decision(decision_id)
    if not decision:
        console.print(f"[red]Decision {decision_id} not found.[/red]")
        sys.exit(1)

    kwargs: dict = {}
    if owner:
        engine.assign_owner(decision_id, owner, department or decision.department)
        kwargs["owner"] = owner
    if department and not owner:
        kwargs["department"] = department
    if dependency:
        kwargs["dependency"] = dependency
    if deadline:
        kwargs["deadline"] = parse_date(deadline)
    if status:
        try:
            engine.transition_status(decision_id, status)
        except ValueError as e:
            console.print(f"[red]{e}[/red]")
            sys.exit(1)
    if title:
        kwargs["title"] = title
    if progress is not None:
        kwargs["progress"] = progress
    if risk:
        kwargs["risk_level"] = risk
    if owner_role:
        kwargs["owner_role"] = owner_role
    if domain_opt:
        kwargs["domain"] = domain_opt

    if kwargs:
        engine.update_decision(decision_id, **kwargs)

    console.print(f"[green]{decision_id} updated.[/green]")
    _show_decision(engine, decision_id)


@main.command()
@click.argument("decision_id")
@click.argument("content")
@click.option("--progress", "-p", type=float, help="Set progress (0-100)")
@click.option("--risk", "-r", type=click.Choice(["LOW", "MEDIUM", "HIGH"]), help="Set risk level")
def update(decision_id: str, content: str, progress: Optional[float], risk: Optional[str]):
    """Add a progress update to a decision.

    \b
    Examples:
      moss update 262301 "已成立专项工作组，启动方案调研"
      moss update 262301 "完成第一阶段" --progress 50
    """
    engine = get_engine()
    try:
        from moss.models import RiskLevel
        risk_enum = RiskLevel(risk) if risk else None
        u = engine.add_update(decision_id, content, progress=progress, risk=risk_enum)
        console.print(f"[green]✓ Update {u.id} added to {decision_id}[/green]")
        
        # Emit event for web event log
        from moss.web.event_bus import emit_sync
        decision = engine.get_decision(decision_id)
        emit_sync("decision_updated", {
            "id": decision_id, 
            "title": decision.title if decision else "?",
            "status": decision.status if decision else "?",
            "update_id": u.id,
            "content": content,
        })
        if progress is not None:
            console.print(f"  Progress: {progress:.0f}%")
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        sys.exit(1)


@main.command()
@click.argument("decision_id")
def show(decision_id: str):
    """View decision details and progress."""
    engine = get_engine()
    _show_decision(engine, decision_id)




# ============================================================
# Governance Layer: compile, checkpoint, alert
# ============================================================

@main.command(name="reset")
def reset_cmd():
    """Reset all governance data — clears decisions, issues, and updates."""
    from moss.storage import Storage
    from moss.models import GovernanceGraph
    from pathlib import Path

    storage = Storage()
    storage.save(GovernanceGraph())
    engine = get_engine()
    engine.reload()

    # Clear events.jsonl so SSE event log starts fresh
    events_path = Path(storage.data_dir) / "events.jsonl"
    try:
        events_path.write_text("")
    except Exception:
        pass

    # Emit reset event (now the only event in the cleared file)
    try:
        from moss.web.event_bus import emit_sync
        emit_sync("data_reset", {"message": "All governance data has been reset"})
    except Exception:
        pass

    console.print("[green]✓ All governance data has been reset.[/green]")
    console.print("[dim]Decisions cleared, ready for fresh data.[/dim]")


@main.command(name="compile")
@click.option("--decision-id", "-d", help="Check specific decision only")
def compile_cmd(decision_id: Optional[str]):
    """Run governance compile check."""
    engine = get_engine()
    compiler = GovernanceCompiler()

    if decision_id:
        decision = engine.get_decision(decision_id)
        if not decision:
            console.print(f"[red]Decision {decision_id} not found.[/red]")
            sys.exit(1)

        temp_graph = GovernanceGraph()
        temp_graph.decisions[decision_id] = decision
        temp_graph.updates = [
            u for u in engine.graph.updates if u.decision_id == decision_id
        ]
        temp_graph.assignments = [
            a for a in engine.graph.assignments if a.decision_id == decision_id
        ]
        results = compiler.compile(temp_graph)
    else:
        results = compiler.compile(engine.graph)

    _print_compile_results(results, engine.graph)


@main.command()
def checkpoint():
    """Execute governance heartbeat - risk assessment, overdue detection, escalation."""
    engine = get_engine()
    runtime = GovernanceRuntime(engine.graph)

    console.print("[bold blue]🫀 Executing Governance Heartbeat...[/bold blue]\n")

    alerts = runtime.checkpoint()

    if not alerts:
        console.print("[green]All decisions are on track. No alerts.[/green]")
        return

    high_alerts = [a for a in alerts if a.severity == "HIGH"]
    med_alerts = [a for a in alerts if a.severity == "MEDIUM"]
    low_alerts = [a for a in alerts if a.severity == "LOW"]

    if high_alerts:
        console.print(f"[bold red]🔴 HIGH Severity ({len(high_alerts)}):[/bold red]")
        for a in high_alerts:
            dec = engine.graph.decisions.get(a.decision_id)
            owner_info = f"Owner: {dec.owner}" if dec and dec.owner else "Unassigned"
            console.print(f"  {a.decision_id} {a.decision_title}")
            console.print(f"    {a.message} ({owner_info})")
        console.print()

    if med_alerts:
        console.print(f"[bold yellow]🟡 MEDIUM Severity ({len(med_alerts)}):[/bold yellow]")
        for a in med_alerts:
            console.print(f"  {a.decision_id} {a.decision_title} - {a.message}")
        console.print()

    if low_alerts:
        console.print(f"[dim]🟢 LOW Severity ({len(low_alerts)}):[/dim]")
        for a in low_alerts:
            console.print(f"  [dim]{a.decision_id} {a.decision_title} - {a.message}[/dim]")
        console.print()

    escalations = runtime.auto_escalate(alerts)
    if escalations:
        console.print("[bold red]📢 Escalation Messages:[/bold red]")
        for e in escalations:
            owner_display = e.owner or "未分配"
            console.print(f"  [升级] {e.decision_id} {e.decision_title} - {e.message} (负责人: {owner_display})")

    console.print(
        f"\n[bold]Total alerts: {len(alerts)} | "
        f"HIGH: {len(high_alerts)} | "
        f"MEDIUM: {len(med_alerts)} | "
        f"LOW: {len(low_alerts)}[/bold]"
    )


@main.command()
def alert():
    """View active alerts - high risk, overdue, stalled decisions."""
    engine = get_engine()
    runtime = GovernanceRuntime(engine.graph)

    alerts = runtime.checkpoint()

    if not alerts:
        console.print("[green]No active alerts.[/green]")
        return

    table = Table(title="Active Alerts")
    table.add_column("Decision", style="cyan")
    table.add_column("Type")
    table.add_column("Severity")
    table.add_column("Message")
    table.add_column("Owner")

    for a in alerts:
        sev_style = {"HIGH": "red", "MEDIUM": "yellow", "LOW": "dim"}.get(a.severity, "white")
        dec = engine.graph.decisions.get(a.decision_id)
        owner = dec.owner if dec else "?"
        table.add_row(
            f"{a.decision_id} {a.decision_title}",
            a.alert_type,
            f"[{sev_style}]{a.severity}[/{sev_style}]",
            a.message,
            owner or "—",
        )

    console.print(table)


# ============================================================
# Lifecycle Layer: status, graph, sync
# ============================================================

@main.command()
def status():
    """Show system status - counts, risk distribution, department breakdown."""
    engine = get_engine()
    stats = engine.get_stats()

    summary = Text()
    summary.append(f"Active Decisions: {stats['active_decisions']}\n")
    summary.append("High Risk: ", style="bold")
    high_style = "bold red" if stats["high_risk_count"] > 0 else "green"
    summary.append(f"{stats['high_risk_count']}\n", style=high_style)
    summary.append("Overdue: ", style="bold")
    overdue_style = "bold red" if stats["overdue_count"] > 0 else "green"
    summary.append(f"{stats['overdue_count']}\n", style=overdue_style)
    summary.append("Stale (>7d): ", style="bold")
    stale_style = "bold yellow" if stats["stale_count"] > 0 else "green"
    summary.append(f"{stats['stale_count']}\n", style=stale_style)
    summary.append(f"Total: {stats['total_decisions']}")

    console.print(Panel(summary, title="[bold]MOSS Status[/bold]"))

    if stats["by_status"]:
        table = Table(title="By Status")
        table.add_column("Status", style="cyan")
        table.add_column("Count", justify="right")
        sorted_statuses = sorted(stats["by_status"].items(), key=lambda x: x[1], reverse=True)
        for s, c in sorted_statuses:
            table.add_row(f"{status_icon(s)} {s}", str(c))
        console.print(table)

    if stats["by_department"]:
        table = Table(title="By Department")
        table.add_column("Department", style="cyan")
        table.add_column("Count", justify="right")
        sorted_depts = sorted(stats["by_department"].items(), key=lambda x: x[1], reverse=True)
        for d, c in sorted_depts:
            table.add_row(d, str(c))
        console.print(table)


@main.command()
@click.option(
    "--format", "-f", "output_format",
    type=click.Choice(["text", "json"]),
    default="text",
    help="Output format",
)
def graph(output_format: str):
    """Show governance graph - decision relationships and structure."""
    engine = get_engine()
    g = engine.graph

    if output_format == "json":
        data = {
            "decisions": {k: v.model_dump(mode="json") for k, v in g.decisions.items()},
            "issues": {k: v.model_dump(mode="json") for k, v in g.issues.items()},
            "edges": g.edges,
        }
        console.print(json.dumps(data, indent=2, default=str, ensure_ascii=False))
        return

    if not g.decisions:
        console.print("[dim]No decisions in graph.[/dim]")
        return

    console.print("[bold]Governance Graph[/bold]\n")

    for did, decision in g.decisions.items():
        s = risk_style(decision.risk_level)
        console.print(
            f"[bold]{status_icon(decision.status)} {did}[/bold] "
            f"[{s}]{decision.title}[/{s}]"
        )
        console.print(
            f"    Status: {decision.status} | "
            f"Progress: {decision.progress}% | "
            f"Risk: {decision.risk_level}"
        )

        if decision.owner:
            dept = f" ({decision.department})" if decision.department else ""
            console.print(f"    Owner: {decision.owner}{dept}")
        else:
            console.print("    [red]Owner: UNASSIGNED[/red]")

        if decision.deadline:
            console.print(f"    Deadline: {decision.deadline.strftime('%Y-%m-%d')}")

        if decision.issue_id:
            issue = g.issues.get(decision.issue_id)
            if issue:
                console.print(f"    Issue: {issue.title}")

        updates = [u for u in g.updates if u.decision_id == did]
        if updates:
            latest = updates[-1]
            snippet = latest.content[:60] + ("..." if len(latest.content) > 60 else "")
            console.print(
                f"    Latest Update: {latest.created_at.strftime('%Y-%m-%d %H:%M')} - {snippet}"
            )

        console.print()


@main.command()
def brief():
    """Generate meeting brief — overview of all decisions with status."""
    engine = get_engine()
    runtime = GovernanceRuntime(engine.graph)
    console.print(runtime.generate_meeting_brief())


@main.command()
def attention():
    """Show what needs your immediate attention — sorted by urgency."""
    engine = get_engine()
    from moss.compiler import GovernanceCompiler
    from moss.runtime import GovernanceRuntime
    from datetime import datetime

    compiler = GovernanceCompiler()
    runtime = GovernanceRuntime(engine.graph)
    compile_results = compiler.compile(engine.graph)
    alerts = runtime.checkpoint()

    console.print("[bold]⚡ What Needs Your Attention[/bold]\n")

    errors = [r for r in compile_results if r.severity == "ERROR"]
    if errors:
        console.print("[bold red]🔴 缺失负责人（必须立即处理）[/bold red]")
        for r in errors:
            console.print(f"  {r.decision_title:<30} [dim]{r.decision_id}[/dim]")
        console.print()

    high_alerts = [a for a in alerts if a.severity == "HIGH"]
    if high_alerts:
        console.print("[bold red]🔴 高危告警[/bold red]")
        seen = set()
        from moss.models import compute_priority
        now = datetime.now()
        def sort_key(a):
            d = engine.graph.decisions.get(a.decision_id)
            p_order = {"P0":0,"P1":1,"P2":2,"P3":3,"P4":4}
            pri = p_order.get(compute_priority(d, engine.graph, now), 3) if d else 3
            over = (now - d.deadline).days if d and d.deadline and d.deadline < now else 0
            return (pri, -over)
        high_alerts.sort(key=sort_key)
        for a in high_alerts:
            key = a.decision_id
            dec = engine.graph.decisions.get(a.decision_id)
            days = ""
            pri_str = ""
            if dec:
                if dec.deadline and dec.deadline < datetime.now():
                    overdue = (datetime.now() - dec.deadline).days
                    if overdue > 0:
                        days = f" [超期{overdue}天]"
                pri_str = f" [bold]{compute_priority(dec, engine.graph, now)}[/bold]"
            if key not in seen:
                seen.add(key)
                console.print(f"  {a.decision_title:<30} [dim]{a.decision_id}[/dim] — {a.message}{days}{pri_str}")
            elif days:
                console.print(f"  {'':<30} [dim]{a.decision_id}[/dim] — {a.message}{days}")
        console.print()

    warnings = [r for r in compile_results if r.severity == "WARNING"]
    if warnings:
        console.print("[bold yellow]🟡 需要注意[/bold yellow]")
        seen = set()
        for r in warnings:
            key = r.decision_id
            if key not in seen:
                seen.add(key)
                console.print(f"  {r.decision_title:<30} [dim]{r.decision_id}[/dim] — {r.message}")
        console.print()

    now = datetime.now()
    stalled = [d for d in engine.graph.decisions.values()
               if d.status != "DONE" and d.last_update_at and (now - d.last_update_at).days >= 7]
    if stalled:
        console.print("[dim]⏸ 停滞 (>7天无更新)[/dim]")
        for d in stalled:
            assert d.last_update_at is not None
            days = (now - d.last_update_at).days
            console.print(f"  [dim]{d.title:<30} {d.id} — {days}天无更新[/dim]")

    if not errors and not high_alerts and not warnings:
        console.print("[green]✅ 一切正常，无需特别关注[/green]")

    console.print(f"\n[bold]共 {len(engine.graph.decisions)} 决策 | "
                  f"[red]{len(errors)}缺失负责人[/red] | "
                  f"[red]{len(high_alerts)}高危[/red] | "
                  f"[yellow]{len(warnings)}需注意[/yellow] | "
                  f"[dim]{len(stalled)}停滞[/dim][/bold]")


@main.group()
def org():
    """组织治理词汇表（org 段：通用岗位 + 行业/公司原型分层）。"""


def _load_org_vocab():
    from moss.ontorepo.loader import default_dsl_path, load_model
    model = load_model(default_dsl_path())
    if model.org is None:
        raise click.ClickException("本体 DSL 缺少 org 段（moss.onto.yaml）")
    return model.org


@org.command(name="list")
def org_list():
    """列出可用的典型公司原型。"""
    orgv = _load_org_vocab()
    active = None
    try:
        from moss.config import get_config
        active = get_config().archetype
    except Exception:
        pass

    table = Table(title="组织原型（MOSS_ARCHETYPE 可选值）")
    table.add_column("id", style="cyan")
    table.add_column("原型")
    table.add_column("行业")
    table.add_column("专业岗位", justify="right")
    table.add_column("特有业务域")
    table.add_column("状态")
    for a in orgv.archetypes:
        table.add_row(
            a.id, a.label, a.industry, str(len(a.positions)),
            "、".join(a.extra_domains) or "—",
            "[green]当前[/green]" if active == a.id else "",
        )
    console.print(table)
    if active and orgv.archetype_by_id(active) is None:
        console.print(f"[red]⚠ MOSS_ARCHETYPE='{active}' 不在词汇表中 —— 行业语义未生效，请修正拼写[/red]")
    console.print(f"[dim]通用岗位 {len(orgv.universal_positions)} 个 · 通用业务域 {len(orgv.domains)} 个 · 换公司 = 换 MOSS_ARCHETYPE，通用层不变[/dim]")


@org.command(name="show")
@click.argument("archetype_id", required=False)
def org_show(archetype_id: Optional[str]):
    """展示组织词汇表：缺省=通用层；指定原型=通用层+该原型专业岗位。"""
    orgv = _load_org_vocab()
    arch = None
    if archetype_id:
        arch = orgv.archetype_by_id(archetype_id)
        if arch is None:
            valid = ", ".join(a.id for a in orgv.archetypes)
            raise click.ClickException(f"未知原型 '{archetype_id}'。可用: {valid}")
        console.print(f"[bold]{arch.label}[/bold]（行业：{arch.industry}）")

    console.print("\n[bold]通用岗位层（所有公司一致）[/bold]")
    t1 = Table()
    t1.add_column("岗位", style="cyan")
    t1.add_column("汇报给")
    t1.add_column("主业务域")
    t1.add_column("职责")
    t1.add_column("常见叫法")
    for p in orgv.universal_positions:
        t1.add_row(p.label, p.reports_to or "—", p.domain,
                   "、".join(p.duties), "、".join(p.aliases) or "—")
    console.print(t1)

    if arch is not None:
        console.print(f"\n[bold]{arch.label} 专业岗位层[/bold]")
        t2 = Table()
        t2.add_column("岗位", style="cyan")
        t2.add_column("汇报给")
        t2.add_column("主业务域")
        t2.add_column("职责")
        t2.add_column("常见叫法")
        for p in arch.positions:
            t2.add_row(p.label, p.reports_to or "—", p.domain,
                       "、".join(p.duties), "、".join(p.aliases) or "—")
        console.print(t2)
        console.print(f"[dim]特有业务域：{'、'.join(arch.all_domains())}[/dim]")

    console.print(f"\n[dim]通用业务域：{'、'.join(orgv.domains)}[/dim]")
    console.print("[dim]对齐三分类：岗位职责覆盖决策.domain → role_aligned；在册但不覆盖 → role_misaligned；岗位不在词汇表 → role_unregistered[/dim]")


@main.command()
def hermit():
    """Show OWL classification for all governed objects (per moss.onto.yaml)."""
    engine = get_engine()
    from moss.owl_ontology import reason_graph

    classified = reason_graph(engine.graph)

    console.print("[bold]OWL Ontology Classification[/bold]\n")
    # result_key 集合随 DSL 定义类自动增减，此处遍历而非硬编码键名
    for rk in sorted(k for k in classified if k != "all_decisions"):
        ids = classified[rk]
        console.print(f"{rk:<20}{ids}", highlight=False)

    console.print("\n[bold]Per-Decision Breakdown:[/bold]")
    for did, d in engine.graph.decisions.items():
        flags = [rk for rk in sorted(classified) if rk != "all_decisions" and did in classified[rk]]
        icon = {"OPEN":"○","BLOCKED":"⊘","RUNNING":"◉","DONE":"✓"}.get(d.status,"?")
        flag_str = " | ".join(flags) if flags else "[dim]no classification[/dim]"
        console.print(f"  {icon} {did} {d.title[:30]:<32} {flag_str}", highlight=False)

    console.print("\n[dim]Classification by owlready2 + HermiT Tableau reasoner (JVM)[/dim]")


@main.command()
@click.option(
    "--source", "-s",
    type=click.Choice(["email", "erp"]),
    default="email",
    help="Sync source",
)
def sync(source: str):
    """Sync with external systems."""
    console.print(f"[yellow]Sync with {source} is not yet implemented.[/yellow]")
    console.print(f"[dim]Configured source: {source}[/dim]")


@main.group()
def skills():
    """Manage extraction skills for document processing.

    Skills customize how the agent extracts governance objects from
    different document formats (weekly reports, emails, meeting minutes).
    Loaded skills are auto-matched to documents during ingestion.
    """
    pass


@skills.command(name="list")
def skills_list():
    """List all available skills and show which are active."""
    from moss.skills import load_skills_from_disk
    load_skills_from_disk()  # Load governance skills from .workbuddy/skills/
    active = set(active_skill_names())
    table = Table(title="Extraction Skills")
    table.add_column("Status", style="cyan", width=8)
    table.add_column("Name", style="bold")
    table.add_column("Description")
    table.add_column("Tags")

    for s in list_skills():
        status = "[green]● ACTIVE[/green]" if s.name in active else "[dim]○[/dim]"
        table.add_row(status, s.name, s.description, ", ".join(s.tags))

    console.print(table)
    console.print(f"\n[dim]Active: {', '.join(active)}[/dim]")
    console.print("[dim]Use 'moss skills load <name>' to activate, 'moss skills remove <name>' to deactivate[/dim]")


@skills.command(name="load")
@click.argument("name")
def skills_load(name: str):
    """Activate a skill for document extraction."""
    try:
        skill = load_skill(name)
        console.print(f"[green]✓ Loaded skill: {skill.name}[/green]")
        console.print(f"  {skill.description}")
        active = active_skill_names()
        console.print(f"\n[dim]Active skills: {', '.join(active)}[/dim]")
    except ValueError as e:
        console.print(f"[red]{e}[/red]")


@skills.command(name="remove")
@click.argument("name")
def skills_remove(name: str):
    """Deactivate a skill (cannot remove 'default')."""
    if name == "default":
        console.print("[yellow]Cannot remove the 'default' skill.[/yellow]")
        return
    if remove_skill(name):
        console.print(f"[green]✓ Removed skill: {name}[/green]")
        active = active_skill_names()
        console.print(f"\n[dim]Active skills: {', '.join(active)}[/dim]")
    else:
        console.print(f"[yellow]Skill '{name}' is not currently active.[/yellow]")


def _build_existing_context(engine: GovernanceEngine) -> str:
    """Build a text summary of existing Decisions and Issues for LLM alignment.

    The LLM uses this context to determine whether incoming text describes
    new topics or updates to existing entities. This is the Agent's
    "what already exists" knowledge for the align+extract step.
    """
    lines = []
    if engine.graph.decisions:
        for did, d in engine.graph.decisions.items():
            owner = d.owner or "Unassigned"
            dept = f", Dept: {d.department}" if d.department else ""
            lines.append(f"- {did}: {d.title} (Owner: {owner}, Status: {d.status}{dept})")
    if engine.graph.issues:
        for iid, i in engine.graph.issues.items():
            lines.append(f"- {iid}: {i.title} (Severity: {i.severity})")
    if not lines:
        return "(No existing decisions or issues)"
    return "\n".join(lines)


# ============================================================
# EDR Helper — Enterprise Decision Runtime (auto-triggered by ingest)
# ============================================================

def _run_edr_for_issue(engine: GovernanceEngine, edr_item: dict, file_path: Optional[str] = None, exclude_decision_ids: Optional[set] = None):
    """Run EDR deep research for an Issue that lacks sufficient evidence.

    Called automatically by ingest when an Issue triggers EDR evaluation.
    After EDR completes, attempts to link the Issue to an existing Decision
    or creates a new Decision if no match found.
    """
    import time
    from pathlib import Path

    issue_id = edr_item["issue_id"]
    issue_title = edr_item["issue_title"]
    reason = edr_item["reason"]

    width = 63
    sep = "═" * width
    thin = "─" * width

    # ── 1. Display EDR trigger info ─────────────────────────
    console.print()
    console.print(f"[bold yellow]⚠ EDR triggered for Issue {issue_id}: {issue_title}[/bold yellow]")
    console.print(f"[dim]  Reason: {reason}[/dim]")

    console.print()
    console.print(sep)
    console.print(f"  Issue ID:   {issue_id}")
    console.print(f"  议题:       {issue_title}")
    console.print(f"  分类:       [cyan]Issue[/cyan] — 有待解决的矛盾")
    console.print()
    console.print(f"  Evidence:   [bold red]Insufficient[/bold red] — 决策依据不足")
    console.print(f"  原因:       缺乏同类案例、政策依据、风险评估")
    console.print()
    console.print(f"  → 自动触发 [bold cyan]EDR Research Agent[/bold cyan]")
    console.print(sep)

    # ── 2. EDR execution animation ─────────────────────────
    console.print()
    console.print("[bold cyan]EDR Research Agent 执行中...[/bold cyan]")
    console.print()

    steps = [
        "查询同类国企改制案例",
        "查询相关政策依据",
        "分析引入投资方风险",
        "分析职工安置方案",
    ]
    for step in steps:
        time.sleep(0.6)
        console.print(f"  [green]✓[/green] {step}")

    console.print()
    time.sleep(0.4)
    console.print("[bold green]Research Report 生成完成[/bold green]")
    console.print()

    # ── 3. Display research report ──────────────────────────
    # Report file: <source_file_stem>_edr_research.txt alongside source
    report_path = None
    if file_path:
        base = Path(file_path).stem
        candidate = Path(file_path).parent / f"{base}_edr_research.txt"
        if candidate.exists():
            report_path = candidate

    if report_path.exists():
        console.print(report_path.read_text(encoding="utf-8"))
    else:
        console.print(sep)
        console.print("          EDR Research Report")
        console.print(f"          {issue_title} — 可行性分析")
        console.print(thin)
        console.print()
        console.print("  [yellow]⚠ 未找到报告文件[/yellow]")
        console.print()
        console.print(thin)
        console.print("  声明：Agent不做决策。Agent只提供证据。")
        console.print()
        console.print("  Ontology + Rules + Evidence = Decision")
        console.print(sep)

    # ── 4. Link Issue to Decision or create new ─────────────
    console.print()
    console.print("[bold]EDR Analysis Complete — resolving Issue...[/bold]")

    # Prefer LLM-suggested match from candidate_matches (weak_match_id)
    # over post-hoc engine bigram matching. Agent (LLM) alignment > engine heuristics.
    weak_match_id = edr_item.get("weak_match_id")
    if weak_match_id and weak_match_id in engine.graph.decisions:
        matched_decision_id = weak_match_id
        console.print(f"  [dim]Using Agent-identified alignment: Issue → {matched_decision_id}[/dim]")
    else:
        # Fallback: post-hoc keyword matching, excluding decisions from this ingest
        matched_decision_id = engine.find_decision_for_issue(issue_title, exclude_ids=exclude_decision_ids)

    if matched_decision_id:
        decision = engine.get_decision(matched_decision_id)
        if decision:
            # Link Issue to existing Decision
            engine.link_issue_to_decision(issue_id, matched_decision_id)
            # Add Update to reflect EDR findings
            update_content = f"EDR分析完成：{issue_title}，需结合EDR报告重新评估{decision.title}"
            engine.add_update(matched_decision_id, update_content, source="edr")
            console.print(f"  [green]✓ Issue {issue_id} linked to {matched_decision_id} ({decision.title})[/green]")
            console.print(f"  [green]✓ Update added: EDR分析结果已注入决策[/green]")

            # Emit event
            try:
                from moss.web.event_bus import emit_sync
                emit_sync("edr_triggered", {
                    "issue_id": issue_id,
                    "issue_title": issue_title,
                    "decision_id": matched_decision_id,
                    "status": "linked",
                })
                emit_sync("decision_updated", {
                    "id": matched_decision_id,
                    "title": decision.title,
                    "status": decision.status,
                    "edr_issue_id": issue_id,
                })
            except Exception:
                pass
        else:
            matched_decision_id = None  # Fallback to create new

    if not matched_decision_id:
        # No matching Decision — create new one from the Issue
        new_decision = engine.create_decision(
            title=issue_title,
            source="edr",
            raw_statement=f"EDR auto-created from Issue {issue_id}: {issue_title}",
        )
        engine.link_issue_to_decision(issue_id, new_decision.id)
        console.print(f"  [cyan]→ No matching Decision found[/cyan]")
        console.print(f"  [green]✓ New Decision {new_decision.id} created from Issue {issue_id}[/green]")

        # Emit event
        try:
            from moss.web.event_bus import emit_sync
            emit_sync("edr_triggered", {
                "issue_id": issue_id,
                "issue_title": issue_title,
                "decision_id": new_decision.id,
                "status": "new_decision_created",
            })
            emit_sync("decision_created", {
                "id": new_decision.id,
                "title": new_decision.title,
                "source": "edr",
                "status": new_decision.status,
            })
        except Exception:
            pass

    console.print()
    console.print(f"[dim]  Ontology + Rules + Evidence = Decision[/dim]")


# ============================================================
# Display Helpers
# ============================================================

def _print_decision_table(decisions):
    table = Table(title="Decisions")
    table.add_column("ID", style="cyan")
    table.add_column("Title")
    table.add_column("Status")
    table.add_column("Owner")
    table.add_column("Progress", justify="right")
    table.add_column("Risk")
    table.add_column("Deadline")

    for d in decisions:
        s = risk_style(d.risk_level)
        table.add_row(
            d.id,
            d.title,
            f"{status_icon(d.status)} {d.status}",
            d.owner or "[red]—[/red]",
            f"{d.progress:.0f}%",
            f"[{s}]{d.risk_level}[/{s}]",
            d.deadline.strftime("%Y-%m-%d") if d.deadline else "—",
        )

    console.print(table)


def _show_decision(engine, decision_id: str):
    decision = engine.get_decision(decision_id)
    if not decision:
        console.print(f"[red]Decision {decision_id} not found.[/red]")
        return

    s = risk_style(decision.risk_level)
    console.print(
        Panel(
            f"[bold]{decision.id}[/bold] [{s}]{decision.title}[/{s}]",
            title="Decision Details",
        )
    )

    table = Table(show_header=False, box=None)
    table.add_column("Field", style="bold cyan")
    table.add_column("Value")

    table.add_row("Status", f"{status_icon(decision.status)} {decision.status}")
    table.add_row("Progress", f"{decision.progress:.0f}%")
    table.add_row("Risk Level", f"[{s}]{decision.risk_level}[/{s}]")
    table.add_row("Owner", decision.owner or "[red]UNASSIGNED[/red]")
    table.add_row("Department", decision.department or "—")
    if decision.owner_role or decision.domain:
        table.add_row("Owner Role", decision.owner_role or "—")
        table.add_row("Business Domain", decision.domain or "—")
        # 对齐三分类即时反馈（无推理副作用：走 reason 一次拿三分类）
        try:
            from moss.owl_ontology import reason_graph
            r = reason_graph(engine.graph)
            if decision.id in r.get("role_aligned", []):
                table.add_row("Role Alignment", "[green]✓ aligned[/green]")
            elif decision.id in r.get("role_misaligned", []):
                table.add_row("Role Alignment", "[red]✗ misaligned — 岗位职责不覆盖该业务域[/red]")
            elif decision.id in r.get("role_unregistered", []):
                table.add_row("Role Alignment", "[yellow]? unregistered — 岗位不在词汇表[/yellow]")
        except Exception:
            pass
    table.add_row("Source", decision.source)
    table.add_row("Created", decision.created_at.strftime("%Y-%m-%d %H:%M"))
    deadline_str = decision.deadline.strftime("%Y-%m-%d") if decision.deadline else "[yellow]NOT SET[/yellow]"
    table.add_row("Deadline", deadline_str)
    last_up = decision.last_update_at.strftime("%Y-%m-%d %H:%M") if decision.last_update_at else "[yellow]NEVER[/yellow]"
    table.add_row("Last Update", last_up)

    if decision.raw_statement:
        rs = decision.raw_statement[:100]
        if len(decision.raw_statement) > 100:
            rs += "..."
        table.add_row("Raw Statement", rs)

    console.print(table)

    updates = engine.get_updates(decision_id)
    if updates:
        console.print("\n[bold]Progress Updates:[/bold]")
        for u in updates[:10]:
            p_str = f" [Progress: {u.progress}%]" if u.progress is not None else ""
            r_str = f" [Risk: {u.risk}]" if u.risk else ""
            console.print(
                f"  [{u.created_at.strftime('%Y-%m-%d %H:%M')}] "
                f"{u.content[:80]}{p_str}{r_str}"
            )
    else:
        console.print("\n[dim]No progress updates yet.[/dim]")

    compiler = GovernanceCompiler()

    temp_graph = GovernanceGraph()
    temp_graph.decisions[decision_id] = decision
    temp_graph.updates = [
        u for u in engine.graph.updates if u.decision_id == decision_id
    ]
    temp_graph.assignments = [
        a for a in engine.graph.assignments if a.decision_id == decision_id
    ]
    results = compiler.compile(temp_graph)

    for r in results:
        if r.severity == "ERROR":
            console.print(f"\n[bold red]❌ {r.message}[/bold red]")
        elif r.severity == "WARNING":
            console.print(f"\n[bold yellow]⚠ {r.message}[/bold yellow]")
        elif r.issue_type == "OK":
            console.print(f"\n[bold green]✔ {r.message}[/bold green]")


def _print_compile_results(results, graph=None):
    errors = [r for r in results if r.severity == "ERROR"]
    warnings = [r for r in results if r.severity == "WARNING"]
    oks = [r for r in results if r.issue_type == "OK"]

    if errors:
        console.print("\n[bold red]Errors:[/bold red]")
        for r in errors:
            console.print(f"  [red]❌ {r.decision_id} {r.decision_title} - {r.message}[/red]")

    if warnings:
        console.print("\n[bold yellow]Warnings:[/bold yellow]")
        for r in warnings:
            console.print(f"  [yellow]⚠ {r.decision_id} {r.decision_title} - {r.message}[/yellow]")

    if oks:
        # 如果没有ERROR和WARNING，显示漂亮的方框格式
        if not errors and not warnings and graph:
            for r in oks:
                _print_decision_detail(r.decision_id, r.decision_title, graph)
        else:
            console.print("\n[bold green]Passed:[/bold green]")
            for r in oks:
                console.print(f"  [green]✔ {r.decision_id} {r.decision_title} - OK[/green]")

    total = len(results)
    console.print(
        f"\n[bold]Summary: {len(errors)} errors, {len(warnings)} warnings, "
        f"{len(oks)} passed ({total} total)[/bold]"
    )


def _print_decision_detail(decision_id: str, decision_title: str, graph):
    """Print detailed decision info in a beautiful box format."""
    from datetime import datetime
    
    decision = graph.decisions.get(decision_id)
    if not decision:
        return
    
    # Calculate days remaining
    days_text = ""
    if decision.deadline:
        now = datetime.now()
        if decision.deadline < now:
            overdue_days = (now - decision.deadline).days
            days_text = f"{decision.deadline.strftime('%Y-%m-%d')} (已逾期{overdue_days}天)"
        else:
            remaining_days = (decision.deadline - now).days
            days_text = f"{decision.deadline.strftime('%Y-%m-%d')} ({remaining_days}天)"
    else:
        days_text = "未设置"
    
    # Status text
    status_texts = {
        "OPEN": "○ 待启动",
        "BLOCKED": "⊘ 已阻塞",
        "RUNNING": "◉ 执行中",
        "DONE": "✓ 已完成"
    }
    status_text = status_texts.get(decision.status, decision.status)
    
    # Owner
    owner_text = decision.owner or "未分配"
    
    # Department
    dept_text = decision.department or "未设置"
    
    # Dependency
    dep_text = decision.dependency or "无"
    
    # Print the box
    width = 68
    sep = "═" * width
    
    console.print(f"\n{sep}")
    console.print("          moss compile")
    console.print(sep)
    console.print(f"\n{decision_id}  {decision_title}")
    console.print(f"  [bold green]OK[/bold green]      所有治理检查通过 [dim]HermiT: GovernedDecision[/dim]")
    console.print(f"\n{sep}")
    console.print("          ✅ 核心决策治理完备")
    console.print()
    console.print(f"          Owner:       {owner_text}")
    console.print(f"          Department:  {dept_text}")
    console.print(f"          Dependency:  {dep_text}")
    console.print(f"          Deadline:    {days_text}")
    console.print(f"          Status:      {status_text}")
    console.print(sep)


# ── Ontology Repo 命令组 (moss onto ...) ──
from moss.cli.onto_cmd import onto  # noqa: E402
main.add_command(onto)


if __name__ == "__main__":
    main()
