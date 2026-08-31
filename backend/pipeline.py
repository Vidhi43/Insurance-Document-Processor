import os
import json
import time
import uuid
import logging
import argparse
import copy
import re
from PIL import Image
import numpy as np
import fitz  # PyMuPDF
from google.cloud import vision
from io import BytesIO

# Setup logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("document_pipeline")


def getattr_or_get(obj, key, default=None):
    """Safely retrieves a key from a dict or attribute from an object."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def pair_same_row_label_values(sources, y_tolerance=3.0):
    """
    merge_multiline_ocr only merges vertically-stacked wrapped lines (close y,
    close x). A bare-colon label and its value(s) can sit on the SAME y-band
    but far apart in x (label column vs. value column) -- that pairing never
    triggers in merge_multiline_ocr's x-proximity check. Attaches ALL same-row
    items to the right of the label (not just the nearest one), since a row can
    have multiple value tokens (e.g. "Cell" + "9782793478").
    """
    result = copy.deepcopy(sources)
    used_as_value = set()

    for i, item in enumerate(result):
        text = item["text"].rstrip()
        if not text.endswith(":"):
            continue

        item_y_center = (item["bbox"][1] + item["bbox"][3]) / 2
        same_row_to_the_right = []

        for j, candidate in enumerate(result):
            if j == i or j in used_as_value:
                continue
            cand_y_center = (candidate["bbox"][1] + candidate["bbox"][3]) / 2
            if abs(cand_y_center - item_y_center) > y_tolerance:
                continue
            if candidate["bbox"][0] < item["bbox"][2]:
                continue
            same_row_to_the_right.append((j, candidate))

        same_row_to_the_right.sort(key=lambda pair: pair[1]["bbox"][0])

        if same_row_to_the_right:
            values_text = " ".join(c["text"] for _, c in same_row_to_the_right)
            result[i]["text"] = f"{text} {values_text}".strip()
            for j, _ in same_row_to_the_right:
                used_as_value.add(j)

    return [item for idx, item in enumerate(result) if idx not in used_as_value]


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


class DocumentProcessorPipeline:
    def __init__(self, use_gpu=False):
        self.use_gpu = use_gpu
        self._whisper = None
        
        load_env_file()
        api_key = os.environ.get("GOOGLE_VISION_API_KEY")
        if api_key:
            from google.api_core.client_options import ClientOptions
            options = ClientOptions(api_key=api_key)
            self.vision_client = vision.ImageAnnotatorClient(client_options=options)
        else:
            self.vision_client = vision.ImageAnnotatorClient()

    def _merge_horizontal_words(self, words_data):
        """
        Helper method to group words on the same horizontal line and merge them
        if the gap between them is small. This prevents the document text from
        being split into individual words on separate lines.
        """
        if not words_data:
            return []

        # Convert word objects to include bounding boxes
        words_with_bboxes = []
        for w in words_data:
            vertices = w["vertices"]
            if not vertices or len(vertices) < 2:
                continue
            xs = [v[0] for v in vertices]
            ys = [v[1] for v in vertices]
            x0, y0, x1, y1 = float(min(xs)), float(min(ys)), float(max(xs)), float(max(ys))
            words_with_bboxes.append({
                "text": w["text"],
                "vertices": vertices,
                "confidence": w["confidence"],
                "bbox": [x0, y0, x1, y1]
            })

        if not words_with_bboxes:
            return []

        # Group words by their vertical alignment (lines)
        # We sort by y_center = (y0 + y1) / 2
        words_with_bboxes.sort(key=lambda w: (w["bbox"][1] + w["bbox"][3]) / 2)

        lines = []
        for w in words_with_bboxes:
            w_y_center = (w["bbox"][1] + w["bbox"][3]) / 2
            placed = False
            
            for line in lines:
                # Calculate average y_center and height of the line
                line_y_centers = [(item["bbox"][1] + item["bbox"][3]) / 2 for item in line]
                avg_line_y = sum(line_y_centers) / len(line)
                
                line_heights = [item["bbox"][3] - item["bbox"][1] for item in line]
                avg_line_h = sum(line_heights) / len(line)
                
                # If the word's y_center is within 45% of the average line height, group them
                tolerance = max(6.0, avg_line_h * 0.45)
                if abs(w_y_center - avg_line_y) <= tolerance:
                    line.append(w)
                    placed = True
                    break
                    
            if not placed:
                lines.append([w])

        # Merge adjacent words on each line horizontally
        merged_results = []
        for line in lines:
            line.sort(key=lambda w: w["bbox"][0])
            
            current_block = None
            for w in line:
                if current_block is None:
                    current_block = {
                        "text": w["text"],
                        "bbox": list(w["bbox"]),
                        "confidence_sum": w["confidence"],
                        "count": 1
                    }
                else:
                    prev_x1 = current_block["bbox"][2]
                    curr_x0 = w["bbox"][0]
                    gap = curr_x0 - prev_x1
                    
                    curr_h = w["bbox"][3] - w["bbox"][1]
                    prev_h = current_block["bbox"][3] - current_block["bbox"][1]
                    avg_h = (curr_h + prev_h) / 2.0
                    
                    # Merge if the gap is <= 2.5 times the average height
                    if gap <= avg_h * 2.5:
                        current_block["text"] += " " + w["text"]
                        current_block["bbox"][0] = min(current_block["bbox"][0], w["bbox"][0])
                        current_block["bbox"][1] = min(current_block["bbox"][1], w["bbox"][1])
                        current_block["bbox"][2] = max(current_block["bbox"][2], w["bbox"][2])
                        current_block["bbox"][3] = max(current_block["bbox"][3], w["bbox"][3])
                        current_block["confidence_sum"] += w["confidence"]
                        current_block["count"] += 1
                    else:
                        merged_results.append(current_block)
                        current_block = {
                            "text": w["text"],
                            "bbox": list(w["bbox"]),
                            "confidence_sum": w["confidence"],
                            "count": 1
                        }
            if current_block:
                merged_results.append(current_block)

        # Convert back to list of dicts: {"text": str, "vertices": list, "confidence": float}
        final_words = []
        for mr in merged_results:
            x0, y0, x1, y1 = mr["bbox"]
            vertices = [
                (int(x0), int(y0)),
                (int(x1), int(y0)),
                (int(x1), int(y1)),
                (int(x0), int(y1))
            ]
            final_words.append({
                "text": mr["text"],
                "vertices": vertices,
                "confidence": mr["confidence_sum"] / mr["count"]
            })
            
        # Re-sort final words by y_center then x0
        final_words.sort(key=lambda w: (
            (w["vertices"][0][1] + w["vertices"][2][1]) / 2, 
            w["vertices"][0][0]
        ))
        
        return final_words

    def _vision_ocr(self, image_bytes):
        """
        Calls Google Cloud Vision API to perform document text detection.
        Returns a list of dicts: {"text": str, "vertices": list of (x, y), "confidence": float}
        """
        image = vision.Image(content=image_bytes)
        response = self.vision_client.document_text_detection(image=image)
        
        if response.error.message:
            raise RuntimeError(f"Google Cloud Vision API Error: {response.error.message}")
            
        words_data = []
        annotation = response.full_text_annotation
        if not annotation:
            return words_data
            
        for page in annotation.pages:
            for block in page.blocks:
                for paragraph in block.paragraphs:
                    for word in paragraph.words:
                        word_text = "".join(symbol.text for symbol in word.symbols)
                        
                        vertices = []
                        if word.bounding_box and word.bounding_box.vertices:
                            for vertex in word.bounding_box.vertices:
                                x = vertex.x if vertex.x is not None else 0
                                y = vertex.y if vertex.y is not None else 0
                                vertices.append((x, y))
                                
                        confidence = getattr(word, "confidence", 1.0)
                        if confidence is None:
                            confidence = 1.0
                            
                        words_data.append({
                            "text": word_text,
                            "vertices": vertices,
                            "confidence": confidence
                        })
        return self._merge_horizontal_words(words_data)

    @property
    def whisper(self):
        """Lazy initialization of Faster-Whisper."""
        if self._whisper is None:
            logger.info("Initializing Faster-Whisper model...")
            from faster_whisper import WhisperModel
            import torch
            device = "cuda" if (self.use_gpu and torch.cuda.is_available()) else "cpu"
            compute_type = "float16" if device == "cuda" else "int8"
            self._whisper = WhisperModel("small", device=device, compute_type=compute_type)
            logger.info(f"Faster-Whisper model initialized on {device} ({compute_type}).")
        return self._whisper

    def merge_multiline_ocr(self, ocr_sources):
        """
        Post-processing step to merge multiline text and sort by reading order.
        Processes BOTH embedded and OCR text to ensure correct page flow.
        """
        if not ocr_sources:
            return []

        sorted_ocr = sorted(ocr_sources, key=lambda item: (item["bbox"][1], item["bbox"][0]))

        letter_x0s = [item["bbox"][0] for item in ocr_sources if any(c.isalpha() for c in item["text"])]
        min_x0 = min(letter_x0s) if letter_x0s else 0.0
        label_threshold = min_x0 + 50.0
        groups = []

        for item in sorted_ocr:
            matched_group_idx = -1
            min_y_gap = 9999.0

            for idx, group in enumerate(groups):
                prev_item = group[-1]

                y_gap = item["bbox"][1] - prev_item["bbox"][3]
                if not (-5 <= y_gap < 25):
                    continue

                prev_text = prev_item["text"]
                prev_x0 = prev_item["bbox"][0]
                prev_x1 = prev_item["bbox"][2]

                prefix_match = re.match(r"^\s*(?::?\s*[A-Za-z\s\-]+:\s*)", prev_text)
                if prefix_match:
                    prefix_len = len(prefix_match.group(0))
                    full_len = len(prev_text)
                    prev_content_x0 = prev_x0 + (prev_x1 - prev_x0) * (prefix_len / full_len)
                else:
                    prev_content_x0 = prev_x0

                x_diff = abs(item["bbox"][0] - prev_content_x0)
                x_diff_orig = abs(item["bbox"][0] - prev_x0)

                if x_diff >= 20 and x_diff_orig >= 20:
                    continue

                if prev_x0 < label_threshold or item["bbox"][0] < label_threshold:
                    continue

                if item["text"].lstrip().startswith(":"):
                    continue

                def get_letters(t):
                    return "".join([c for c in t if c.isalpha()])

                prev_content_text = prev_text[len(prefix_match.group(0)):] if prefix_match else prev_text
                prev_letters = get_letters(prev_content_text)
                item_letters = get_letters(item["text"])

                if len(prev_letters) >= 3 and len(item_letters) >= 3:
                    if prev_letters.isupper() != item_letters.isupper():
                        continue

                has_intervening_label = False
                for L in ocr_sources:
                    if L["bbox"][0] < label_threshold:
                        if prev_item["bbox"][1] < L["bbox"][1] <= item["bbox"][1]:
                            if abs(L["bbox"][1] - item["bbox"][1]) >= 8:
                                if L is not item and L is not prev_item:
                                    has_intervening_label = True
                                    break
                if has_intervening_label:
                    continue

                if y_gap < min_y_gap:
                    min_y_gap = y_gap
                    matched_group_idx = idx

            if matched_group_idx != -1:
                groups[matched_group_idx].append(item)
            else:
                groups.append([item])

        return [self._merge_group(g) for g in groups]

    def _merge_group(self, group):
        if len(group) == 1:
            return group[0]

        first_item = group[0]
        merged_text = " ".join(item["text"] for item in group)
        avg_confidence = sum(item["confidence"] for item in group) / len(group)

        # Preserve source type dynamically instead of hardcoding "ocr"
        sources = set(item["source"] for item in group)
        source_type = sources.pop() if len(sources) == 1 else "mixed"

        return {
            "source": source_type,
            "text": merged_text,
            "bbox": first_item["bbox"],
            "confidence": avg_confidence,
            "metadata": first_item.get("metadata", {"bbox": first_item["bbox"]})
        }

    def classify_document(self, file_path):
        """
        Flowchart Step B: Document Classifier.
        Classifies the document based on extension.
        """
        _, ext = os.path.splitext(file_path)
        ext = ext.lower()
        if ext == ".pdf":
            return "pdf"
        elif ext in [".png", ".jpg", ".jpeg", ".tiff", ".bmp", ".webp"]:
            return "image"
        elif ext in [".docx", ".doc"]:
            return "docx" if ext == ".docx" else "doc"
        elif ext in [".mp3", ".wav", ".m4a", ".aac", ".flac"]:
            return "audio"
        elif ext in [".mp4", ".avi", ".mov", ".mkv", ".webm"]:
            return "video"
        else:
            raise ValueError(f"Unsupported document format: {ext}")

    def process_image(self, file_path):
        """
        Flowchart Steps C & D for Image Branch:
        Run Google Cloud Vision OCR on the whole image and return raw text + bounding boxes.
        """
        logger.info(f"Entering Image Branch for: {file_path}")
        img = Image.open(file_path).convert("RGB")
        page_width, page_height = img.size

        with open(file_path, "rb") as f:
            image_bytes = f.read()

        t0 = time.perf_counter()
        words_data = self._vision_ocr(image_bytes)
        duration_ms = round((time.perf_counter() - t0) * 1000, 2)

        text_sources = []
        for word in words_data:
            vertices = word["vertices"]
            if not vertices or len(vertices) < 2:
                continue
            xs = [v[0] for v in vertices]
            ys = [v[1] for v in vertices]
            x0 = float(min(xs))
            y0 = float(min(ys))
            x1 = float(max(xs))
            y1 = float(max(ys))
            bbox = [x0, y0, x1, y1]

            text_sources.append({
                "source": "ocr",
                "text": word["text"].strip(),
                "bbox": bbox,
                "confidence": float(word["confidence"]),
                "metadata": {"bbox": bbox}
            })

        merged_text_sources = self.merge_multiline_ocr(text_sources)

        return [{
            "page_number": 1,
            "width": float(page_width),
            "height": float(page_height),
            "text_sources": merged_text_sources,
            "ocr_duration_ms": duration_ms
        }]

    def process_pdf(self, file_path, force_ocr=False):
        """
        Flowchart Steps E to L for PDF Branch.
        OCR is always run on ALL image regions regardless of embedded text --
        deduplication in precleaning.py handles any overlapping content.
        force_ocr is kept for backward compatibility but no longer changes behavior.
        """
        logger.info(f"Entering PDF Branch for: {file_path}")
        doc = fitz.open(file_path)
        pages_data = []

        for page_num in range(len(doc)):
            page = doc[page_num]
            page_idx = page_num + 1
            logger.info(f"Processing PDF Page {page_idx}/{len(doc)}")

            # Step G: Extract Embedded Text
            embedded_sources = []
            blocks = page.get_text("blocks")
            for b in blocks:
                if len(b) >= 7 and b[6] == 0:
                    text = b[4].strip()
                elif len(b) >= 5:
                    text = b[4].strip()
                else:
                    continue
                if text:
                    bbox = [float(b[0]), float(b[1]), float(b[2]), float(b[3])]
                    embedded_sources.append({
                        "source": "embedded",
                        "text": text,
                        "bbox": bbox,
                        "confidence": 1.0,
                        "metadata": {"bbox": bbox}
                    })

            # Step H: Detect Image Regions -- only OCR if embedded text < 50 or force_ocr
            image_info_list = page.get_image_info()
            ocr_sources = []

            embedded_chars = sum(len(item["text"]) for item in embedded_sources)

            if embedded_chars < 50 or force_ocr:
                if image_info_list:
                    logger.info(f"Page {page_idx}: Detected {len(image_info_list)} image region(s). Running OCR (embedded_chars={embedded_chars} < 50 or force_ocr={force_ocr})...")
                    for idx, img_info in enumerate(image_info_list):
                        bbox = img_info.get("bbox")
                        if not bbox:
                            continue
                        rect = fitz.Rect(bbox)
                        if rect.is_empty:
                            continue

                        # Render cropped region at 150 DPI and get raw PNG bytes
                        dpi = 150
                        pix = page.get_pixmap(clip=rect, dpi=dpi)
                        img_data = pix.tobytes("png")

                        try:
                            t_ocr_start = time.perf_counter()
                            words_data = self._vision_ocr(img_data)
                            reg_ocr_ms = round((time.perf_counter() - t_ocr_start) * 1000, 2)
                            logger.info(f"  -> OCR on region {idx+1}/{len(image_info_list)} took {reg_ocr_ms} ms")

                            # Map crop pixels (150 DPI) to PDF points (72 DPI)
                            # scale factor is points per pixel: 72.0 / 150.0
                            scale_factor = 72.0 / 150.0
                            rx0, ry0 = bbox[0], bbox[1]

                            for word in words_data:
                                vertices = word["vertices"]
                                if not vertices or len(vertices) < 2:
                                    continue
                                xs = [v[0] for v in vertices]
                                ys = [v[1] for v in vertices]
                                ox0 = float(min(xs))
                                oy0 = float(min(ys))
                                ox1 = float(max(xs))
                                oy1 = float(max(ys))

                                # Apply scaling and add crop origin coordinates to get page coordinates
                                page_x0 = rx0 + ox0 * scale_factor
                                page_y0 = ry0 + oy0 * scale_factor
                                page_x1 = rx0 + ox1 * scale_factor
                                page_y1 = ry0 + oy1 * scale_factor
                                page_bbox = [page_x0, page_y0, page_x1, page_y1]

                                ocr_sources.append({
                                    "source": "ocr",
                                    "text": word["text"].strip(),
                                    "bbox": page_bbox,
                                    "confidence": float(word["confidence"]),
                                    "metadata": {"bbox": page_bbox}
                                })
                        except Exception as ocr_err:
                            logger.error(f"Error performing OCR on page {page_idx} image region {idx}: {ocr_err}")
                else:
                    logger.info(f"Page {page_idx}: No image regions detected.")
            else:
                logger.info(f"Page {page_idx}: Embedded text chars ({embedded_chars}) >= 50 and force_ocr=False. Skipping image region OCR.")

            # Step L: Pair same-row labels with values, then merge into reading order
            all_sources = pair_same_row_label_values(embedded_sources + ocr_sources)
            merged_sources = self.merge_multiline_ocr(all_sources) if all_sources else []

            pages_data.append({
                "page_number": page_idx,
                "width": float(page.rect.width),
                "height": float(page.rect.height),
                "text_sources": merged_sources
            })

        doc.close()
        return pages_data

    def convert_doc_to_docx(self, file_path):
        """
        Converts a .doc file to a temporary .docx file using win32com on Windows.
        """
        abs_doc_path = os.path.abspath(file_path)
        abs_docx_path = os.path.splitext(abs_doc_path)[0] + f"_temp_{int(time.time())}.docx"

        try:
            import win32com.client as win32
        except ImportError:
            raise ImportError("pywin32 is required to convert .doc files on Windows. Please install it using 'pip install pywin32'.")

        try:
            try:
                word = win32.gencache.EnsureDispatch('Word.Application')
            except Exception:
                word = win32.Dispatch('Word.Application')
        except Exception as e:
            raise RuntimeError("Microsoft Word is not installed or registered on this system. Cannot convert .doc files.") from e

        word.Visible = False
        doc = None
        try:
            doc = word.Documents.Open(abs_doc_path)
            doc.SaveAs2(abs_docx_path, FileFormat=16)
            logger.info(f"Successfully converted {file_path} to {abs_docx_path}")
            return abs_docx_path
        except Exception as e:
            logger.error(f"Failed to convert .doc to .docx: {e}")
            raise e
        finally:
            if doc:
                try:
                    doc.Close(SaveChanges=False)
                except Exception:
                    pass
            try:
                word.Quit()
            except Exception:
                pass

    def process_word(self, file_path, doc_type, force_ocr=False):
        """
        Processes Word documents (.docx and .doc).
        Extracts text from paragraphs and tables, and runs OCR on all embedded images.
        If .doc, converts to .docx first.
        """
        logger.info(f"Entering Word Branch for: {file_path} (type={doc_type})")
        target_path = file_path
        temp_docx = None

        if doc_type == "doc":
            logger.info("Converting .doc to .docx...")
            target_path = self.convert_doc_to_docx(file_path)
            temp_docx = target_path

        try:
            from docx import Document as docx_factory
            from docx.document import Document as DocxDocument
            from docx.oxml.text.paragraph import CT_P
            from docx.oxml.table import CT_Tbl
            from docx.table import Table, _Cell
            from docx.text.paragraph import Paragraph

            doc = docx_factory(target_path)
            text_sources = []
            processed_cells = set()
            processed_image_blobs = set()

            def extract_images_from_part(part):
                """OCR every unique image embedded in a document part."""
                if not part:
                    return
                try:
                    rels = part.rels
                except AttributeError:
                    return

                for rel in rels.values():
                    if "image" not in rel.reltype:
                        continue
                    try:
                        image_blob = rel.target_part.blob
                        blob_hash = hash(image_blob)
                        if blob_hash in processed_image_blobs:
                            continue
                        processed_image_blobs.add(blob_hash)

                        logger.info(f"Running Google Vision OCR on Word image region...")
                        t_ocr_start = time.perf_counter()
                        words_data = self._vision_ocr(image_blob)
                        logger.info(f"Word image OCR took {round((time.perf_counter() - t_ocr_start)*1000, 2)} ms")

                        for word in words_data:
                            text_sources.append({
                                "source": "ocr",
                                "text": word["text"].strip(),
                                "bbox": [0.0, 0.0, 0.0, 0.0],
                                "confidence": float(word["confidence"])
                            })
                    except Exception as e:
                        logger.warning(f"Failed to process image in Word document: {e}")

            def get_element(obj):
                if isinstance(obj, DocxDocument):
                    return obj.element.body
                elif isinstance(obj, _Cell):
                    return obj._tc
                return getattr(obj, "_element", getattr(obj, "element", None))

            def iter_block_items(parent_element, parent_obj):
                skipped_elements = set()
                for child in parent_element.iter():
                    if child in skipped_elements:
                        continue
                    is_fallback = False
                    ancestor = child.getparent()
                    while ancestor is not None and ancestor != parent_element:
                        if ancestor.tag.endswith("Fallback"):
                            is_fallback = True
                            break
                        ancestor = ancestor.getparent()
                    if is_fallback:
                        continue
                    if child.tag.endswith("tbl"):
                        yield Table(child, parent_obj)
                        for desc in child.iter():
                            skipped_elements.add(desc)
                    elif child.tag.endswith("p"):
                        yield Paragraph(child, parent_obj)

            processed_header_footers = set()

            def extract_hdr_ftr_text(hdr_ftr):
                if not hdr_ftr:
                    return
                elm = get_element(hdr_ftr)
                if elm is None or elm in processed_header_footers:
                    return
                processed_header_footers.add(elm)

                for block in iter_block_items(elm, hdr_ftr):
                    if isinstance(block, Paragraph):
                        text = block.text.strip() if block.text is not None else ""
                        if text:
                            text_sources.append({
                                "source": "embedded",
                                "text": text,
                                "bbox": [0.0, 0.0, 0.0, 0.0],
                                "confidence": 1.0,
                                "metadata": {"bbox": [0.0, 0.0, 0.0, 0.0]}
                            })
                    elif isinstance(block, Table):
                        for row in block.rows:
                            for cell in row.cells:
                                if cell._tc not in processed_cells:
                                    processed_cells.add(cell._tc)
                                    for cell_block in iter_block_items(cell._tc, cell):
                                        if isinstance(cell_block, Paragraph):
                                            cell_text = cell_block.text.strip() if cell_block.text is not None else ""
                                            if cell_text:
                                                text_sources.append({
                                                    "source": "embedded",
                                                    "text": cell_text,
                                                    "bbox": [0.0, 0.0, 0.0, 0.0],
                                                    "confidence": 1.0,
                                                    "metadata": {"bbox": [0.0, 0.0, 0.0, 0.0]}
                                                })

            # 1. Headers (text only first)
            for section in doc.sections:
                if section.different_first_page_header_footer:
                    extract_hdr_ftr_text(section.first_page_header)
                extract_hdr_ftr_text(section.even_page_header)
                extract_hdr_ftr_text(section.header)

            # 2. Body (text only first)
            for block in iter_block_items(get_element(doc), doc):
                if isinstance(block, Paragraph):
                    text = block.text.strip() if block.text is not None else ""
                    if text:
                        text_sources.append({
                            "source": "embedded",
                            "text": text,
                            "bbox": [0.0, 0.0, 0.0, 0.0],
                            "confidence": 1.0,
                            "metadata": {"bbox": [0.0, 0.0, 0.0, 0.0]}
                        })
                elif isinstance(block, Table):
                    for row in block.rows:
                        for cell in row.cells:
                            if cell._tc not in processed_cells:
                                processed_cells.add(cell._tc)
                                for cell_block in iter_block_items(cell._tc, cell):
                                    if isinstance(cell_block, Paragraph):
                                        cell_text = cell_block.text.strip() if cell_block.text is not None else ""
                                        if cell_text:
                                            text_sources.append({
                                                "source": "embedded",
                                                "text": cell_text,
                                                "bbox": [0.0, 0.0, 0.0, 0.0],
                                                "confidence": 1.0,
                                                "metadata": {"bbox": [0.0, 0.0, 0.0, 0.0]}
                                            })

            # 3. Footers (text only first)
            for section in doc.sections:
                if section.different_first_page_header_footer:
                    extract_hdr_ftr_text(section.first_page_footer)
                extract_hdr_ftr_text(section.even_page_footer)
                extract_hdr_ftr_text(section.footer)

            # Check character count threshold before running OCR on embedded images
            embedded_chars = sum(len(item["text"]) for item in text_sources)
            if embedded_chars < 50 or force_ocr:
                logger.info(f"Word document: Embedded chars ({embedded_chars}) < 50 or force_ocr={force_ocr}. Running OCR on images.")
                # Run OCR on all images in headers, body, footers
                for section in doc.sections:
                    if section.different_first_page_header_footer:
                        extract_images_from_part(getattr(section.first_page_header, 'part', None))
                    extract_images_from_part(getattr(section.even_page_header, 'part', None))
                    extract_images_from_part(getattr(section.header, 'part', None))

                extract_images_from_part(doc.part)

                for section in doc.sections:
                    if section.different_first_page_header_footer:
                        extract_images_from_part(getattr(section.first_page_footer, 'part', None))
                    extract_images_from_part(getattr(section.even_page_footer, 'part', None))
                    extract_images_from_part(getattr(section.footer, 'part', None))
            else:
                logger.info(f"Word document: Embedded chars ({embedded_chars}) >= 50 and force_ocr=False. Skipping image OCR.")

            return [{
                "page_number": 1,
                "width": 612.0,
                "height": 792.0,
                "text_sources": text_sources
            }]

        finally:
            if temp_docx and os.path.exists(temp_docx):
                try:
                    os.remove(temp_docx)
                    logger.info(f"Cleaned up temporary .docx: {temp_docx}")
                except Exception as clean_err:
                    logger.warning(f"Failed to clean up {temp_docx}: {clean_err}")

    def process_audio(self, file_path):
        """
        Processes audio files using Faster-Whisper.
        Decodes audio to 16kHz mono, transcribes it, and formats the output
        using the universal schema with metadata containing start and end times.
        """
        logger.info(f"Entering Audio Branch for: {file_path}")

        t0 = time.perf_counter()

        from faster_whisper.audio import decode_audio

        # Decode and preprocess audio to 16kHz mono
        try:
            audio_data = decode_audio(file_path, sampling_rate=16000)
        except Exception as e:
            logger.error(f"Failed to decode audio using faster_whisper.audio.decode_audio: {e}")
            raise e

        # Transcribe using WhisperModel
        segments, info = self.whisper.transcribe(audio_data)

        text_sources = []
        for segment in segments:
            text = segment.text.strip()
            if not text:
                continue

            start_time = float(segment.start)
            end_time = float(segment.end)

            try:
                confidence = float(np.exp(segment.avg_logprob))
                confidence = max(0.0, min(1.0, confidence))
            except Exception:
                confidence = 1.0

            text_sources.append({
                "source": "audio",
                "text": text,
                "bbox": [start_time, 0.0, end_time, 0.0],
                "confidence": confidence,
                "metadata": {
                    "start_time": start_time,
                    "end_time": end_time
                }
            })

        duration_ms = round((time.perf_counter() - t0) * 1000, 2)

        return [{
            "page_number": 1,
            "width": 0.0,
            "height": 0.0,
            "text_sources": text_sources,
            "transcription_duration_ms": duration_ms
        }]

    def process_video(self, file_path, extraction_mode="both"):
        """
        Processes video files by extracting frames and/or audio and merging them.
        """
        from video_processor import process_video
        return process_video(self, file_path, extraction_mode=extraction_mode)

    def run(self, file_path, force_ocr=False, extraction_mode="both"):
        """
        Runs the full pipeline on a file and returns the structured dictionary (in-memory JSON).
        """
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"File not found: {file_path}")

        t_start = time.perf_counter()
        doc_type = self.classify_document(file_path)

        if doc_type == "pdf":
            pages = self.process_pdf(file_path, force_ocr=force_ocr)
        elif doc_type == "image":
            pages = self.process_image(file_path)
        elif doc_type in ["docx", "doc"]:
            pages = self.process_word(file_path, doc_type, force_ocr=force_ocr)
        elif doc_type == "audio":
            pages = self.process_audio(file_path)
        elif doc_type == "video":
            pages = self.process_video(file_path, extraction_mode=extraction_mode)

        duration_ms = round((time.perf_counter() - t_start) * 1000, 2)
        doc_id = str(uuid.uuid4())

        result = {
            "document_id": doc_id,
            "document_name": os.path.basename(file_path),
            "document_type": doc_type,
            "total_pages": len(pages),
            "processing_time_ms": duration_ms,
            "pages": pages
        }
        return result


def main():
    parser = argparse.ArgumentParser(description="Run Document Processor Pipeline")
    parser.add_argument("file_path", nargs="?", type=str, help="Path to a PDF, Image, Word, or Audio file to process")
    parser.add_argument("--output-dir", type=str, default="output", help="Directory to save JSON results")
    parser.add_argument("--use-gpu", action="store_true", help="Enable GPU for models")
    parser.add_argument("--force-ocr", action="store_true", help="Force OCR on PDF image regions even if embedded text is found")
    parser.add_argument("--extraction-mode", type=str, default="both", choices=["frames", "audio", "both"], help="Video extraction mode: frames, audio, or both")
    args = parser.parse_args()

    os.makedirs(args.output_dir, exist_ok=True)

    input_path = args.file_path
    if not input_path:
        while True:
            input_path = input("Enter path to Video, Image, PDF, Word, or Audio file for processing: ").strip()
            if input_path.startswith('"') and input_path.endswith('"'):
                input_path = input_path[1:-1]
            elif input_path.startswith("'") and input_path.endswith("'"):
                input_path = input_path[1:-1]
            if os.path.exists(input_path):
                break
            print(f"[!] Error: File '{input_path}' does not exist. Please try again.")

    pipeline = DocumentProcessorPipeline(use_gpu=args.use_gpu)

    logger.info(f"Running pipeline on: {input_path} (force_ocr={args.force_ocr}, extraction_mode={args.extraction_mode})")
    try:
        result = pipeline.run(input_path, force_ocr=args.force_ocr, extraction_mode=args.extraction_mode)

        has_embedded = any(
            item["source"] == "embedded"
            for page in result["pages"]
            for item in page["text_sources"]
        )
        has_ocr = any(
            item["source"] == "ocr"
            for page in result["pages"]
            for item in page["text_sources"]
        )

        base_name = os.path.splitext(result['document_name'])[0]

        if result["document_type"] == "pdf" and has_embedded and has_ocr:
            # Save embedded and OCR as separate files for inspection
            for source_type, suffix in [("embedded", "_embedded"), ("ocr", "_ocr")]:
                split_result = dict(result)
                split_result["pages"] = []
                for page in result["pages"]:
                    p_copy = dict(page)
                    p_copy["text_sources"] = [i for i in page["text_sources"] if i["source"] == source_type]
                    split_result["pages"].append(p_copy)
                split_path = os.path.join(args.output_dir, f"doc_{base_name}{suffix}.json")
                with open(split_path, "w", encoding="utf-8") as f:
                    json.dump(split_result, f, indent=2, ensure_ascii=False)
                logger.info(f"Saved {source_type} result to {split_path}")
            print(f"\n[+] PDF contains both text and images: Produced 2 separate JSON files.")
        else:
            output_path = os.path.join(args.output_dir, f"doc_{base_name}_merged.json")
            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(result, f, indent=2, ensure_ascii=False)
            logger.info(f"Saved result to {output_path}")

        print("\n" + "=" * 50)
        print(f"Document Summary: {result['document_name']}")
        print(f"Type: {result['document_type'].upper()}")
        print(f"ID: {result['document_id']}")
        print(f"Total Pages: {result['total_pages']}")
        print(f"Time Taken: {result['processing_time_ms']} ms")
        for page in result["pages"]:
            embedded_count = sum(1 for item in page["text_sources"] if item["source"] == "embedded")
            ocr_count = sum(1 for item in page["text_sources"] if item["source"] == "ocr")
            audio_count = sum(1 for item in page["text_sources"] if item["source"] == "audio")
            if result["document_type"] == "audio":
                print(f"  Page {page['page_number']}: Audio blocks={audio_count}")
            elif result["document_type"] == "video":
                print(f"  Page {page['page_number']}: Video OCR blocks={ocr_count}, Audio blocks={audio_count}")
            else:
                print(f"  Page {page['page_number']}: Embedded blocks={embedded_count}, OCR blocks={ocr_count}")
        print("=" * 50)
    except Exception as e:
        logger.exception(f"Failed to process document: {e}")


if __name__ == "__main__":
    main()