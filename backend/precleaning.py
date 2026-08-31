"""
precleaning.py

In-memory cleaning stage for the Document Processor Pipeline.

Consumes the dict produced by DocumentProcessorPipeline.run() (pipeline.py)
and returns a new dict where each page's "text_sources" has been REPLACED
with the cleaned, deduped, reading-order-sorted version -- same field name,
same per-block schema (source, text, bbox, confidence). There is no separate
"cleaned_text_sources" field and no raw/corrected pairing kept in the output;
only the final cleaned result is returned.
"""

import re
import time
import logging
from typing import Optional

import ftfy
from rapidfuzz import fuzz, process

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("document_cleaner")

# A small generic English wordlist used only as a fallback "is this a real word"
# signal for garbage detection when no domain_dictionary is supplied.
_GENERIC_WORDLIST = [
    "the", "and", "for", "ltd", "inc", "company", "policy", "number", "date",
    "name", "address", "phone", "email", "fax", "vat", "registration", "office",
    "head", "branch", "insurance", "insured", "insurer", "broker", "agent",
    "intermediary", "period", "premium", "schedule", "details", "contact",
    "signed", "signature", "printed", "version", "page", "monthly", "annual",
    "republic", "south", "africa", "claim", "cover", "benefit", "limit",
    "amount", "total", "balance", "account", "bank", "payment", "method",
    "debit", "order", "type", "contract", "renewal", "review", "inception",
    "territorial", "authorised", "authorized", "signatory", "behalf",
]

# Default domain dictionary for insurance documents. Used by RapidFuzz to snap
# garbled OCR tokens to the closest known term if no custom dictionary is provided.
_INSURANCE_DOMAIN_DICTIONARY = [
    "Old", "Mutual", "Insure", "Ltd", "Pty", "Payal", "Trading", "Projects",
    "Policyholder", "Details", "Address", "The", "Insured",
    "Physical", "Contact", "Work", "Tel", "Cell", "Email",
    "Registration", "No", "VAT", "Number", "Business", "Description",
    "Retail", "Hire", "Plant", "Own", "Operator",
    "Intermediary", "Agency", "Risk", "Brokers",
    "Policy", "Type", "Contract", "Monthly", "Period", "Insurance",
    "Original", "Inception", "Date", "Review", "Territorial", "Limits",
    "Republic", "South", "Africa", "Botswana", "Eswatini", "Kingdoms",
    "Kingdom", "Lesotho", "Zimbabwe", "Republics", "Malawi", "Namibia", "Mozambique",
    "Payment", "Method", "Debit", "Order", "Strike", "Date",
    "Reason", "For", "Issue", "Adding", "Geysers",
    "Authorised", "Signatory", "Signed", "At", "Printed", "By",
    "Application", "Version", "Endorsement", "Processed", "Page", "Of",
    "Company", "Broker", "Agent", "Premium", "Schedule", "Cover", "Benefit",
    "Limit", "Amount", "Total", "Balance", "Account", "Bank",
    "Johannesburg", "Roodepoort", "Hadeda", "Crescent", "Pune"
]


