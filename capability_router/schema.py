"""Small closed JSON Schema subset used at both trust boundaries."""

from __future__ import annotations

from decimal import Decimal
import math
import re
import time
from typing import Any
from urllib.parse import unquote_to_bytes


TYPES = {"object", "array", "string", "integer", "number", "boolean", "null"}
VALIDATION_LIMIT = "schema validation work limit exceeded"
MAX_VALIDATION_ERRORS = 32
SUPPORTED_DIALECTS = {
    "https://json-schema.org/draft/2020-12/schema",
    "https://json-schema.org/draft/2020-12/schema#",
}
KNOWN_KEYWORDS = {
    "type", "properties", "required", "additionalProperties", "items", "minItems",
    "maxItems", "uniqueItems", "enum", "const", "minimum", "maximum",
    "exclusiveMinimum", "minLength", "maxLength", "pattern", "description", "title",
    "default", "$schema", "$defs", "$ref", "anyOf", "allOf", "oneOf", "format",
    "propertyNames",
}


def _valid_required(value: Any) -> bool:
    return (
        isinstance(value, list)
        and all(isinstance(item, str) for item in value)
        and len(set(value)) == len(value)
    )


def _valid_nonnegative_integer(value: Any) -> bool:
    return _is_integer(value) and value >= 0


def _valid_pattern(value: Any) -> bool:
    return isinstance(value, str) and _pattern_supported(value)


KEYWORD_VALIDATORS = {
    "type": lambda value: schema_types(value) is not None,
    "$schema": lambda value: isinstance(value, str) and value in SUPPORTED_DIALECTS,
    "format": lambda value: isinstance(value, str),
    "title": lambda value: isinstance(value, str),
    "description": lambda value: isinstance(value, str),
    "required": _valid_required,
    "minLength": _valid_nonnegative_integer,
    "maxLength": _valid_nonnegative_integer,
    "minItems": _valid_nonnegative_integer,
    "maxItems": _valid_nonnegative_integer,
    "minimum": lambda value: _is_number(value),
    "maximum": lambda value: _is_number(value),
    "exclusiveMinimum": lambda value: _is_number(value),
    "uniqueItems": lambda value: isinstance(value, bool),
    "pattern": _valid_pattern,
    "enum": lambda value: isinstance(value, list),
}


class ValidationLimit(Exception):
    pass


def _record_error(errors: list[str], error: str) -> None:
    if len(errors) < MAX_VALIDATION_ERRORS:
        errors.append(error)


def _merge_errors(errors: list[str], additions: list[str]) -> None:
    remaining = MAX_VALIDATION_ERRORS - len(errors)
    if remaining > 0:
        errors.extend(additions[:remaining])


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and (
        not isinstance(value, float) or math.isfinite(value)
    )


def _is_integer(value: Any) -> bool:
    return _is_number(value) and (isinstance(value, int) or value.is_integer())


def _json_identity(
    value: Any,
    *,
    budget: list[int] | None = None,
    deadline: float | None = None,
) -> Any:
    if budget is not None:
        budget[0] -= 1
        if budget[0] < 0:
            raise ValidationLimit
    if deadline is not None and time.monotonic() >= deadline:
        raise ValidationLimit
    if value is None:
        return ("null",)
    if isinstance(value, bool):
        return ("boolean", value)
    if _is_number(value):
        return ("number", Decimal(str(value)))
    if isinstance(value, str):
        return ("string", value)
    if isinstance(value, list):
        return (
            "array",
            tuple(_json_identity(item, budget=budget, deadline=deadline) for item in value),
        )
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return (
            "object",
            tuple(
                sorted(
                    (key, _json_identity(child, budget=budget, deadline=deadline))
                    for key, child in value.items()
                )
            ),
        )
    raise ValueError("value is not interoperable JSON")


def _json_value_supported(value: Any) -> bool:
    try:
        _json_identity(value)
    except ValueError:
        return False
    return True


def _pattern_supported(pattern: str) -> bool:
    if (
        len(pattern) > 256
        or not pattern.startswith("^")
        or not pattern.endswith("$")
        or any(character in pattern for character in "\\(){}|")
    ):
        return False
    in_class = False
    class_characters = 0
    quantifiers: list[int] = []
    for index, character in enumerate(pattern):
        if character == "[":
            if in_class:
                return False
            in_class = True
            class_characters = 0
        elif character == "]":
            if not in_class or class_characters == 0:
                return False
            in_class = False
        elif not in_class and character in "^$" and index not in {0, len(pattern) - 1}:
            return False
        elif not in_class and character in "*+?":
            quantifiers.append(index)
        elif in_class:
            class_characters += 1
    if in_class or len(quantifiers) > 1:
        return False
    if quantifiers and pattern[quantifiers[0] + 1 :] not in {"", "$"}:
        return False
    try:
        re.compile(pattern)
    except re.error:
        return False
    return True


