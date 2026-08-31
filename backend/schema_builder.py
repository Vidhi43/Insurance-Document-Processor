"""
schema_builder.py

Builds a Pydantic v2 model per insurance document type, driven entirely by
fields.yaml. Every field is typed as str (matching the soft-fail-to-empty-
string philosophy already used in extraction_schema.py for the 'life'
type): if a field's value doesn't match its declared `format`, it's coerced
to "" rather than raising the whole record -- except fields marked
`required: true`, which raise if missing/empty, the same hard-fail behavior
extraction_schema.py uses for insurer_name/intermediary_name.


Usage:
    from schema_builder import build_model
    CarExtraction = build_model("car")
    validated = CarExtraction.model_validate(parsed_json)
"""

import os
import re
from datetime import datetime
from typing import Dict, Type

import yaml
from pydantic import BaseModel, create_model, field_validator

_current_dir = os.path.dirname(os.path.abspath(__file__))
_fields_yaml_path = os.path.join(_current_dir, "fields.yaml")

if not os.path.exists(_fields_yaml_path):
    raise FileNotFoundError(f"Could not find fields.yaml at: {_fields_yaml_path}")

with open(_fields_yaml_path, "r", encoding="utf-8") as _f:
    FIELDS: dict = yaml.safe_load(_f)

# Tolerant of the date formats seen across reviewed sample documents so far:
# DD/MM/YYYY (life, travel), DD-MMM-YYYY / DD-MMM-YY (car). Extend this list
# rather than adding a second "format" tag if a new variant shows up.
_DATE_FORMATS = [
    "%d/%m/%Y", "%d/%m/%y",
    "%d-%b-%Y", "%d-%b-%y",
    "%d-%m-%Y",
    "%B %d, %Y", "%b %d, %Y",
]

def _check_date(v: str) -> str:
    if not v:
        return v

    cleaned = v.strip()
    cleaned = re.sub(r"(\d+)(st|nd|rd|th)", r"\1", cleaned, flags=re.I)
    cleaned = re.sub(r"\s+\d{1,2}:\d{2}.*$", "", cleaned).strip()

    for fmt in _DATE_FORMATS:
        try:
            dt = datetime.strptime(cleaned, fmt)
            return dt.strftime("%d/%m/%Y")
        except ValueError:
            continue

    return ""


def _check_phone(v: str) -> str:
    if not v:
        return v
    digits = re.sub(r"\D", "", v)
    return digits if 9 <= len(digits) <= 11 else ""


def _check_email(v: str) -> str:
    if not v:
        return v
    return v if ("." in v and any(c.isalnum() for c in v)) else ""


def _check_currency(v: str) -> str:
    if not v:
        return v
    return v if re.search(r"\d", v) else ""


def _check_percent(v: str) -> str:
    if not v:
        return v
    cleaned = v.strip().rstrip("%").strip()
    try:
        float(cleaned)
        return v
    except ValueError:
        return ""


def _check_integer(v: str) -> str:
    if not v:
        return v
    return v if re.fullmatch(r"-?\d+", v.strip()) else ""


_FORMAT_CHECKS = {
    "date": _check_date,
    "phone": _check_phone,
    "email": _check_email,
    "currency": _check_currency,
    "percent": _check_percent,
    "integer": _check_integer,
    # "free_text" or any unrecognized format -> no extra validation
}

_model_cache: Dict[str, Type[BaseModel]] = {}


def _make_validator(check_fn):
    """
    Factory so each generated validator closes over its own check_fn
    correctly. Defining the inner function directly inside the build_model
    loop would cause every validator to share the SAME check_fn (the last
    one assigned in the loop) due to Python's late-binding closures -- this
    factory pattern avoids that bug.
    """
    def _validator(cls, v):
        return check_fn(v)
    return _validator


def _make_required_check(field_name: str):
    """
    `default=...` on a str field only enforces that the KEY is present in
    the input -- it does NOT reject an explicitly-passed empty string. This
    closes that gap so `required: true` in fields.yaml actually hard-fails
    on "" the same way extraction_schema.py's min_length=1 does for
    insurer_name/intermediary_name.
    """
    def _validator(cls, v):
        if not v:
            raise ValueError(f"'{field_name}' is required and cannot be empty")
        return v
    return _validator


def build_model(doc_type: str) -> Type[BaseModel]:
    """
    Returns (and caches) a Pydantic model for doc_type, built from
    fields.yaml. Do not call this for doc_type == "life" -- use
    extraction_schema.PolicyExtraction directly for that type instead.
    """
    if doc_type in _model_cache:
        return _model_cache[doc_type]

    if doc_type not in FIELDS:
        raise KeyError(f"'{doc_type}' has no entry in fields.yaml")

    field_defs = FIELDS[doc_type] or []
    if not field_defs:
        raise ValueError(
            f"fields.yaml['{doc_type}'] is empty -- add field definitions "
            f"before extracting this type."
        )

    field_kwargs = {}
    validators = {}

    for f in field_defs:
        name = f["name"]
        required = bool(f.get("required", False))
        field_kwargs[name] = (str, ... if required else "")

        if required:
            validators[f"check_{name}_required"] = field_validator(name)(_make_required_check(name))

        fmt = f.get("format", "free_text")
        check_fn = _FORMAT_CHECKS.get(fmt)
        if check_fn:
            validators[f"check_{name}_format"] = field_validator(name)(_make_validator(check_fn))

    model = create_model(
        doc_type.capitalize() + "Extraction",
        __validators__=validators,
        **field_kwargs,
    )
    _model_cache[doc_type] = model
    return model


def get_field_names(doc_type: str):
    return [f["name"] for f in (FIELDS.get(doc_type) or [])]


__all__ = ["build_model", "get_field_names", "FIELDS"]
