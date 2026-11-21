"""OntologyOps Ontology Repo DSL 的 Pydantic 数据模型。

对应 ontology/schema/ontology.schema.json（参考编码 v1）。
DSL 语义 = OWL 2 DL；这里只做结构建模，不做 OWL 语义解释（那在 owl_ontology 编译器里）。
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class OntologyMeta(BaseModel):
    model_config = ConfigDict(extra="allow")
    iri: str
    version: Optional[str] = None
    label: Optional[str] = None


class Concept(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    parent: str = "Thing"
    comment: Optional[str] = None


class Property(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    domain: str = "Thing"
    range: str
    functional: bool = False
    comment: Optional[str] = None


class PropertySet(BaseModel):
    model_config = ConfigDict(extra="forbid")
    data: list[Property] = Field(default_factory=list)
    object: list[Property] = Field(default_factory=list)


class Filler(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")
    # `class` is a Python keyword → alias
    class_: Optional[str] = Field(default=None, alias="class")
    datatype: Optional[Literal["string", "float", "integer", "boolean"]] = None


class Expr(BaseModel):
    """判别式联合的 OWL 类表达式节点（class/and/or/not/some/value）。"""
    model_config = ConfigDict(populate_by_name=True, extra="forbid")
    type: Literal["class", "and", "or", "not", "some", "value"]
    # class
    name: Optional[str] = None
    # and / or
    operands: Optional[list["Expr"]] = None
    # not
    operand: Optional["Expr"] = None
    # some / value
    property: Optional[str] = None
    filler: Optional[Filler] = None
    value: Optional[Any] = None


class DefinedClass(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    parent: str = "Decision"
    result_key: Optional[str] = None
    comment: Optional[str] = None
    equivalent_to: Expr


class Binding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: Optional[str] = None
    transform: Optional[str] = None
    computed: Optional[str] = None
    relation: Optional[str] = None


class OrgPosition(BaseModel):
    """管理岗位：主业务域 + 具体职责清单（org 词汇表）。"""
    model_config = ConfigDict(extra="forbid")
    id: str
    label: str
    reports_to: Optional[str] = None
    domain: str
    duties: list[str] = Field(default_factory=list)
    # 常见叫法（如 财务总监/CFO → 首席财务官）；归一化后参与匹配
    aliases: list[str] = Field(default_factory=list)
    # 高管岗（董事长/CEO）对全部业务域负最终责任 → 对齐判定恒真
    covers_all_domains: bool = False


class Archetype(BaseModel):
    """典型公司原型：行业 + 特有业务域 + 专业岗位（通用岗位由 universal_positions 提供）。"""
    model_config = ConfigDict(extra="forbid")
    id: str
    label: str
    industry: str
    extra_domains: list[str] = Field(default_factory=list)
    # 特有业务域的常见叫法：标准域 → 别名列表
    domain_aliases: dict[str, list[str]] = Field(default_factory=dict)
    positions: list[OrgPosition] = Field(default_factory=list)

    def all_domains(self) -> list[str]:
        """该原型下可用的全部业务域（不含通用域）。"""
        return list(self.extra_domains) + [p.domain for p in self.positions]


def _norm(s: str) -> str:
    """词汇匹配归一化：去首尾空白、剥全角/半角括号注释、统一小写。

    "首席财务官（分管财务）" → "首席财务官"；"CFO " → "cfo"。
    """
    t = s.strip().lower()
    for open_b, close_b in (("（", "）"), ("(", ")")):
        if open_b in t:
            t = t.split(open_b)[0].strip()
    return t


class OrgVocabulary(BaseModel):
    """组织治理词汇表：通用岗位（L0）+ 通用业务域 + 行业/公司原型分层（L1/L2/L3）。"""
    model_config = ConfigDict(extra="forbid")
    universal_positions: list[OrgPosition] = Field(default_factory=list)
    domains: list[str] = Field(default_factory=list)
    # 通用业务域别名：标准域 → 常见叫法（预算→财务、人事→人力）
    domain_aliases: dict[str, list[str]] = Field(default_factory=dict)
    archetypes: list[Archetype] = Field(default_factory=list)

    def archetype_by_id(self, archetype_id: Optional[str]) -> Optional[Archetype]:
        if not archetype_id:
            return None
        for a in self.archetypes:
            if a.id == archetype_id:
                return a
        return None

    def _position_matches(self, pos: OrgPosition, role_norm: str) -> bool:
        if _norm(pos.id) == role_norm or _norm(pos.label) == role_norm:
            return True
        return any(_norm(alias) == role_norm for alias in pos.aliases)

    def find_position(self, role: Optional[str], archetype_id: Optional[str] = None) -> Optional[OrgPosition]:
        """按 id / label / alias 匹配岗位（归一化比较）：先当前原型专业岗位，后通用岗位。"""
        if not role:
            return None
        role_norm = _norm(role)
        arch = self.archetype_by_id(archetype_id)
        pools: list[list[OrgPosition]] = []
        if arch:
            pools.append(arch.positions)
        pools.append(self.universal_positions)
        for pool in pools:
            for p in pool:
                if self._position_matches(p, role_norm):
                    return p
        return None

    def resolve_domain(self, name: Optional[str], archetype_id: Optional[str] = None) -> Optional[str]:
        """把业务域的任意常见叫法解析为标准域名（先原型特有别名，后通用别名，均归一化比较）。"""
        if not name:
            return None
        n = _norm(name)
        arch = self.archetype_by_id(archetype_id)
        candidates: list[str] = list(self.domains)
        if arch:
            candidates = arch.all_domains() + candidates
        for std in candidates:
            if _norm(std) == n:
                return std
        alias_maps: list[dict[str, list[str]]] = []
        if arch:
            alias_maps.append(arch.domain_aliases)
        alias_maps.append(self.domain_aliases)
        for amap in alias_maps:
            for std, aliases in amap.items():
                if any(_norm(a) == n for a in aliases):
                    return std
        return None


class OntologyModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")
    protocol: Optional[str] = None
    protocol_version: Optional[str] = None
    ontology: OntologyMeta
    datatypes: dict[str, str] = Field(default_factory=dict)
    concepts: list[Concept]
    properties: PropertySet
    defined_classes: list[DefinedClass]
    abox_binding: dict[str, Binding] = Field(default_factory=dict)
    org: Optional[OrgVocabulary] = None
    rules: list[Any] = Field(default_factory=list)

    # ── helpers ───────────────────────────────────────────────
    def defined_by_key(self) -> dict[str, DefinedClass]:
        """result_key → DefinedClass（仅含声明了 result_key 的）。"""
        return {dc.result_key: dc for dc in self.defined_classes if dc.result_key}

    def result_keys(self) -> list[str]:
        return [dc.result_key for dc in self.defined_classes if dc.result_key]

    def canonical_dict(self) -> dict:
        """规范化字典：列表按 name 排序，供稳定 hash / diff。

        仅列表顺序不同不应产生不同的内容 hash。
        """
        d = self.model_dump(by_alias=True, exclude_none=True, mode="json")
        if isinstance(d.get("concepts"), list):
            d["concepts"] = sorted(d["concepts"], key=lambda x: x.get("name", ""))
        props = d.get("properties")
        if isinstance(props, dict):
            for k in ("data", "object"):
                if isinstance(props.get(k), list):
                    props[k] = sorted(props[k], key=lambda x: x.get("name", ""))
        if isinstance(d.get("defined_classes"), list):
            d["defined_classes"] = sorted(d["defined_classes"], key=lambda x: x.get("name", ""))
        org = d.get("org")
        if isinstance(org, dict):
            if isinstance(org.get("universal_positions"), list):
                org["universal_positions"] = sorted(org["universal_positions"], key=lambda x: x.get("id", ""))
            if isinstance(org.get("archetypes"), list):
                for a in org["archetypes"]:
                    if isinstance(a.get("positions"), list):
                        a["positions"] = sorted(a["positions"], key=lambda x: x.get("id", ""))
                org["archetypes"] = sorted(org["archetypes"], key=lambda x: x.get("id", ""))
        return d


# recursive model
Expr.model_rebuild()