def _python_pattern(pattern: str) -> str:
    translated: list[str] = []
    in_class = False
    for character in pattern[1:-1]:
        if character == "[":
            in_class = True
        elif character == "]":
            in_class = False
        if character == "." and not in_class:
            translated.append("[^\n\r\u2028\u2029]")
        else:
            translated.append(character)
    return "".join(translated)


def schema_types(value: Any) -> list[str] | None:
    if isinstance(value, str):
        return [value] if value in TYPES else None
    if isinstance(value, list) and value and all(isinstance(item, str) for item in value):
        if len(set(value)) == len(value) and all(item in TYPES for item in value):
            return value
    return None


def resolve_reference(root: dict[str, Any], reference: Any) -> Any:
    if not isinstance(reference, str) or not reference.startswith("#/"):
        return None
    fragment = reference[1:]
    if re.search(r"%(?![0-9A-Fa-f]{2})", fragment):
        return None
    try:
        pointer = unquote_to_bytes(fragment).decode("utf-8")
    except UnicodeDecodeError:
        return None
    if not pointer.startswith("/"):
        return None
    current: Any = root
    for encoded in pointer[1:].split("/"):
        key = encoded.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict) and key in current:
            current = current[key]
        elif (
            isinstance(current, list)
            and key.isascii()
            and key.isdigit()
            and int(key) < len(current)
        ):
            current = current[int(key)]
        else:
            return None
    return current


def _keyword_values_supported(schema: dict[str, Any]) -> bool:
    if not set(schema).issubset(KNOWN_KEYWORDS):
        return False
    return all(
        keyword not in KEYWORD_VALIDATORS
        or KEYWORD_VALIDATORS[keyword](value)
        for keyword, value in schema.items()
    )


def _schema_children(schema: dict[str, Any], root: dict[str, Any]) -> list[Any] | None:
    children: list[Any] = []
    if "$ref" in schema:
        target = resolve_reference(root, schema["$ref"])
        if target is None:
            return None
        children.append(target)
    for keyword in ("$defs", "properties"):
        if keyword in schema:
            values = schema[keyword]
            if not isinstance(values, dict):
                return None
            children.extend(values.values())
    for keyword in ("anyOf", "allOf", "oneOf"):
        if keyword in schema:
            values = schema[keyword]
            if not isinstance(values, list) or not values:
                return None
            children.extend(values)
    if "items" in schema:
        children.append(schema["items"])
    if "additionalProperties" in schema:
        additional = schema["additionalProperties"]
        if not isinstance(additional, bool):
            children.append(additional)
    if "propertyNames" in schema:
        children.append(schema["propertyNames"])
    return children


def schema_supported(
    schema: Any,
    *,
    _root: dict[str, Any] | None = None,
    _active: frozenset[int] | None = None,
    _memo: dict[tuple[int, int], bool] | None = None,
    _budget: list[int] | None = None,
    _depth: int = 0,
) -> bool:
    if _depth >= 128 or not isinstance(schema, dict):
        return False
    if _root is None and not _json_value_supported(schema):
        return False
    root = schema if _root is None else _root
    active = frozenset() if _active is None else _active
    memo = {} if _memo is None else _memo
    budget = [4096] if _budget is None else _budget
    identity = id(schema)
    if identity in active:
        return True
    memo_key = (identity, _depth)
    if memo_key in memo:
        return memo[memo_key]
    budget[0] -= 1
    if budget[0] < 0:
        return False
    if not _keyword_values_supported(schema):
        memo[memo_key] = False
        return False
    children = _schema_children(schema, root)
    if children is None:
        memo[memo_key] = False
        return False
    child_active = active | {identity}
    supported = all(
        schema_supported(
            child,
            _root=root,
            _active=child_active,
            _memo=memo,
            _budget=budget,
            _depth=_depth + 1,
        )
        for child in children
    )
    memo[memo_key] = supported
    return supported


