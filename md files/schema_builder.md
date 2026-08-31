# Schema Builder Architecture

This document explains how `backend/schema_builder.py` dynamically constructs Pydantic models from `backend/fields.yaml` and why this design is used.

---

## 1. Purpose

`schema_builder.py` is responsible for building a validation schema for insurance document types other than `life`.

The key goals are:
* Support multiple insurance categories (`car`, `travel`, `health`, `property`) with a single dynamic engine.
* Keep all field definitions, formats, and required flags in a single declarative YAML file.
* Enforce validation rules consistently without hand-coding one model per document type.
* Maintain a soft-fail/repair-friendly design that matches the overall extraction pipeline strategy.

---

## 2. How It Works

### Input Source: `fields.yaml`

`fields.yaml` is the source of truth. Each document type section contains a list of field definitions.

Example shape:
```yaml
car:
  - name: registration_number
    required: true
    format: free_text
  - name: engine_number
    required: false
    format: free_text
```

Each field can define:
* `name` — the JSON key expected from the LLM
* `required` — whether the value must be present and non-empty
* `format` — a semantic validation rule such as `date`, `phone`, `email`, `currency`, `percent`, or `integer`

### Build Process

`build_model(doc_type)` performs these steps:
1. Load `fields.yaml` and cache it in the module-scoped `FIELDS` dictionary.
2. Check if a model for `doc_type` is already in `_model_cache`.
   * If cached, return the existing model immediately.
3. Validate `doc_type` exists in `FIELDS`.
4. Build `field_kwargs` for Pydantic using:
   * `(str, ...)` for `required: true`
   * `(str, "")` for optional fields
5. Create validators for:
   * required-value enforcement
   * format validation based on the declared field type
6. Call `pydantic.create_model()` with the dynamic fields and validators.
7. Cache and return the generated model.

This allows the pipeline to validate multiple document types without a static Python class for every type.

---

## 3. Format Validation

`schema_builder.py` supports format checks by mapping field format names to validator functions.

Supported format checks:
* `date`
* `phone`
* `email`
* `currency`
* `percent`
* `integer`

A field with no recognized format or `free_text` receives no additional check.

### Example format validators

`_check_date(v)`:
* Accepts common date styles such as `DD/MM/YYYY`, `DD/MM/YY`, `DD-MMM-YYYY`, `DD-MMM-YY`, `DD-MM-YYYY`, `Month DD, YYYY`.
* Normalizes to `DD/MM/YYYY` if parse succeeds.
* Returns `""` on failure, allowing the field to remain present but empty.

`_check_phone(v)`:
* Strips non-digits.
* Accepts only 9-11 digit phone numbers.
* Returns empty string on invalid phone values.

`_check_email(v)`:
* Ensures the string contains a dot and at least one alphanumeric character.
* Returns empty string if the input is not a plausible email.

`_check_currency(v)`:
* Ensures the string contains at least one digit.
* Leaves the input unchanged if valid, otherwise returns empty string.

`_check_percent(v)`:
* Strips a trailing `%` and validates the remaining value as a float.
* Returns empty string for invalid values.

`_check_integer(v)`:
* Accepts only signed integers.
* Returns empty string for invalid values.

### Why formats return `""` instead of raising

This design matches the pipeline's tolerant style:
* The extractor prefers to preserve partially valid outputs.
* Invalid field formats are converted to empty strings rather than breaking the whole document.
* Required fields are still strictly enforced when `required: true` is set.

---

## 4. Required Field Handling

Pydantic's default behavior for `str` fields is to require the key, but not to reject an empty string.

To make `required: true` meaningful, schema_builder adds a custom validator using `_make_required_check(field_name)`.

This validator:
* Raises a `ValueError` when the field value is empty or missing.
* Ensures required fields behave like `min_length=1`.
* Applies only to fields marked `required: true`.

This is particularly important for insurance extraction where certain fields must not be empty.

---

## 5. Model Caching

Generated models are stored in the `_model_cache` dictionary.

Why cache?
* Avoid rebuilding the same Pydantic model on every request.
* Improve API latency for repeated document processing.
* Keep the pipeline efficient while still using dynamic schema generation.

Caching is keyed by `doc_type`, so each document type model is built once per process.

---

## 6. Life Type Exception

`schema_builder.py` is intentionally not used for `doc_type == "life"`.

Why?
* The life insurance schema uses a hand-tuned Pydantic model in `backend/extraction_schema.py`.
* That model contains more specific validation and required-field handling for life policies.
* `llm_extractor.py` dispatches `life` to `extraction_schema.PolicyExtraction` instead of `build_model("life")`.

This means `schema_builder.py` is the generic validator for all other types only.

---

## 7. Helper API

`get_field_names(doc_type)` returns the list of field names defined in `fields.yaml` for a specific document type.

Usage example:
```python
from schema_builder import get_field_names

fields = get_field_names("car")
print(fields)
```

This is useful for downstream code that needs to know the expected keys without instantiating the model.

---

## 8. Example Usage

### Build and validate a model
```python
from schema_builder import build_model

CarModel = build_model("car")
validated = CarModel.model_validate({
    "registration_number": "ABC123",
    "engine_number": "ENG987"
})
```

### Handle a validation error
```python
from pydantic import ValidationError

try:
    validated = CarModel.model_validate({
        "registration_number": "",
        "engine_number": "ENG987"
    })
except ValidationError as e:
    print(e)
```

---

## 9. Design Decisions

### Why YAML-driven schemas?
* Centralizes field definitions in a single file.
* Makes it easy to add or modify document types without changing Python code.
* Supports non-developers updating field names, required flags, and format rules.

### Why dynamic model creation?
* Avoids repetitive static Pydantic class definitions.
* Keeps validation logic consistent across document types.
* Enables the same pipeline code to validate any supported insurance type.

### Why soft validation?
* The extraction pipeline is designed to tolerate partial data.
* Invalid formats should be cleaned or emptied, not cause a complete failure.
* Required fields are still enforced, while optional fields remain flexible.

---

## 10. Related Files

* `backend/fields.yaml` — field definitions driving this module.
* `backend/llm_extractor.py` — uses `build_model()` for non-life types.
* `backend/extraction_schema.py` — hand-tuned schema for `life` documents.

---

## 11. Troubleshooting

### `KeyError: 'travel' has no entry in fields.yaml`
* Means the requested doc type is missing from `fields.yaml`.
* Add the doc type and field definitions to `fields.yaml` before running again.

### `ValueError: 'field' is required and cannot be empty`
* Means that field is marked `required: true` in `fields.yaml`.
* Provide a non-empty string value for that field.

### `ImportError: No module named 'yaml'`
* Install the YAML parser dependency by running:
```powershell
.venv\Scripts\pip install pyyaml
```
