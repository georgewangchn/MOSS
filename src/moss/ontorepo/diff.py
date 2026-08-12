"""语义级本体 Diff（非文本 diff）。

按实体类型（concept / data_property / object_property / defined_class）建
name→entity 索引，求对称差得 added/removed，交集逐字段比较得 modified。
删除 concept/property、或改动 defined_class 的 equivalent_to 视为 breaking
（会改变分类结果）。
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from .model import OntologyModel


class EntityChange(BaseModel):
    kind: str            # concept | data_property | object_property | defined_class
    name: str
    change: str          # added | removed | modified
    detail: dict[str, Any] = {}


class OntologyDiff(BaseModel):
    from_version: str
    to_version: str
    changes: list[EntityChange] = []
    breaking: bool = False

    def is_empty(self) -> bool:
        return not self.changes


def _index(items, key: str = "name") -> dict:
    return {getattr(i, key): i for i in items}


def _dump(entity) -> dict:
    return entity.model_dump(by_alias=True, exclude_none=True, mode="json")


def _compare(kind, a_map, b_map, breaking_on_remove, breaking_field=None):
    changes: list[EntityChange] = []
    breaking = False
    for name in b_map.keys() - a_map.keys():
        changes.append(EntityChange(kind=kind, name=name, change="added", detail=_dump(b_map[name])))
    for name in a_map.keys() - b_map.keys():
        changes.append(EntityChange(kind=kind, name=name, change="removed", detail=_dump(a_map[name])))
        if breaking_on_remove:
            breaking = True
    for name in a_map.keys() & b_map.keys():
        da, db = _dump(a_map[name]), _dump(b_map[name])
        if da != db:
            fields = {k: {"old": da.get(k), "new": db.get(k)} for k in set(da) | set(db) if da.get(k) != db.get(k)}
            changes.append(EntityChange(kind=kind, name=name, change="modified", detail=fields))
            if breaking_field and breaking_field in fields:
                breaking = True
    return changes, breaking


def diff_models(a: OntologyModel, b: OntologyModel, from_version: str = "A", to_version: str = "B") -> OntologyDiff:
    changes: list[EntityChange] = []
    breaking = False

    for kind, a_items, b_items, brk_rm, brk_field in [
        ("concept", a.concepts, b.concepts, True, None),
        ("data_property", a.properties.data, b.properties.data, True, None),
        ("object_property", a.properties.object, b.properties.object, True, None),
        ("defined_class", a.defined_classes, b.defined_classes, False, "equivalent_to"),
    ]:
        c, brk = _compare(kind, _index(a_items), _index(b_items), brk_rm, brk_field)
        changes.extend(c)
        breaking = breaking or brk

    return OntologyDiff(from_version=from_version, to_version=to_version, changes=changes, breaking=breaking)
