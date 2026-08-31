import os
import json
import argparse
import re
import requests
import sys
import time
import yaml

from pipeline import DocumentProcessorPipeline
from precleaning import DocumentCleaner
from classification import classify_document_type
from schema_builder import build_model
from pydantic import ValidationError

DEFAULT_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "google/gemma-4-31b-it:free"

_current_dir = os.path.dirname(os.path.abspath(__file__))
_prompts_yaml_path = os.path.join(_current_dir, "prompts.yaml")
if not os.path.exists(_prompts_yaml_path):
    raise FileNotFoundError(f"Could not find prompts.yaml at: {_prompts_yaml_path}")

with open(_prompts_yaml_path, "r", encoding="utf-8") as _f:
    PROMPTS = yaml.safe_load(_f)

# Fields removed from the life schema — strip them from LLM output so they
# never appear in the JSON, frontend, or evaluation report.
_LIFE_REMOVED_FIELDS = {"policy_type", "policy_status"}


def get_document_text(file_path, extraction_mode="both"):
    print(f"[*] Step 1: Running pipeline raw extraction on: {file_path}")
    pipeline = DocumentProcessorPipeline()
    raw_result = pipeline.run(file_path, extraction_mode=extraction_mode)

    print(f"[*] Step 2: Running in-memory cleaning pipeline...")
    cleaner = DocumentCleaner(drop_garbage=False)
    cleaned_result = cleaner.clean(raw_result)

    full_text = []
    for page in cleaned_result["pages"]:
        page_text = "\n".join(block["text"] for block in page["text_sources"])
        full_text.append(f"--- PAGE {page['page_number']} ---\n{page_text}")

    return "\n\n".join(full_text)


def query_llm_openrouter(document_text, api_key, model, system_prompt, user_prompt_template):
    print(f"[*] Step 4: Sending extraction prompt to OpenRouter ({model})...")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/atharv-suryavanshi06/Insurance-Document-Processor",
        "X-Title": "Insurance Document Processor"
    }

    user_prompt = user_prompt_template.format(document_text=document_text)

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "temperature": 0.1,
        "max_tokens": 4096,
        "response_format": {"type": "json_object"}
    }

    start_time = time.time()
    response = requests.post(DEFAULT_OPENROUTER_URL, headers=headers, json=payload)
    latency = time.time() - start_time

    if response.status_code == 429 and model.endswith(":free"):
        fallback_model = model[:-5]
        print(f"\n[!] Rate-limit (429) for free model '{model}'. Falling back to '{fallback_model}'...")
        return query_llm_openrouter(document_text, api_key, fallback_model, system_prompt, user_prompt_template)

    if response.status_code == 402:
        result_json = response.json()
        err_msg = result_json.get("error", {}).get("message", response.text)
        raise RuntimeError(f"[402] Insufficient OpenRouter credits: {err_msg}")

    if response.status_code != 200:
        raise RuntimeError(f"OpenRouter API call failed with status {response.status_code}: {response.text}")

    result_json = response.json()
    if "choices" not in result_json or len(result_json["choices"]) == 0:
        raise ValueError(f"Invalid or empty response structure from OpenRouter: {result_json}")

    usage = result_json.get("usage") or {}
    prompt_tokens = usage.get("prompt_tokens", "N/A")
    completion_tokens = usage.get("completion_tokens", "N/A")
    total_tokens = usage.get("total_tokens", "N/A")
    print(f"[*] LLM call latency: {latency:.2f}s")
    print(f"[*] Token usage -> input: {prompt_tokens}, output: {completion_tokens}, total: {total_tokens}")

    return result_json["choices"][0]["message"]["content"]


def load_env_file():
    search_paths = [
        ".env",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    ]
    for env_path in search_paths:
        if os.path.exists(env_path):
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        key, val = line.split("=", 1)
                        key = key.strip()
                        val = val.strip().strip('"').strip("'")
                        os.environ[key] = val
            break


def _parse_llm_json(raw_content):
    try:
        parsed = json.loads(raw_content.strip())
    except json.JSONDecodeError:
        cleaned = raw_content.strip()
        if cleaned.startswith("```"):
            lines = cleaned.split("\n")
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            cleaned = "\n".join(lines).strip()
        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError:
            print("\n[!] Warning: LLM output was not valid JSON. Raw output below:")
            print(raw_content)
            return None

    # Coerce any non-string values (e.g. -1, null, 0) to ""
    # Some models return -1 or null instead of "" for missing fields
    if isinstance(parsed, dict):
        parsed = {k: ("" if not isinstance(v, str) else v) for k, v in parsed.items()}

    return parsed
