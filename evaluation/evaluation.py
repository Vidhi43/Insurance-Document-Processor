import os
import sys
import json
import argparse
import glob as glob_module

backend_dir = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "backend")
)
sys.path.insert(0, backend_dir)

from llm_extractor import process_document, load_env_file
from rapidfuzz import fuzz

# Fields permanently removed from the life schema.
# Stripped from GT expected dicts at load time so they never appear in the
# report or affect any metric, even if an old gt file still contains them.
_REMOVED_FIELDS = {"policy_type", "policy_status"}

# Groq models this harness can evaluate. Pick one at the start of the run.
AVAILABLE_MODELS = [
    "qwen/qwen3-32b",
    "llama-3.3-70b-versatile",
]

# Paths
performance_dir = os.path.dirname(os.path.abspath(__file__))
project_root    = os.path.abspath(os.path.join(performance_dir, ".."))
data_dir        = os.path.join(project_root, "data")
STATE_PATH      = os.path.join(performance_dir, "evaluation_state.json")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_file_format(filename):
    ext = os.path.splitext(filename)[1].lower()
    mapping = {
        ".pdf":  "pdf",
        ".png":  "image", ".jpg": "image", ".jpeg": "image",
        ".tiff": "image", ".bmp": "image", ".webp": "image",
        ".doc":  "word",  ".docx": "word",
        ".wav":  "audio", ".mp3": "audio", ".aac": "audio",
        ".m4a":  "audio", ".flac": "audio",
        ".mp4":  "video", ".avi": "video", ".mov": "video", ".mkv": "video",
    }
    return mapping.get(ext, "unknown")


def sanitize_model_name(model):
    """Turn a model id like 'qwen/qwen3-32b' into a filename-safe tag."""
    return model.replace("/", "_")


def find_actual_file(filename, search_dir):
    """Exact match first, then normalised match (case / underscores / spaces)."""
    if os.path.exists(os.path.join(search_dir, filename)):
        return filename

    def normalize(name):
        return name.lower().replace("_", "").replace("+", "").replace(" ", "")

    target = normalize(filename)
    if not os.path.exists(search_dir):
        return None
    for f in os.listdir(search_dir):
        if normalize(f) == target:
            return f
    return None


# ---------------------------------------------------------------------------
# Model selection
# ---------------------------------------------------------------------------

def select_model():
    """
    List the supported Groq models and let the user pick one to evaluate.
    Returns the chosen model identifier string.
    """
    print("\nAvailable models to test:")
    for i, m in enumerate(AVAILABLE_MODELS, 1):
        print(f"  [{i}] {m}")

    while True:
        choice = input(f"\nSelect model [1-{len(AVAILABLE_MODELS)}]: ").strip()
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(AVAILABLE_MODELS):
                selected = AVAILABLE_MODELS[idx]
                print(f"[*] Selected model: {selected}")
                return selected
        except ValueError:
            pass
        print(f"    Invalid choice. Enter a number between 1 and {len(AVAILABLE_MODELS)}.")


# ---------------------------------------------------------------------------
# GT file selection
# ---------------------------------------------------------------------------

def select_gt_file():
    """
    List every gt*.json in the evaluation folder and let the user pick one.
    Returns the full path to the chosen file.
    """
    gt_files = sorted(glob_module.glob(os.path.join(performance_dir, "gt*.json")))
    if not gt_files:
        print("[!] No gt*.json files found in the evaluation folder.")
        sys.exit(1)

    if len(gt_files) == 1:
        print(f"[*] Only one ground truth file found — using: {os.path.basename(gt_files[0])}")
        return gt_files[0]

    print("\nAvailable ground truth files:")
    for i, f in enumerate(gt_files, 1):
        print(f"  [{i}] {os.path.basename(f)}")

    while True:
        choice = input(f"\nSelect GT file [1-{len(gt_files)}]: ").strip()
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(gt_files):
                selected = gt_files[idx]
                print(f"[*] Selected: {os.path.basename(selected)}")
                return selected
        except ValueError:
            pass
        print(f"    Invalid choice. Enter a number between 1 and {len(gt_files)}.")


# ---------------------------------------------------------------------------
# Persistent state (accumulated across multiple runs)
# ---------------------------------------------------------------------------

