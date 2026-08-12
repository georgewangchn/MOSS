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
        return d


# recursive model
Expr.model_rebuild()