def _validate_object(
    schema: dict[str, Any],
    value: dict[str, Any],
    path: str,
    root: dict[str, Any],
    seen: set[tuple[int, int]],
    memo: dict[tuple[int, int, str], tuple[str, ...]],
    budget: list[int],
    deadline: float | None,
    depth: int,
) -> list[str]:
    errors: list[str] = []
    properties = schema.get("properties", {})

    def child_errors(child_schema: dict[str, Any], child: Any, child_path: str) -> list[str]:
        return validate(
            child_schema,
            child,
            child_path,
            _root=root,
            _seen=set(seen),
            _memo=memo,
            _budget=budget,
            _deadline=deadline,
            _depth=depth + 1,
        )

    for required in schema.get("required", []):
        if required not in value:
            _record_error(errors, f"{path}: missing required property {required}")
    if schema.get("additionalProperties") is False:
        for key in value:
            if key not in properties:
                _record_error(errors, f"{path}: unexpected property {key}")
    elif isinstance(schema.get("additionalProperties"), dict):
        for key, child in value.items():
            if key not in properties:
                _merge_errors(
                    errors,
                    child_errors(schema["additionalProperties"], child, f"{path}.{key}"),
                )
    if isinstance(schema.get("propertyNames"), dict):
        for key in value:
            _merge_errors(
                errors,
                child_errors(schema["propertyNames"], key, f"{path}.{key}"),
            )
    for key, child in value.items():
        if key in properties:
            _merge_errors(
                errors,
                child_errors(properties[key], child, f"{path}.{key}"),
            )
    return errors


def _validate_array(
    schema: dict[str, Any],
    value: list[Any],
    path: str,
    root: dict[str, Any],
    seen: set[tuple[int, int]],
    memo: dict[tuple[int, int, str], tuple[str, ...]],
    budget: list[int],
    deadline: float | None,
    depth: int,
) -> list[str]:
    errors: list[str] = []
    if "minItems" in schema and len(value) < schema["minItems"]:
        _record_error(errors, f"{path}: too few items")
    if "maxItems" in schema and len(value) > schema["maxItems"]:
        _record_error(errors, f"{path}: too many items")
    if schema.get("uniqueItems"):
        try:
            encoded = [
                _json_identity(item, budget=budget, deadline=deadline) for item in value
            ]
            if len(set(encoded)) != len(encoded):
                _record_error(errors, f"{path}: items are not unique")
        except ValidationLimit:
            _record_error(errors, f"{path}: {VALIDATION_LIMIT}")
    if "items" in schema:
        for index, child in enumerate(value):
            child_errors = validate(
                schema["items"],
                child,
                f"{path}[{index}]",
                _root=root,
                _seen=set(seen),
                _memo=memo,
                _budget=budget,
                _deadline=deadline,
                _depth=depth + 1,
            )
            _merge_errors(errors, child_errors)
            if any(VALIDATION_LIMIT in error for error in child_errors):
                break
    return errors


def _validate_string(schema: dict[str, Any], value: str, path: str) -> list[str]:
    errors: list[str] = []
    if "minLength" in schema and len(value) < schema["minLength"]:
        _record_error(errors, f"{path}: string is too short")
    if "maxLength" in schema and len(value) > schema["maxLength"]:
        _record_error(errors, f"{path}: string is too long")
    if "pattern" in schema:
        try:
            if re.fullmatch(_python_pattern(schema["pattern"]), value) is None:
                _record_error(errors, f"{path}: string does not match pattern")
        except re.error:
            _record_error(errors, f"{path}: invalid pattern")
    return errors


def _validate_number(schema: dict[str, Any], value: int | float, path: str) -> list[str]:
    errors: list[str] = []
    if "minimum" in schema and value < schema["minimum"]:
        _record_error(errors, f"{path}: value is below minimum")
    if "maximum" in schema and value > schema["maximum"]:
        _record_error(errors, f"{path}: value is above maximum")
    if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
        _record_error(errors, f"{path}: value is not above exclusive minimum")
    return errors


def _validate_composition(
    schema: dict[str, Any],
    value: Any,
    path: str,
    root: dict[str, Any],
    seen: set[tuple[int, int]],
    memo: dict[tuple[int, int, str], tuple[str, ...]],
    budget: list[int],
    deadline: float | None,
    depth: int,
) -> list[str]:
    errors: list[str] = []

    def branch_errors(branch: dict[str, Any]) -> list[str]:
        return validate(
            branch,
            value,
            path,
            _root=root,
            _seen=set(seen),
            _memo=memo,
            _budget=budget,
            _deadline=deadline,
            _depth=depth + 1,
        )

    if "$ref" in schema:
        target = resolve_reference(root, schema["$ref"])
        if not isinstance(target, dict):
            return [f"{path}: invalid schema reference"]
        _merge_errors(errors, branch_errors(target))
    for branch in schema.get("allOf", []):
        _merge_errors(errors, branch_errors(branch))
        if any(VALIDATION_LIMIT in error for error in errors):
            break
    if "anyOf" in schema:
        results = [branch_errors(branch) for branch in schema["anyOf"]]
        if not any(not result for result in results):
            if any(any(VALIDATION_LIMIT in error for error in result) for result in results):
                _record_error(errors, f"{path}: {VALIDATION_LIMIT}")
            else:
                _record_error(errors, f"{path}: value does not match any allowed schema")
    if "oneOf" in schema:
        results = [branch_errors(branch) for branch in schema["oneOf"]]
        if any(any(VALIDATION_LIMIT in error for error in result) for result in results):
            _record_error(errors, f"{path}: {VALIDATION_LIMIT}")
        elif sum(not result for result in results) != 1:
            _record_error(errors, f"{path}: value does not match exactly one allowed schema")
    return errors