def _validate_life_extraction(parsed_json, document_text=None):
    # Drop removed fields and internal meta before schema validation
    clean = {
        k: v for k, v in parsed_json.items()
        if k not in _LIFE_REMOVED_FIELDS and k != "_extraction_meta"
    }
    model_cls = build_model("life")
    return model_cls.model_validate(clean)



def validate_extraction(doc_type, parsed_json, document_text=None):
    try:
        if doc_type == "life":
            validated = _validate_life_extraction(parsed_json, document_text=document_text)
        else:
            model_cls = build_model(doc_type)
            validated = model_cls.model_validate(parsed_json)

        return validated

    except ValidationError as e:
        print("\n[!] Schema validation failed (soft fallback):")
        for err in e.errors():
            print(f"    {'.'.join(str(p) for p in err['loc'])}: {err['msg']}")

        # For life: patch empty required fields and retry once
        if doc_type == "life":
            patched = dict(parsed_json)
            if not patched.get("insurer_name"):
                patched["insurer_name"] = "Unknown"
            if not patched.get("intermediary_name"):
                patched["intermediary_name"] = "Unknown"
            try:
                return _validate_life_extraction(patched, document_text=document_text)
            except ValidationError:
                pass

        # Last resort: return raw JSON wrapped so both
        # evaluation.py (.model_dump()) and server.py (.model_dump_json()) work.
        # Strip removed life fields so they never leak through even in the fallback.
        from types import SimpleNamespace
        stripped = {
            k: v for k, v in parsed_json.items()
            if not (doc_type == "life" and k in _LIFE_REMOVED_FIELDS)
        }
        ns = SimpleNamespace()
        ns.model_dump = lambda: dict(stripped)
        ns.model_dump_json = lambda indent=2, by_alias=False: json.dumps(stripped, indent=indent)
        print("[!] Returning raw extracted fields without schema validation.")
        return ns


def process_document(file_path, api_key, model, extraction_mode="both"):
    overall_start = time.time()
    document_text = get_document_text(file_path, extraction_mode=extraction_mode)

    print(f"[*] Step 3: Classifying document type...")
    doc_type = classify_document_type(document_text, api_key, model)
    print(f"    -> doc_type = {doc_type}")

    type_prompts = PROMPTS.get(doc_type) or {}
    system_prompt = type_prompts.get("system_prompt") or ""
    user_prompt_template = type_prompts.get("user_prompt_template") or ""
    if not system_prompt or not user_prompt_template:
        raise ValueError(
            f"No prompts configured yet for doc_type='{doc_type}' in prompts.yaml."
        )

    extracted_content = query_llm_openrouter(document_text, api_key, model, system_prompt, user_prompt_template)
    parsed_json = _parse_llm_json(extracted_content)

    if parsed_json is None:
        try:
            from db import init_db_pool, save_extraction_result
            init_db_pool()
            save_extraction_result(
                filename=os.path.basename(file_path),
                doc_type=doc_type,
                status="failed",
                validated_data={}
            )
        except Exception as db_err:
            print(f"[!] Database save error (on failure): {db_err}")
        print(f"[*] Total processing time: {time.time() - overall_start:.2f}s")
        return doc_type, None

    validated = validate_extraction(doc_type, parsed_json, document_text=document_text)

    try:
        from db import init_db_pool, save_extraction_result
        init_db_pool()
        filename = os.path.basename(file_path)
        data_dict = validated.model_dump() if hasattr(validated, "model_dump") else json.loads(validated.model_dump_json(by_alias=True))
        save_extraction_result(
            filename=filename,
            doc_type=doc_type,
            status="success",
            validated_data=data_dict
        )
    except Exception as db_err:
        print(f"[!] Database save error (on success): {db_err}")

    print(f"[*] Total processing time: {time.time() - overall_start:.2f}s")
    return doc_type, validated


def main():
    load_env_file()

    parser = argparse.ArgumentParser(description="Insurance Document LLM Extractor (OpenRouter)")
    parser.add_argument("file_path", type=str, nargs="?", help="Path to the PDF, Image, or Word document to parse")
    parser.add_argument("--model", type=str, default=None, help="OpenRouter model identifier")
    parser.add_argument("--api-key", type=str, default=None, help="OpenRouter API Key")
    args = parser.parse_args()

    api_key = args.api_key or os.environ.get("OPENROUTER_API_KEY")
    model = args.model or os.environ.get("OPENROUTER_MODEL") or DEFAULT_MODEL

    if not api_key:
        print("[!] Error: OpenRouter API key not found.")
        sys.exit(1)

    input_path = args.file_path
    if not input_path:
        while True:
            input_path = input("Enter path to file: ").strip().strip('"').strip("'")
            if os.path.exists(input_path):
                break
            print(f"[!] File not found: {input_path}")

    try:
        process_document(input_path, api_key, model)
    except Exception as e:
        print(f"\n[!] Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()