def load_state(state_path=None):
    """
    State schema
    ------------
    field_all_similarities : {field: [sim, sim, ...]}   -- raw similarity per counted field hit
    field_correct_counts   : {field: int}               -- number of exact (100%) hits
    format_metrics         : {fmt: {total, correct, similarity}}
    overall_total          : int
    overall_correct        : int
    overall_similarity     : float
    per_file_report_lines  : [str, ...]                 -- all per-file text accumulated
    """
    state_path = state_path or STATE_PATH
    if os.path.exists(state_path):
        with open(state_path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "field_all_similarities": {},
        "field_correct_counts":   {},
        "format_metrics":         {},
        "overall_total":          0,
        "overall_correct":        0,
        "overall_similarity":     0.0,
        "per_file_report_lines":  [],
    }


def save_state(state, state_path=None):
    state_path = state_path or STATE_PATH
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def clear_state(state_path=None):
    state_path = state_path or STATE_PATH
    if os.path.exists(state_path):
        os.remove(state_path)
        print("[*] Evaluation state cleared.")
    else:
        print("[*] No accumulated state found — nothing to clear.")


# ---------------------------------------------------------------------------
# Report writer  (always rewrites the whole file from accumulated state)
# ---------------------------------------------------------------------------

def build_report(state, output_path):
    lines = []
    lines.append("======================================================================")
    lines.append(" EVALUATION REPORT")
    lines.append("======================================================================\n")

    # All per-file sections from every run
    lines.extend(state["per_file_report_lines"])

    # ---- Combined field-wise accuracy ----
    lines.append("======================================================================")
    lines.append(" COMBINED FIELD-WISE ACCURACY (Average across all tested files)")
    lines.append("======================================================================\n")

    for field, scores in sorted(state["field_all_similarities"].items()):
        avg_score   = sum(scores) / len(scores)
        scores_str  = " + ".join(str(round(s)) for s in scores)
        formula_str = f"({scores_str})/{len(scores)}"
        lines.append(f"{field:30s}: {avg_score:.2f}%  (formula: {formula_str})")

    # ---- Document-type-wise accuracy ----
    lines.append("\n======================================================================")
    lines.append(" DOCUMENT-TYPE-WISE ACCURACY")
    lines.append(" (pdf / image / word / audio / video)")
    lines.append("======================================================================\n")

    for fmt, result in state["format_metrics"].items():
        if result["total"] == 0:
            continue
        exact      = result["correct"]    / result["total"] * 100
        similarity = result["similarity"] / result["total"]
        lines.append(fmt.upper())
        lines.append(f"  Fields Compared : {result['total']}")
        lines.append(f"  Exact Accuracy  : {exact:.2f}%")
        lines.append(f"  Avg Similarity  : {similarity:.2f}%")
        lines.append("")

    # ---- Project-wide overall metrics ----
    ot  = state["overall_total"]
    oc  = state["overall_correct"]
    os_ = state["overall_similarity"]
    overall_exact          = (oc  / ot * 100) if ot else 0.0
    overall_avg_similarity = (os_ / ot)       if ot else 0.0

    lines.append("\n======================================================================")
    lines.append(" PROJECT-WIDE OVERALL METRICS")
    lines.append("======================================================================\n")
    lines.append(f"Overall Exact Accuracy : {overall_exact:.2f}%")
    lines.append(f"Overall Avg Similarity : {overall_avg_similarity:.2f}%")
    lines.append(f"Total Fields Compared  : {ot}")
    lines.append("----------------------------------------------------------------------\n")

    with open(output_path, "w", encoding="utf-8") as f_out:
        f_out.write("\n".join(lines))

    print(f"\n[+] Evaluation report saved to: {output_path}")


# ---------------------------------------------------------------------------
# Core evaluation loop
# ---------------------------------------------------------------------------