def _validate_equality_keywords(
    schema: dict[str, Any],
    value: Any,
    path: str,
    budget: list[int],
    deadline: float | None,
) -> list[str]:
    errors: list[str] = []
    if "enum" not in schema and "const" not in schema:
        return errors
    try:
        identity = _json_identity(value, budget=budget, deadline=deadline)
        if "enum" in schema and not any(
            identity == _json_identity(item, budget=budget, deadline=deadline)
            for item in schema["enum"]
        ):
            _record_error(errors, f"{path}: value is not in enum")
        if "const" in schema and identity != _json_identity(
            schema["const"], budget=budget, deadline=deadline
        ):
            _record_error(errors, f"{path}: value does not match const")
    except ValidationLimit:
        _record_error(errors, f"{path}: {VALIDATION_LIMIT}")
    return errors


TYPE_MATCHERS = {
    "object": lambda value: isinstance(value, dict),
    "array": lambda value: isinstance(value, list),
    "string": lambda value: isinstance(value, str),
    "integer": _is_integer,
    "number": _is_number,
    "boolean": lambda value: isinstance(value, bool),
    "null": lambda value: value is None,
}


def _type_errors(schema: dict[str, Any], value: Any, path: str) -> list[str]:
    expected = schema.get("type")
    if expected is None:
        return []
    expected_types = schema_types(expected)
    if expected_types is None:
        return [f"{path}: invalid schema type"]
    if any(TYPE_MATCHERS[item](value) for item in expected_types):
        return []
    return [f"{path}: expected {' or '.join(expected_types)}"]


def _value_errors(
    schema: dict[str, Any],
    value: Any,
    path: str,
    root: dict[str, Any],
    seen: set[tuple[int, int]],
    memo: dict[tuple[int, int, str], tuple[str, ...]],
    budget: list[int],
    deadline: float | None,
    depth: int,
) -> list[str]:
    if isinstance(value, dict):
        return _validate_object(
            schema, value, path, root, seen, memo, budget, deadline, depth
        )
    if isinstance(value, list):
        return _validate_array(
            schema, value, path, root, seen, memo, budget, deadline, depth
        )
    if isinstance(value, str):
        return _validate_string(schema, value, path)
    if _is_number(value):
        return _validate_number(schema, value, path)
    return []


def validate(
    schema: dict[str, Any],
    value: Any,
    path: str = "$",
    *,
    _root: dict[str, Any] | None = None,
    _seen: set[tuple[int, int]] | None = None,
    _memo: dict[tuple[int, int, str], tuple[str, ...]] | None = None,
    _budget: list[int] | None = None,
    _deadline: float | None = None,
    _depth: int = 0,
) -> list[str]:
    root = schema if _root is None else _root
    seen = set() if _seen is None else _seen
    memo = {} if _memo is None else _memo
    budget = [4096] if _budget is None else _budget
    memo_key = (id(schema), id(value), path)
    budget[0] -= 1
    if (
        _depth >= 128
        or budget[0] < 0
        or (_deadline is not None and time.monotonic() >= _deadline)
    ):
        return [f"{path}: {VALIDATION_LIMIT}"]
    if memo_key in memo:
        return list(memo[memo_key])
    identity = (id(schema), id(value))
    if identity in seen:
        return []
    seen.add(identity)
    errors = _validate_composition(
        schema, value, path, root, seen, memo, budget, _deadline, _depth
    )
    type_errors = _type_errors(schema, value, path)
    if type_errors:
        memo[memo_key] = tuple(type_errors)
        return type_errors
    _merge_errors(
        errors, _validate_equality_keywords(schema, value, path, budget, _deadline)
    )
    _merge_errors(
        errors,
        _value_errors(
            schema, value, path, root, seen, memo, budget, _deadline, _depth
        ),
    )
    memo[memo_key] = tuple(errors)
    return errors