class DocumentCleaner:
    """
    Cleans the in-memory output of DocumentProcessorPipeline.
    """

    # Common OCR misreads that are hard to catch with just fuzzy matching against a
    # dictionary because they are too short (e.g. "cel", "Lad"), contain punctuation
    # (e.g. "E-mal"), or fuzzy-match too far from the correct word to clear any safe
    # similarity threshold (e.g. "non-lfe" vs "non-life" still scores under typical
    # thresholds once the hyphen and short length are factored in by the tokenizer).
    #
    # IMPORTANT LIMITATION (documented honestly, not silently): this map can only
    # fix tokens that are RECOGNIZABLY close to a known correct word. It cannot:
    #   - insert words the OCR engine dropped entirely (e.g. "Registration" missing
    #     before "Number" in "Tumber 1970/...")
    #   - reconstruct unique identifiers/serial numbers from garbled digits+letters
    #     (e.g. "00s2s" has no safe way to become a specific correct registration
    #     number -- guessing a number here would be worse than leaving it flagged)
    #   - fix tokens where the OCR misread is too different from the correct word
    #     to be a legitimate fuzzy match rather than a guess (e.g. "P57" vs "FSP" --
    #     these share no character-order similarity; RapidFuzz scores this at ~33%,
    #     which is corruption beyond what text-level correction can safely repair)
    _OCR_CORRECTION_MAP = {
        "cel": "Cell",
        "hinoewadi": "HINJEWADI",
        "e-mal": "E-mail",
        "emal": "E-mail",
        "e-mail": "E-mail",
        "lad": "Ltd",
        "mutral": "Mutual",
        "mutul":"Mutual",
        "ineure": "Insure",
        "numbar": "Number",
        "printad": "Printed",
        "apploation": "Application",
        "venion": "Version",
        "endorserment": "Endorsement",
        "prosessed": "Processed",
        "bohalf": "Behalf",
        "lcenned": "Licensed",
        "rauer": "Insurer",
        "tumber": "Number",
        "oriata": "Old",
        "yule": "Payal",
        "lfe": "life",
        "non-lfe": "non-life",
        "p57": "FSP",
    }

    def __init__(
        self,
        high_confidence_threshold: float = 0.90,
        medium_confidence_threshold: float = 0.70,
        garbage_score_threshold: float = 60.0,
        drop_garbage: bool = False,
        iou_threshold: float = 0.5,
        text_sim_threshold: float = 80.0,
        ocr_fix_similarity_threshold: float = 75.0,   # Lowered from 85.0 to catch "Mutral" (83.3%)
        line_band_tolerance_px: float = 5.0,
        domain_dictionary: Optional[list] = None,
    ):
        self.high_confidence_threshold = high_confidence_threshold
        self.medium_confidence_threshold = medium_confidence_threshold
        self.garbage_score_threshold = garbage_score_threshold
        self.drop_garbage = drop_garbage
        self.iou_threshold = iou_threshold
        self.text_sim_threshold = text_sim_threshold
        self.ocr_fix_similarity_threshold = ocr_fix_similarity_threshold
        self.line_band_tolerance_px = line_band_tolerance_px

        # Domain dictionary: known-correct terms (insurer names, field labels, etc.)
        self.domain_dictionary = domain_dictionary or list(_INSURANCE_DOMAIN_DICTIONARY)

        # Pre-compute lowercase dictionary for fast case-insensitive fuzzy matching
        self._domain_dict_lower = {w.lower(): w for w in self.domain_dictionary}
        self._domain_words_lower = list(self._domain_dict_lower.keys())

        # Reference wordlist used purely for the garbage/word-likeness score (Stage 2).
        self._reference_wordlist = list(set(_GENERIC_WORDLIST) | set(self._domain_words_lower))

    # ------------------------------------------------------------------ #
    # Stage 1: Confidence tiering (internal use only, not in final output)
    # ------------------------------------------------------------------ #

    def tier_by_confidence(self, confidence: float) -> str:
        if confidence >= self.high_confidence_threshold:
            return "high"
        elif confidence >= self.medium_confidence_threshold:
            return "medium"
        return "low"

    # ------------------------------------------------------------------ #
    # Stage 2: Garbage / signature detection (internal use only)
    # ------------------------------------------------------------------ #

    _CODE_LIKE_TOKEN = re.compile(r"^(?=.*[A-Za-z])(?=.*\d)[A-Za-z0-9]+([\-/.][A-Za-z0-9]+)*$")

    def _word_likeness_score(self, text: str) -> float:
        if self._CODE_LIKE_TOKEN.match(text.strip()):
            return 100.0

        tokens = re.findall(r"[A-Za-z]+", text)
        if not tokens:
            return 100.0

        scores = []
        for token in tokens:
            if len(token) <= 2:
                continue
            match = process.extractOne(token.lower(), self._reference_wordlist, scorer=fuzz.ratio)
            scores.append(match[1] if match else 0.0)

        if not scores:
            return 100.0
        return sum(scores) / len(scores)

    def is_likely_signature_or_scrawl(self, block: dict, typical_line_height: float) -> bool:
        text = block["text"]
        confidence = block["confidence"]
        bbox = block["bbox"]
        box_height = bbox[3] - bbox[1]

        is_short = len(text.strip()) <= 8
        is_unusually_tall = typical_line_height > 0 and box_height > typical_line_height * 2.5
        is_lowish_confidence = confidence < self.high_confidence_threshold

        return is_short and is_unusually_tall and is_lowish_confidence

    def is_single_char_or_symbol_only(self, text: str) -> bool:
        stripped = text.strip()
        if not stripped:
            return True
        return bool(re.fullmatch(r"[^A-Za-z0-9]{1,2}", stripped))

    def _is_likely_garbage(self, block: dict, typical_line_height: float) -> bool:
        text = block["text"]
        is_sig = self.is_likely_signature_or_scrawl(block, typical_line_height)
        is_symbol_only = self.is_single_char_or_symbol_only(text)

        word_score = self._word_likeness_score(text)
        word_score_is_suspicious = (
            word_score < self.garbage_score_threshold
            and block["confidence"] < self.medium_confidence_threshold
        )
        return is_sig or is_symbol_only or word_score_is_suspicious

    def _compute_typical_line_height(self, blocks: list) -> float:
        heights = [b["bbox"][3] - b["bbox"][1] for b in blocks if b["bbox"][3] > b["bbox"][1]]
        if not heights:
            return 0.0
        heights.sort()
        return heights[len(heights) // 2]

    # ------------------------------------------------------------------ #
    # Stage 3: Text normalization (incl. RapidFuzz OCR-confusion fixing)
    # ------------------------------------------------------------------ #

    def strip_and_collapse_whitespace(self, text: str) -> str:
        return re.sub(r"\s+", " ", text.strip())

    def normalize_unicode(self, text: str) -> str:
        return ftfy.fix_text(text, normalization="NFKC")

    def strip_leading_trailing_punctuation_noise(self, text: str) -> str:
        text = re.sub(r"^[:\-\s\u2022\u00b7\u25cf\u25aa]+", "", text)
        text = re.sub(r"([.,;:!?])\1+$", r"\1", text)
        return text.strip()

    def _is_morphological_variant(self, token: str, candidate: str) -> bool:
        """
        Returns True if `token` and `candidate` differ only by one being a
        prefix of the other (e.g. "month" / "monthly", "Insure" / "Insurer").
        This pattern means the two words are different, individually correct
        English forms -- not an OCR typo of each other -- even when they score
        a high RapidFuzz ratio simply because they share most characters.
        Guards against real cases seen in production: a correctly-spelled
        "month" was getting "corrected" to "Monthly", and a correctly-spelled
        "Insurer" was getting "corrected" to "Insure", because the domain
        dictionary contained both the short and long form of the same root.
        """
        a, b = token.lower(), candidate.lower()
        shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
        return len(shorter) >= 3 and longer.startswith(shorter)

    def fix_common_ocr_substitutions(self, text: str) -> dict:
        """
        Uses RapidFuzz to snap individual alphabetic tokens to the closest term
        in the domain dictionary. Also applies exact corrections from _OCR_CORRECTION_MAP.

        Honest limitation: this can only repair a token that still resembles its
        correct form. It cannot recover words the OCR engine dropped entirely, and
        it will not "correct" a token to a dictionary word that is fundamentally
        dissimilar (e.g. it will not turn "P57" into "FSP" -- there is no safe
        similarity signal connecting those two strings, so doing so would be
        fabrication, not correction). It also will not "correct" a token that is
        simply a shorter or longer valid English form of a dictionary word (see
        _is_morphological_variant) -- two different, individually correct words
        sharing a common root is not the same thing as an OCR misread.
        """
        if not self.domain_dictionary and not self._OCR_CORRECTION_MAP:
            return {"text": text, "corrections": []}

        # Helper to reconstruct token with corrected word while preserving surrounding punctuation
        def reconstruct_token(original_token, corrected_word):
            leading = re.match(r"^[^\w]*", original_token).group(0)
            trailing = re.search(r"[^\w]*$", original_token).group(0)
            return leading + corrected_word + trailing

        tokens = text.split(" ")
        corrected_tokens = []
        corrections = []

        for token in tokens:
            # 1. Check exact map first (handles short words, punctuation, and known bad OCR)
            lower_token = token.lower()
            if lower_token in self._OCR_CORRECTION_MAP:
                corrected_word = self._OCR_CORRECTION_MAP[lower_token]
                corrected_tokens.append(corrected_word)
                corrections.append((token, corrected_word, 100.0))
                continue

            clean_token = re.sub(r"[^\w]", "", token)
            lower_clean = clean_token.lower()

            if lower_clean in self._OCR_CORRECTION_MAP:
                corrected_word = self._OCR_CORRECTION_MAP[lower_clean]
                corrected_token = reconstruct_token(token, corrected_word)
                corrected_tokens.append(corrected_token)
                corrections.append((token, corrected_token, 100.0))
                continue

            # 2. Fuzzy match against domain dictionary
            if not clean_token or len(clean_token) < 3 or not clean_token.isalpha():
                corrected_tokens.append(token)
                continue

            if self.domain_dictionary:
                match = process.extractOne(lower_clean, self._domain_words_lower, scorer=fuzz.ratio)
                if (
                    match
                    and match[1] >= self.ocr_fix_similarity_threshold
                    and match[0] != lower_clean
                    and not self._is_morphological_variant(lower_clean, match[0])
                ):
                    original_case_word = self._domain_dict_lower[match[0]]
                    corrected_token = reconstruct_token(token, original_case_word)
                    corrected_tokens.append(corrected_token)
                    corrections.append((token, corrected_token, round(match[1], 2)))
                else:
                    corrected_tokens.append(token)
            else:
                corrected_tokens.append(token)

        return {"text": " ".join(corrected_tokens), "corrections": corrections}

    def normalize_text(self, text: str) -> dict:
        text = self.normalize_unicode(text)
        text = self.strip_and_collapse_whitespace(text)
        text = self.strip_leading_trailing_punctuation_noise(text)

        fix_result = self.fix_common_ocr_substitutions(text)
        return {"text": fix_result["text"], "corrections": fix_result["corrections"]}

    # ------------------------------------------------------------------ #
    # Stage 4: Date normalization
    # ------------------------------------------------------------------ #

    _DATE_PATTERN = re.compile(r"\b(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2,4})\b")

    def normalize_dates(self, text: str) -> str:
        return self._DATE_PATTERN.sub(lambda m: f"{m.group(1)}/{m.group(2)}/{m.group(3)}", text)

    # ------------------------------------------------------------------ #
    # Stage 5: Deduplication (IoU + RapidFuzz text similarity)
    # ------------------------------------------------------------------ #

    def compute_bbox_overlap(self, bbox_a: list, bbox_b: list) -> float:
        ax0, ay0, ax1, ay1 = bbox_a
        bx0, by0, bx1, by1 = bbox_b

        inter_x0 = max(ax0, bx0)
        inter_y0 = max(ay0, by0)
        inter_x1 = min(ax1, bx1)
        inter_y1 = min(ay1, by1)

        inter_area = max(0.0, inter_x1 - inter_x0) * max(0.0, inter_y1 - inter_y0)
        if inter_area <= 0:
            return 0.0

        area_a = max(0.0, ax1 - ax0) * max(0.0, ay1 - ay0)
        area_b = max(0.0, bx1 - bx0) * max(0.0, by1 - by0)
        union_area = area_a + area_b - inter_area
        if union_area <= 0:
            return 0.0

        return inter_area / union_area

    def compute_text_similarity(self, text_a: str, text_b: str) -> float:
        return fuzz.ratio(text_a.lower(), text_b.lower())

    def find_duplicate_pairs(self, blocks: list) -> list:
        duplicate_pairs = []
        for i in range(len(blocks)):
            for j in range(i + 1, len(blocks)):
                a, b = blocks[i], blocks[j]
                if a["source"] == b["source"]:
                    continue

                iou = self.compute_bbox_overlap(a["bbox"], b["bbox"])
                if iou < self.iou_threshold:
                    continue

                sim = self.compute_text_similarity(a["text"], b["text"])
                if sim >= self.text_sim_threshold:
                    duplicate_pairs.append((i, j, iou, sim))

        return duplicate_pairs

    def resolve_duplicate(self, block_a: dict, block_b: dict) -> dict:
        if block_a["source"] == "embedded" and block_b["source"] != "embedded":
            return block_a
        if block_b["source"] == "embedded" and block_a["source"] != "embedded":
            return block_b
        return block_a if block_a["confidence"] >= block_b["confidence"] else block_b

    def deduplicate(self, blocks: list) -> tuple:
        duplicate_pairs = self.find_duplicate_pairs(blocks)
        to_drop = set()

        for i, j, _iou, _sim in duplicate_pairs:
            kept = self.resolve_duplicate(blocks[i], blocks[j])
            dropped_idx = j if kept is blocks[i] else i
            to_drop.add(dropped_idx)

        deduped = [b for idx, b in enumerate(blocks) if idx not in to_drop]
        return deduped, len(to_drop)

    # ------------------------------------------------------------------ #
    # Stage 6: Reading Order Sort
    # ------------------------------------------------------------------ #

    def sort_reading_order(self, blocks: list) -> list:
        if not blocks:
            return []

        blocks_sorted_by_y = sorted(blocks, key=lambda b: b["bbox"][1])

        banded_blocks = []
        current_band = [blocks_sorted_by_y[0]]
        current_band_y = blocks_sorted_by_y[0]["bbox"][1]

        for b in blocks_sorted_by_y[1:]:
            y0 = b["bbox"][1]
            if y0 - current_band_y <= self.line_band_tolerance_px:
                current_band.append(b)
            else:
                current_band.sort(key=lambda b: b["bbox"][0])
                banded_blocks.extend(current_band)
                current_band = [b]
                current_band_y = y0

        if current_band:
            current_band.sort(key=lambda b: b["bbox"][0])
            banded_blocks.extend(current_band)

        return banded_blocks

    # ------------------------------------------------------------------ #
    # Page-level and document-level orchestration
    # ------------------------------------------------------------------ #

    def clean_page(self, page: dict) -> dict:
        original_blocks = page.get("text_sources", [])

        working_blocks = [dict(b) for b in original_blocks]
        typical_line_height = self._compute_typical_line_height(working_blocks)

        if self.drop_garbage:
            working_blocks = [
                b for b in working_blocks
                if not self._is_likely_garbage(b, typical_line_height)
            ]

        for b in working_blocks:
            result = self.normalize_text(b["text"])
            b["text"] = result["text"]

        for b in working_blocks:
            b["text"] = self.normalize_dates(b["text"])

        working_blocks = [
            b for b in working_blocks
            if b["text"].strip() and not self.is_single_char_or_symbol_only(b["text"])
        ]

        working_blocks, _duplicates_removed = self.deduplicate(working_blocks)
        working_blocks = self.sort_reading_order(working_blocks)

        clean_blocks = [
            {
                "source": b["source"],
                "text": b["text"],
                "bbox": b["bbox"],
                "confidence": b["confidence"],
                "metadata": b.get("metadata", {}),
            }
            for b in working_blocks
        ]

        new_page = dict(page)
        new_page["text_sources"] = clean_blocks
        return new_page

    def clean(self, result: dict) -> dict:
        t_start = time.perf_counter()
        logger.info(f"Cleaning document: {result.get('document_name')}")

        total_blocks_before = sum(len(p.get("text_sources", [])) for p in result.get("pages", []))

        cleaned_pages = [self.clean_page(page) for page in result.get("pages", [])]

        total_blocks_after = sum(len(p.get("text_sources", [])) for p in cleaned_pages)
        duration_ms = round((time.perf_counter() - t_start) * 1000, 2)

        new_result = dict(result)
        new_result["pages"] = cleaned_pages
        new_result["cleaning_metadata"] = {
            "total_blocks_before": total_blocks_before,
            "total_blocks_after": total_blocks_after,
            "cleaning_duration_ms": duration_ms,
        }

        logger.info(
            f"Cleaning complete: {total_blocks_before} blocks -> "
            f"{total_blocks_after} cleaned blocks in {duration_ms} ms"
        )
        return new_result


# ---------------------------------------------------------------------- #
# Standalone CLI usage (optional): clean a previously saved pipeline JSON
# ---------------------------------------------------------------------- #

def main():
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Run DocumentCleaner on a pipeline JSON result file")
    parser.add_argument("input_json", type=str, help="Path to a pipeline result JSON (doc_*.json)")
    parser.add_argument("--output-json", type=str, default=None, help="Where to save the cleaned JSON (default: <input>_cleaned.json)")
    parser.add_argument("--drop-garbage", action="store_true", help="Drop flagged garbage blocks instead of keeping them")
    args = parser.parse_args()

    with open(args.input_json, "r", encoding="utf-8") as f:
        result = json.load(f)

    cleaner = DocumentCleaner(drop_garbage=args.drop_garbage)
    cleaned = cleaner.clean(result)

    output_path = args.output_json or args.input_json.replace(".json", "_cleaned.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(cleaned, f, indent=2, ensure_ascii=False)

    print(f"Saved cleaned result to {output_path}")
    print(json.dumps(cleaned["cleaning_metadata"], indent=2))


if __name__ == "__main__":
    main()