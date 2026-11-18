"""The real tool schemas (the node tool lists cached in data/nodes/*.json) and validation against them."""

from __future__ import annotations

import json
from pathlib import Path


def load_tools(nodes_dir: Path) -> dict[str, dict]:
    """tool name -> {"description", "schema"} from every cached node list. Missing directory: no tools."""
    tools: dict[str, dict] = {}
    for path in sorted(nodes_dir.glob("*.json")):
        try:
            listed = json.loads(path.read_text())
        except ValueError:
            continue
        for tool in listed if isinstance(listed, list) else []:
            name = tool.get("name")
            if name and name not in tools:
                tools[name] = {
                    "description": tool.get("description") or "",
                    "schema": tool.get("input_schema") or tool.get("inputSchema") or {"type": "object"},
                }
    return tools


def _type_ok(value: object, kind: str) -> bool:
    if kind == "string":
        return isinstance(value, str)
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "array":
        return isinstance(value, list)
    if kind == "object":
        return isinstance(value, dict)
    if kind == "null":
        return value is None
    return True


def value_ok(value: object, schema: dict) -> bool:
    if not isinstance(schema, dict):
        return True
    if "anyOf" in schema or "oneOf" in schema:
        return any(value_ok(value, s) for s in schema.get("anyOf") or schema.get("oneOf") or [])
    if "enum" in schema and value not in schema["enum"]:
        return False
    kind = schema.get("type")
    if isinstance(kind, list):
        return any(_type_ok(value, k) for k in kind)
    if kind and not _type_ok(value, kind):
        return False
    if kind == "array" and isinstance(schema.get("items"), dict):
        return all(value_ok(v, schema["items"]) for v in value)
    return True


def validate_call(call: object, tools: dict[str, dict]) -> str | None:
    """None when the call is valid for the real schemas, else the reason it is not."""
    if not isinstance(call, dict) or not isinstance(call.get("tool"), str):
        return "call is not {tool, args}"
    name = call["tool"]
    if name not in tools:
        return f"unknown tool {name}"
    args = call.get("args", {})
    if not isinstance(args, dict):
        return "args is not an object"
    schema = tools[name]["schema"]
    props = schema.get("properties") or {}
    for arg, value in args.items():
        if arg not in props:
            return f"{name} has no argument {arg}"
        if not value_ok(value, props[arg]):
            return f"{name}.{arg} has the wrong type"
    for required in schema.get("required") or []:
        if required not in args:
            return f"{name} needs {required}"
    return None
