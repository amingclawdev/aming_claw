"""Source-controlled governance contract template registry."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import json
from pathlib import Path
from typing import Any


DEFAULT_TEMPLATE_DIR = Path(__file__).resolve().parent / "contract_templates"


class ContractTemplateError(ValueError):
    """Base error for contract template registry failures."""


class UnknownContractTemplateError(ContractTemplateError):
    """Raised when no source-controlled template matches a requested key."""


class MalformedContractTemplateError(ContractTemplateError):
    """Raised when a template file is not usable by the registry."""


def _template_paths(template_dir: str | Path = DEFAULT_TEMPLATE_DIR) -> list[Path]:
    root = Path(template_dir)
    if not root.exists():
        return []
    return sorted(root.glob("*.json"), key=lambda item: item.name)


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise MalformedContractTemplateError(f"{path.name}: invalid json: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise MalformedContractTemplateError(f"{path.name}: template root must be an object")
    return payload


def _string_list(value: Any, *, file_name: str, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise MalformedContractTemplateError(f"{file_name}: {field} must be a list of strings")
    return list(value)


def _template_version(template_id: str, payload: Mapping[str, Any]) -> str:
    version = payload.get("version")
    if isinstance(version, str) and version:
        return version
    if "." in template_id:
        return template_id.rsplit(".", 1)[-1]
    return ""


def _validate_template(payload: Mapping[str, Any], *, file_name: str) -> dict[str, Any]:
    template_id = payload.get("template_id")
    schema_version = payload.get("schema_version")
    if not isinstance(template_id, str) or not template_id:
        raise MalformedContractTemplateError(f"{file_name}: missing template_id")
    if not isinstance(schema_version, str) or not schema_version:
        raise MalformedContractTemplateError(f"{file_name}: missing schema_version")

    task_types = _string_list(payload.get("task_types"), file_name=file_name, field="task_types")
    stages = _string_list(payload.get("stages"), file_name=file_name, field="stages")
    version = _template_version(template_id, payload)
    if not version:
        raise MalformedContractTemplateError(f"{file_name}: missing version")

    normalized = dict(payload)
    normalized["version"] = version
    normalized["task_types"] = task_types
    normalized["stages"] = stages
    normalized.setdefault("source", {"type": "source_controlled", "path": file_name})
    return normalized


def load_contract_templates(template_dir: str | Path = DEFAULT_TEMPLATE_DIR) -> list[dict[str, Any]]:
    """Load and validate all source-controlled contract templates."""

    templates: list[dict[str, Any]] = []
    for path in _template_paths(template_dir):
        templates.append(_validate_template(_load_json(path), file_name=path.name))
    return sorted(templates, key=lambda item: str(item["template_id"]))


def list_contract_templates(
    *,
    template_dir: str | Path = DEFAULT_TEMPLATE_DIR,
    task_type: str | None = None,
    stage: str | None = None,
) -> list[dict[str, Any]]:
    """List templates, optionally filtered by task type and stage."""

    templates = load_contract_templates(template_dir)
    return [
        template
        for template in templates
        if _matches(template, task_type=task_type, stage=stage)
    ]


def get_contract_template(
    template_id: str,
    *,
    template_dir: str | Path = DEFAULT_TEMPLATE_DIR,
) -> dict[str, Any]:
    """Return a template by exact versioned template id."""

    for template in load_contract_templates(template_dir):
        if template["template_id"] == template_id:
            return template
    raise UnknownContractTemplateError(f"unknown contract template: {template_id}")


def resolve_contract_template(
    *,
    template_id: str | None = None,
    task_type: str | None = None,
    stage: str | None = None,
    version: str | None = None,
    template_dir: str | Path = DEFAULT_TEMPLATE_DIR,
) -> dict[str, Any]:
    """Resolve a template by exact id, id plus version, or task_type/stage."""

    templates = list_contract_templates(template_dir=template_dir, task_type=task_type, stage=stage)
    if template_id:
        templates = [
            template
            for template in templates
            if template["template_id"] == template_id
            or (
                version
                and str(template["template_id"]) == f"{template_id}.{version}"
            )
        ]
    if version:
        templates = [template for template in templates if template.get("version") == version]
    if not templates:
        key = ", ".join(
            part
            for part in (
                f"template_id={template_id}" if template_id else "",
                f"task_type={task_type}" if task_type else "",
                f"stage={stage}" if stage else "",
                f"version={version}" if version else "",
            )
            if part
        )
        raise UnknownContractTemplateError(f"unknown contract template resolution: {key or 'empty query'}")
    if len(templates) > 1:
        raise ContractTemplateError(
            "ambiguous contract template resolution: "
            + ", ".join(str(template["template_id"]) for template in templates)
        )
    return templates[0]


def _matches(template: Mapping[str, Any], *, task_type: str | None, stage: str | None) -> bool:
    task_types = _as_set(template.get("task_types"))
    stages = _as_set(template.get("stages"))
    if task_type and task_type not in task_types:
        return False
    if stage and stage not in stages:
        return False
    return True


def _as_set(values: Any) -> set[str]:
    if isinstance(values, Iterable) and not isinstance(values, (str, bytes, dict)):
        return {str(value) for value in values}
    return set()
