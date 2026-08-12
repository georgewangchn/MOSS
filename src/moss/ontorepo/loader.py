"""加载本体 DSL（YAML）并做 JSON Schema 校验 → OntologyModel。

路径解析：默认取**随包分发的** resources/ontology/moss.onto.yaml（可用环境变量
MOSS_ONTOLOGY 覆盖成工作区副本）。这样 pip 安装后也能开箱即用，不再依赖"仓库根"布局。
校验分两层：先 JSON Schema（结构/参考编码合法性），再 Pydantic（类型/语义结构）。
两层都失败即抛 OntologyValidationError，供 `moss onto validate` 呈现。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import yaml

from .model import OntologyModel

# src/moss/ontorepo/loader.py → parent.parent = moss 包根；
# 本体数据随包分发在 moss/resources/ontology/。
_ONTOLOGY_DIR = Path(__file__).resolve().parent.parent / "resources" / "ontology"

DEFAULT_DSL_PATH = _ONTOLOGY_DIR / "moss.onto.yaml"
SCHEMA_PATH = _ONTOLOGY_DIR / "schema" / "ontology.schema.json"


class OntologyValidationError(Exception):
    """DSL 未通过 JSON Schema 或 Pydantic 校验。"""


def default_dsl_path() -> Path:
    env = os.environ.get("MOSS_ONTOLOGY")
    if env:
        return Path(env).expanduser().resolve()
    return DEFAULT_DSL_PATH


def build_dir() -> Path:
    """编译产物（.owl/.ttl/.lock）写到可写的工作区，而不是只读的安装包内。"""
    from moss.config import get_config
    d = get_config().data_path() / "ontology-build"
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_raw(path: Path | None = None) -> dict[str, Any]:
    p = Path(path) if path else default_dsl_path()
    if not p.exists():
        raise OntologyValidationError(f"本体 DSL 文件不存在: {p}")
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise OntologyValidationError(f"YAML 解析失败 ({p}): {e}") from e
    if not isinstance(data, dict):
        raise OntologyValidationError(f"本体 DSL 顶层必须是映射 (mapping): {p}")
    return data


def validate_schema(data: dict[str, Any]) -> None:
    """用 JSON Schema 校验；聚合所有错误一次抛出。"""
    try:
        import jsonschema
        from jsonschema import Draft202012Validator
    except ImportError as e:  # pragma: no cover - 依赖缺失
        raise OntologyValidationError(
            "缺少依赖 jsonschema。请 `pip install jsonschema>=4.20`。"
        ) from e

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(data), key=lambda e: list(e.path))
    if errors:
        lines = []
        for err in errors:
            loc = "/".join(str(p) for p in err.path) or "<root>"
            lines.append(f"  • {loc}: {err.message}")
        raise OntologyValidationError(
            "JSON Schema 校验失败:\n" + "\n".join(lines)
        )


def load_model(path: Path | None = None) -> OntologyModel:
    """加载 + 双层校验，返回 OntologyModel。"""
    data = load_raw(path)
    validate_schema(data)
    try:
        return OntologyModel.model_validate(data)
    except Exception as e:  # pydantic ValidationError
        raise OntologyValidationError(f"模型校验失败:\n{e}") from e
