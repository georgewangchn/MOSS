"""`govops onto ...` — Ontology Repo 命令组。

把本体当代码一样版本化治理（OntologyOps "Ontology Repo" 支柱）:
  validate  校验本体 DSL（JSON Schema + 模型）
  show      展示本体结构（concepts / properties / defined classes）
  build     编译成 OWL TBox + 产出 .owl/.ttl 产物            [Phase B]
  diff / log / tag / rollback / commit                        [Phase D]
  conformance  跑符合性条款，自证是否符合 OntologyOps 协议   [Phase E]

命名刻意避开既有 compile(决策合规) / checkpoint(治理心跳) / migrate(ABox 迁移)。
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.tree import Tree

from governanceops.ontorepo.loader import (
    OntologyValidationError,
    build_dir,
    default_dsl_path,
    load_model,
)

console = Console()


def _content_hash(model) -> str:
    canon = json.dumps(model.canonical_dict(), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


@click.group()
def onto():
    """Ontology Repo — 把本体当代码一样版本化治理 (validate/build/diff/tag/rollback)."""


@onto.command()
@click.option("-f", "--file", "path", default=None, help="本体 DSL 路径（默认 ontology/govops.onto.yaml）")
def validate(path):
    """校验本体 DSL：JSON Schema + 模型结构。"""
    dsl = Path(path) if path else default_dsl_path()
    try:
        model = load_model(dsl)
    except OntologyValidationError as e:
        console.print(f"[red]✗ 本体 DSL 不合法[/red] ({dsl})")
        console.print(str(e))
        sys.exit(1)
    n_concepts = len(model.concepts)
    n_props = len(model.properties.data) + len(model.properties.object)
    n_defined = len(model.defined_classes)
    console.print(f"[green]✓ 本体 DSL 合法[/green]  [dim]{dsl}[/dim]")
    console.print(
        f"  IRI {model.ontology.iri}  ·  version {model.ontology.version}  ·  "
        f"{n_concepts} concepts / {n_props} properties / {n_defined} defined-classes"
    )


@onto.command()
@click.option("-f", "--file", "path", default=None, help="本体 DSL 路径")
def show(path):
    """以树形展示本体结构。"""
    dsl = Path(path) if path else default_dsl_path()
    try:
        model = load_model(dsl)
    except OntologyValidationError as e:
        console.print(f"[red]✗ {e}[/red]")
        sys.exit(1)

    tree = Tree(f"[bold]{model.ontology.label or model.ontology.iri}[/bold] "
                f"[dim]v{model.ontology.version}[/dim]")

    c_node = tree.add("[cyan]Concepts[/cyan]")
    for c in model.concepts:
        c_node.add(f"{c.name} [dim](⊑ {c.parent})[/dim]")

    p_node = tree.add("[cyan]Properties[/cyan]")
    for p in model.properties.data:
        fn = " ·functional" if p.functional else ""
        p_node.add(f"{p.name}: {p.domain} → {p.range} [dim](data{fn})[/dim]")
    for p in model.properties.object:
        p_node.add(f"{p.name}: {p.domain} → {p.range} [dim](object)[/dim]")

    d_node = tree.add("[cyan]Defined Classes[/cyan] [dim](HermiT 分类目标)[/dim]")
    for dc in model.defined_classes:
        rk = f" [green]→ {dc.result_key}[/green]" if dc.result_key else ""
        label = f"{dc.name}{rk}"
        if dc.comment:
            label += f"  [dim]{dc.comment}[/dim]"
        d_node.add(label)

    console.print(tree)


@onto.command()
@click.option("-f", "--file", "path", default=None, help="本体 DSL 路径")
@click.option("--commit", is_flag=True, help="编译成功后提交为一个本体版本 (Phase D)")
@click.option("-m", "--message", default="build", help="提交信息 (配合 --commit)")
def build(path, commit, message):
    """编译 DSL → owlready2 TBox，跑一次分类自检，产出 build/govops.{owl,ttl} + lock.json。"""
    dsl = Path(path) if path else default_dsl_path()
    try:
        model = load_model(dsl)
    except OntologyValidationError as e:
        console.print(f"[red]✗ {e}[/red]")
        sys.exit(1)

    # 编译成内存 TBox（不加载 ABox → 产物为纯 TBox）
    from governanceops.owl_ontology import GovernanceOntology
    from governanceops.ontorepo.export import export_owl, export_ttl

    o = GovernanceOntology()
    o._model = model
    o.build()

    bdir = build_dir()
    owl_path = export_owl(o.onto, bdir / "govops.owl")
    ttl_path = export_ttl(o.onto, bdir / "govops.ttl")

    chash = _content_hash(model)
    lock = {
        "content_hash": chash,
        "iri": model.ontology.iri,
        "version": model.ontology.version,
        "concepts": len(model.concepts),
        "properties": len(model.properties.data) + len(model.properties.object),
        "defined_classes": len(model.defined_classes),
        "result_keys": model.result_keys(),
        "model": model.canonical_dict(),
    }
    lock_path = bdir / "govops.lock.json"
    lock_path.write_text(json.dumps(lock, ensure_ascii=False, indent=2), encoding="utf-8")

    console.print(f"[green]✓ 编译成功[/green]  content_hash [yellow]{chash[:12]}[/yellow]")
    console.print(f"  [dim]产物目录[/dim] {bdir}")
    for p in (owl_path, ttl_path, lock_path):
        console.print(f"  {p.name}")

    if commit:
        from governanceops.ontorepo.store import OntologyStore
        vid = OntologyStore().commit(model, message)
        console.print(f"[green]✓ committed[/green] version [yellow]{vid}[/yellow]")


# ── 版本化子命令 (Phase D) ────────────────────────────────────

@onto.command()
@click.option("-m", "--message", default="commit", help="提交信息")
@click.option("-f", "--file", "path", default=None, help="本体 DSL 路径")
def commit(message, path):
    """把当前本体 DSL 提交为一个版本（内容不变则不新增）。"""
    from governanceops.ontorepo.store import OntologyStore
    dsl = Path(path) if path else default_dsl_path()
    try:
        model = load_model(dsl)
    except OntologyValidationError as e:
        console.print(f"[red]✗ {e}[/red]")
        sys.exit(1)
    store = OntologyStore()
    prev = store.head()
    vid = store.commit(model, message)
    if vid == prev:
        console.print(f"[dim]无变更，HEAD 仍为 {vid}[/dim]")
    else:
        console.print(f"[green]✓ committed[/green] [yellow]{vid}[/yellow]  {message}")


@onto.command()
def log():
    """展示本体版本历史（HEAD → 根）。"""
    from governanceops.ontorepo.store import OntologyStore
    from rich.table import Table
    entries = OntologyStore().log()
    if not entries:
        console.print("[dim]暂无版本。先跑 `govops onto commit` 或 `govops onto build --commit`。[/dim]")
        return
    table = Table(title="Ontology 版本历史", show_lines=False)
    table.add_column("version", style="yellow")
    table.add_column("parent", style="dim")
    table.add_column("created", style="dim")
    table.add_column("message")
    table.add_column("tags", style="green")
    for i, e in enumerate(entries):
        vid = e["id"] + ("  [cyan](HEAD)[/cyan]" if i == 0 else "")
        table.add_row(vid, (e["parent"] or "—")[:12], e["created_at"], e["message"], " ".join(e["tags"]))
    console.print(table)


@onto.command()
@click.argument("a", required=False)
@click.argument("b", required=False)
@click.option("-f", "--file", "path", default=None, help="工作区 DSL 路径（默认本体文件）")
def diff(a, b, path):
    """比较两个版本；缺省比较 HEAD 与工作区文件。

    用法: onto diff              (HEAD vs 工作区)
          onto diff <ver>        (<ver> vs 工作区)
          onto diff <verA> <verB>
    """
    from governanceops.ontorepo.store import OntologyStore, OntologyStoreError
    from governanceops.ontorepo.diff import diff_models
    store = OntologyStore()
    dsl = Path(path) if path else default_dsl_path()

    try:
        if a and b:
            ma, mb = store.get(store.resolve(a)), store.get(store.resolve(b))
            fv, tv = a, b
        elif a:
            ma = store.get(store.resolve(a))
            mb = load_model(dsl)
            fv, tv = a, "working"
        else:
            head = store.head()
            if head is None:
                console.print("[dim]无 HEAD。先 commit 一个版本再 diff。[/dim]")
                return
            ma = store.get(head)
            mb = load_model(dsl)
            fv, tv = "HEAD", "working"
    except (OntologyStoreError, OntologyValidationError) as e:
        console.print(f"[red]✗ {e}[/red]")
        sys.exit(1)

    d = diff_models(ma, mb, fv, tv)
    if d.is_empty():
        console.print(f"[green]无差异[/green]  {fv} == {tv}")
        return
    tag = "[red]BREAKING[/red]" if d.breaking else "[yellow]compatible[/yellow]"
    console.print(f"Diff [dim]{fv} → {tv}[/dim]  {tag}")
    sym = {"added": "[green]+[/green]", "removed": "[red]-[/red]", "modified": "[yellow]~[/yellow]"}
    for c in d.changes:
        console.print(f"  {sym.get(c.change,'?')} {c.kind} [bold]{c.name}[/bold] [dim]{c.change}[/dim]")


@onto.command()
@click.argument("name")
@click.argument("version", required=False)
def tag(name, version):
    """给某版本打标签（缺省 HEAD）。"""
    from governanceops.ontorepo.store import OntologyStore, OntologyStoreError
    try:
        vid = OntologyStore().tag(name, version)
    except OntologyStoreError as e:
        console.print(f"[red]✗ {e}[/red]")
        sys.exit(1)
    console.print(f"[green]✓ tag[/green] [green]{name}[/green] → [yellow]{vid}[/yellow]")


@onto.command()
@click.argument("version")
@click.option("-f", "--file", "path", default=None, help="要还原的工作区 DSL 路径")
def rollback(version, path):
    """回滚到某版本：还原工作区 DSL 文件并前移 HEAD。"""
    from governanceops.ontorepo.store import OntologyStore, OntologyStoreError
    dsl = Path(path) if path else default_dsl_path()
    try:
        vid = OntologyStore().rollback(version, working_path=dsl)
    except OntologyStoreError as e:
        console.print(f"[red]✗ {e}[/red]")
        sys.exit(1)
    console.print(f"[green]✓ rolled back[/green] HEAD → [yellow]{vid}[/yellow]  [dim]（已还原 {dsl.name}）[/dim]")


# ── 符合性 (Phase E) ──────────────────────────────────────────

@onto.command()
@click.option("--json", "as_json", is_flag=True, help="输出机器可读的符合性报告")
@click.option("-f", "--file", "path", default=None, help="本体 DSL 路径")
def conformance(as_json, path):
    """跑符合性条款，自证是否符合 OntologyOps 'Ontology Repo' 协议。"""
    from governanceops.ontorepo.conformance import run
    report = run(Path(path) if path else None)

    if as_json:
        console.print_json(report.model_dump_json())
        sys.exit(0 if report.passed else 1)

    from rich.table import Table
    table = Table(title=f"符合性: {report.protocol} v{report.version}")
    table.add_column("clause", style="yellow")
    table.add_column("层", style="cyan")
    table.add_column("", width=3)
    table.add_column("说明")
    table.add_column("结果", style="dim")
    for c in report.clauses:
        icon = "[green]✓[/green]" if c.passed else "[red]✗[/red]"
        table.add_row(c.id, c.profile, icon, c.desc, c.message)
    console.print(table)
    verdict = "[green]PASS ✅ 符合协议[/green]" if report.passed else "[red]FAIL ❌ 不符合[/red]"
    console.print(verdict)
    sys.exit(0 if report.passed else 1)