def evaluate(limit=None, target_file=None, output_path=None, gt_path=None, model=None):

    load_env_file()

    # Select model to test ---------------------------------------------------
    if model is None:
        model = select_model()

    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        print("[!] GROQ_API_KEY not found. Add it to your .env file.")
        return

    model_tag = sanitize_model_name(model)
    state_path = os.path.join(performance_dir, f"evaluation_state_{model_tag}.json")

    # Select GT file --------------------------------------------------------
    if gt_path is None:
        gt_path = select_gt_file()

    gt_name = os.path.basename(gt_path)

    if not os.path.exists(gt_path):
        print(f"[!] Ground truth file not found: {gt_path}")
        return

    with open(gt_path, "r", encoding="utf-8") as f:
        gt = json.load(f)

    # Strip removed fields from every expected dict so they never appear in
    # the report, regardless of whether the gt file has been updated.
    for doc in gt.values():
        for field in _REMOVED_FIELDS:
            doc.get("expected", {}).pop(field, None)

    if output_path is None:
        output_path = os.path.join(performance_dir, f"evaluation_report_{model_tag}.txt")

    # Optionally narrow to a single file
    if target_file:
        if target_file not in gt:
            print(f"[!] File '{target_file}' not found in {gt_name}.")
            return
        gt = {target_file: gt[target_file]}

    # Load accumulated state ------------------------------------------------
    state = load_state(state_path)

    # Section header for this run (goes into per_file_report_lines)
    run_lines = [
        "======================================================================",
        f" RUN: {gt_name}",
        "======================================================================\n",
    ]

    # This-run-only metrics for console output
    run_field_similarities = {}   # {field: [sim, ...]}
    run_field_correct      = {}   # {field: int}
    run_format_metrics     = {}
    run_total = run_correct = 0
    run_similarity = 0.0

    processed_count = 0
    for filename, doc in gt.items():
        if limit is not None and processed_count >= limit:
            break

        expected = doc["expected"]

        actual_filename = find_actual_file(filename, data_dir)
        if not actual_filename:
            print(f"Skipping {filename} (not found in {data_dir})")
            continue

        filepath    = os.path.join(data_dir, actual_filename)
        file_format = get_file_format(actual_filename)
        processed_count += 1

        # Ensure format bucket exists in state and run metrics
        for metrics_dict in (state["format_metrics"], run_format_metrics):
            if file_format not in metrics_dict:
                metrics_dict[file_format] = {"total": 0, "correct": 0, "similarity": 0.0}

        print(f"\nProcessing: {actual_filename} (GT key: {filename})")

        try:
            _, validated = process_document(filepath, api_key, model)
        except Exception as e:
            print(f"  Extraction error: {e}")
            run_lines.append(f"File: {filename} (resolved as: {actual_filename})")
            run_lines.append(f"Status: Extraction error: {e}\n")
            run_lines.append("-" * 70 + "\n")
            continue

        if validated is None:
            print("  Extraction failed.")
            run_lines.append(f"File: {filename} (resolved as: {actual_filename})")
            run_lines.append("Status: Extraction failed\n")
            run_lines.append("-" * 70 + "\n")
            continue

        prediction = validated.model_dump()

        file_report = [
            f"File: {filename} (resolved as: {actual_filename})",
            "Fields:",
        ]

        for field, gt_value in expected.items():
            gt_str   = str(gt_value).strip()
            pred_str = str(prediction.get(field, "")).strip()

            # ----------------------------------------------------------------
            # Scoring rules:
            #   1. both empty  → SKIP  (excluded from all metrics entirely)
            #   2. GT empty,   LLM has value → score = 0
            #   3. GT has val, LLM empty     → score = 0
            #   4. both non-empty            → fuzz.ratio similarity
            # ----------------------------------------------------------------

            if gt_str == "" and pred_str == "":
                # Case 1: both empty — do not count this field at all
                file_report.append(
                    f"  - {field:30s} : actual=''                                            "
                    f"| predicted=''                                  | [SKIPPED - both empty]"
                )
                continue

            # Cases 2, 3, 4: fuzz.ratio returns 0 when one side is empty
            similarity = fuzz.ratio(gt_str.lower(), pred_str.lower())

            # ---- accumulate into persistent state -------------------------
            state["field_all_similarities"].setdefault(field, []).append(similarity)
            state["field_correct_counts"][field] = (
                state["field_correct_counts"].get(field, 0) + (1 if similarity == 100 else 0)
            )
            state["format_metrics"][file_format]["total"]      += 1
            state["format_metrics"][file_format]["similarity"] += similarity
            state["overall_total"]      += 1
            state["overall_similarity"] += similarity
            if similarity == 100:
                state["format_metrics"][file_format]["correct"] += 1
                state["overall_correct"] += 1

            # ---- accumulate into this-run metrics -------------------------
            run_field_similarities.setdefault(field, []).append(similarity)
            run_field_correct[field] = run_field_correct.get(field, 0) + (1 if similarity == 100 else 0)
            run_format_metrics[file_format]["total"]      += 1
            run_format_metrics[file_format]["similarity"] += similarity
            run_total      += 1
            run_similarity += similarity
            if similarity == 100:
                run_format_metrics[file_format]["correct"] += 1
                run_correct += 1

            # ---- format report line ---------------------------------------
            clean_gt   = gt_str.replace("\n", " ")
            clean_pred = pred_str.replace("\n", " ")
            actual_part    = f"actual='{clean_gt}'"
            predicted_part = f"predicted='{clean_pred}'"

            file_report.append(
                f"  - {field:30s} : {actual_part:45s} | {predicted_part:45s} | accuracy={similarity:6.2f}%"
            )

        file_report.append("")
        file_report.append("-" * 70 + "\n")
        run_lines.extend(file_report)

    # Append this run's lines to accumulated state
    state["per_file_report_lines"].extend(run_lines)
    save_state(state, state_path)

    # Rewrite the full report with updated combined totals
    build_report(state, output_path)

    # -----------------------------------------------------------------------
    # Console output  (this-run metrics only)
    # -----------------------------------------------------------------------
    print("\n==============================")
    print(f" THIS RUN: {gt_name}")
    print(" FIELD-WISE ACCURACY")
    print("==============================\n")

    for field in sorted(run_field_similarities):
        scores  = run_field_similarities[field]
        n       = len(scores)
        exact   = run_field_correct.get(field, 0) / n * 100
        avg_sim = sum(scores) / n
        print(f"{field:30s} exact={exact:6.2f}%  similarity={avg_sim:6.2f}%  (n={n})")

    print("\n==============================")
    print(" THIS RUN: DOCUMENT-TYPE-WISE ACCURACY")
    print("==============================\n")

    for fmt, result in run_format_metrics.items():
        if result["total"] == 0:
            continue
        exact      = result["correct"]    / result["total"] * 100
        similarity = result["similarity"] / result["total"]
        print(fmt.upper())
        print(f"  Fields Compared : {result['total']}")
        print(f"  Exact Accuracy  : {exact:.2f}%")
        print(f"  Avg Similarity  : {similarity:.2f}%")
        print()

    run_exact_acc = (run_correct / run_total * 100) if run_total else 0.0
    run_avg_sim   = (run_similarity / run_total)     if run_total else 0.0

    print("==============================")
    print(f" THIS RUN: OVERALL ({gt_name})")
    print("==============================\n")
    print(f"Exact Accuracy : {run_exact_acc:.2f}%")
    print(f"Avg Similarity : {run_avg_sim:.2f}%")
    print(f"Fields Compared: {run_total}")
    print("------------------------------")

    # Accumulated totals (across all runs ever)
    ot  = state["overall_total"]
    oc  = state["overall_correct"]
    os_ = state["overall_similarity"]
    print("\n==============================")
    print(" ACCUMULATED OVERALL (all runs)")
    print("==============================\n")
    print(f"Exact Accuracy : {(oc / ot * 100) if ot else 0:.2f}%")
    print(f"Avg Similarity : {(os_ / ot) if ot else 0:.2f}%")
    print(f"Fields Compared: {ot}")
    print("------------------------------")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Evaluate extraction performance against ground truth."
    )
    parser.add_argument(
        "--limit", "-l", type=int, default=None,
        help="Limit the number of files evaluated in this run."
    )
    parser.add_argument(
        "--file", "-f", type=str, default=None,
        help="Evaluate a specific file only (must be a key in the GT file)."
    )
    parser.add_argument(
        "--output", "-o", type=str, default=None,
        help="Path to save the evaluation report (default: evaluation_report.txt)."
    )
    parser.add_argument(
        "--gt", "-g", type=str, default=None,
        help="Path to a specific GT JSON file. Skips the interactive prompt."
    )
    parser.add_argument(
        "--model", "-m", type=str, default=None, choices=AVAILABLE_MODELS,
        help="Groq model to evaluate. Skips the interactive prompt."
    )
    parser.add_argument(
        "--reset", action="store_true",
        help="Clear the accumulated state for the selected model and start fresh before this run."
    )
    args = parser.parse_args()

    chosen_model = args.model or select_model()

    if args.reset:
        clear_state(os.path.join(performance_dir, f"evaluation_state_{sanitize_model_name(chosen_model)}.json"))

    evaluate(
        limit=args.limit,
        target_file=args.file,
        output_path=args.output,
        gt_path=args.gt,
        model=chosen_model,
    )
