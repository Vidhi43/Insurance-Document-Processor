"""
classification.py

LLM-based doc type classifier with keyword pre-screening.
Falls back to "life" (most common type) on any failure.
"""

import json
import re
import requests

CLASSIFY_URL = "https://openrouter.ai/api/v1/chat/completions"
VALID_TYPES = {"car", "life", "health", "property", "travel"}

# Fast keyword pre-screen — avoids an LLM call for obvious cases.
# Checked BEFORE the LLM. First match wins.
_KEYWORD_RULES = [
    ("travel",   [r"pnr\s*number", r"train\s*name", r"originating\s*station", r"irctc"]),
    ("car",      [r"registration\s*number", r"engine\s*number", r"chassis\s*number", r"two.wheeler", r"vehicle\s*idv"]),
    ("health",   [r"basic\s*floater\s*sum\s*insured", r"scheme\s*description", r"irdai\s*regn", r"star\s*health", r"family\s*accident\s*care"]),
    ("property", [r"the\s*business\s*:", r"mast\.\s*pol\.\s*number", r"multisure", r"broadform\s*liability", r"sasria"]),
    # life last — broad terms; only wins if nothing above matched
    ("life",     [r"personal\s*scheme", r"omi.multisure", r"quicksure\s*personal", r"policy\s*number\s*\(broker\)", r"intermediary\s*:", r"period\s*of\s*insurance", r"policyholder\s*details", r"the\s*insured\s*:", r"insured.s\s*date\s*of\s*birth", r"occupation\s*of\s*the\s*insured"]),
]

_SYSTEM_PROMPT = (
    "You are a document classifier. Read the insurance document text and "
    "decide which ONE of these 5 types it is: car, life, health, property, travel.\n"
    "- life: personal/home/contents policy schedule (Old Mutual, Quicksure, OMI-MULTISURE, Personal Scheme)\n"
    "- car: motor/vehicle/two-wheeler policy\n"
    "- health: medical/accident/hospitalization insurance\n"
    "- property: commercial property, business, Multisure commercial binder\n"
    "- travel: train/flight travel insurance certificate (IRCTC)\n"
    'Return ONLY: {"doc_type": "<type>"}'
)


def _keyword_classify(text: str) -> str:
    t = text[:6000].lower()
    # travel/car/health/property need 2 hits (specific enough)
    # life needs only 1 hit (catches video/fragmented OCR)
    for doc_type, patterns in _KEYWORD_RULES:
        threshold = 1 if doc_type == "life" else 2
        if sum(1 for p in patterns if re.search(p, t)) >= threshold:
            return doc_type
    return ""


def _strip_fence(content: str) -> str:
    content = content.strip()
    if content.startswith("```"):
        lines = content.split("\n")
        lines = lines[1:] if lines[0].startswith("```") else lines
        lines = lines[:-1] if lines and lines[-1].startswith("```") else lines
        content = "\n".join(lines).strip()
    return content


def classify_document_type(document_text: str, api_key: str, model: str) -> str:
    # 1. Fast keyword pre-screen
    kw = _keyword_classify(document_text)
    if kw:
        return kw

    # 2. LLM fallback
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": document_text[:4000]},
        ],
        "temperature": 0.0,
        "response_format": {"type": "json_object"},
    }

    try:
        response = requests.post(CLASSIFY_URL, headers=headers, json=payload, timeout=30)

        if response.status_code == 429 and model.endswith(":free"):
            return classify_document_type(document_text, api_key, model[:-5])

        if response.status_code != 200:
            raise RuntimeError(f"Classification failed ({response.status_code})")

        result_json = response.json()
        if "choices" not in result_json or not result_json["choices"]:
            raise ValueError("Empty response")

        content = _strip_fence(result_json["choices"][0]["message"]["content"])
        doc_type = ""

        try:
            doc_type = str(json.loads(content).get("doc_type", "")).strip().lower()
        except json.JSONDecodeError:
            pass

        if doc_type not in VALID_TYPES:
            match = re.search(r"\b(car|life|health|property|travel)\b", content.lower())
            doc_type = match.group(1) if match else ""

        # Final fallback: re-run keyword screen on the full text, then default life
        return doc_type or _keyword_classify(document_text) or "life"

    except Exception:
        return _keyword_classify(document_text) or "life"


__all__ = ["classify_document_type", "VALID_TYPES"]