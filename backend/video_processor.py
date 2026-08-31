import os
import time
import logging
import concurrent.futures
import tempfile
import cv2
import av
from PIL import Image
import io
import numpy as np
from rapidfuzz import fuzz

logger = logging.getLogger("document_pipeline")


def calculate_ssim(img1, img2):
    """
    Calculates the SSIM (Structural Similarity Index) between two frames.
    Focuses on the active text/edge regions to handle sparse solid-color backgrounds.
    """
    gray1 = cv2.cvtColor(img1, cv2.COLOR_BGR2GRAY)
    gray2 = cv2.cvtColor(img2, cv2.COLOR_BGR2GRAY)
    
    # Standardize image size for edge detection
    gray1_std = cv2.resize(gray1, (512, 512))
    gray2_std = cv2.resize(gray2, (512, 512))
    
    # Detect edges to find the active text area
    edges1 = cv2.Canny(gray1_std, 30, 100)
    edges2 = cv2.Canny(gray2_std, 30, 100)
    edges = cv2.bitwise_or(edges1, edges2)
    
    pts = np.argwhere(edges > 0)
    if len(pts) > 10:  # Need enough edge points to be meaningful
        y_min, x_min = pts.min(axis=0)
        y_max, x_max = pts.max(axis=0)
        
        # Add some padding
        y_min = max(0, y_min - 10)
        x_min = max(0, x_min - 10)
        y_max = min(511, y_max + 10)
        x_max = min(511, x_max + 10)
        
        if (y_max - y_min) >= 16 and (x_max - x_min) >= 16:
            crop1 = gray1_std[y_min:y_max+1, x_min:x_max+1]
            crop2 = gray2_std[y_min:y_max+1, x_min:x_max+1]
        else:
            crop1 = gray1_std
            crop2 = gray2_std
    else:
        crop1 = gray1_std
        crop2 = gray2_std
        
    # Resize the active region to 256x256 for uniform SSIM calculation
    crop1_res = cv2.resize(crop1, (256, 256))
    crop2_res = cv2.resize(crop2, (256, 256))
    
    # SSIM formula constants
    C1 = 6.5025
    C2 = 58.5225

    I1 = crop1_res.astype(np.float32)
    I2 = crop2_res.astype(np.float32)

    mu1 = cv2.GaussianBlur(I1, (11, 11), 1.5)
    mu2 = cv2.GaussianBlur(I2, (11, 11), 1.5)

    mu1_sq = mu1 ** 2
    mu2_sq = mu2 ** 2
    mu1_mu2 = mu1 * mu2

    sigma1_sq = cv2.GaussianBlur(I1 ** 2, (11, 11), 1.5) - mu1_sq
    sigma2_sq = cv2.GaussianBlur(I2 ** 2, (11, 11), 1.5) - mu2_sq
    sigma12 = cv2.GaussianBlur(I1 * I2, (11, 11), 1.5) - mu1_mu2

    num = (2 * mu1_mu2 + C1) * (2 * sigma12 + C2)
    den = (mu1_sq + mu2_sq + C1) * (sigma1_sq + sigma2_sq + C2)

    ssim_map = num / den
    return float(np.mean(ssim_map))


def extract_audio_track(video_path, output_wav_path):
    """
    Extracts the audio track from the video and saves it to a WAV file using PyAV.
    Resamples to 16kHz mono.
    """
    logger.info(f"Extracting audio track from video {video_path} to {output_wav_path}...")
    input_container = av.open(video_path)
    audio_stream = next((s for s in input_container.streams if s.type == 'audio'), None)
    if not audio_stream:
        logger.warning(f"No audio stream found in video: {video_path}")
        input_container.close()
        return False
        
    output_container = av.open(output_wav_path, 'w')
    output_stream = output_container.add_stream('pcm_s16le', rate=16000, layout='mono')
    
    resampler = av.AudioResampler(
        format='s16',
        layout='mono',
        rate=16000
    )
    
    for packet in input_container.demux(audio_stream):
        for frame in packet.decode():
            resampled_frames = resampler.resample(frame)
            for rf in resampled_frames:
                packets = output_stream.encode(rf)
                for p in packets:
                    output_container.mux(p)
                    
    # Flush resampler and encoder
    packets = output_stream.encode(None)
    for p in packets:
        output_container.mux(p)
        
    input_container.close()
    output_container.close()
    logger.info(f"Audio extraction complete. Size: {os.path.getsize(output_wav_path)} bytes")
    return True


def process_frames(pipeline, video_path):
    """
    Extracts 1 frame every 2 seconds.
    Calculates SSIM score between current and previous frame.
    If SSIM < 0.85 -> perform OCR.
    If SSIM > 0.90 -> skip OCR.
    If 0.85 <= SSIM <= 0.90 -> perform OCR, apply stricter text comparison:
      If text is > 95.0% similar to previous kept frame, discard results.
    """
    logger.info(f"Starting frame extraction and OCR for video: {video_path}...")
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise ValueError(f"Could not open video file: {video_path}")
        
    fps = cap.get(cv2.CAP_PROP_FPS)
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    
    width = 0.0
    height = 0.0
    if fps <= 0 or total_frames <= 0:
        cap.release()
        raise ValueError(f"Invalid video parameters: FPS={fps}, total_frames={total_frames}")
        
    duration = total_frames / fps
    logger.info(f"Video FPS: {fps}, Total Frames: {total_frames}, Duration: {duration:.2f}s")
    
    frame_sources = []
    
    prev_frame = None
    prev_kept_text = ""
    
    # Extract 1 frame every 2 seconds
    for t in range(0, int(duration) + 1, 2):
        frame_idx = int(round(t * fps))
        if frame_idx >= total_frames:
            break
            
        cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
        ret, frame = cap.read()
        if not ret:
            continue
            
        # Get frame dimensions
        height, width, _ = frame.shape
        
        # Check SSIM transition against the previous frame
        should_ocr = True
        ssim_val = 1.0
        
        if prev_frame is not None:
            try:
                ssim_val = calculate_ssim(frame, prev_frame)
                logger.info(f"Frame at {t}s vs previous frame: SSIM={ssim_val:.4f}")
                
                if ssim_val > 0.90:
                    logger.info(f"Frame at {t}s skipped (SSIM > 0.90 - identical frame)")
                    should_ocr = False
                elif ssim_val < 0.85:
                    logger.info(f"Frame at {t}s triggers OCR (SSIM < 0.85 - slide change)")
                    should_ocr = True
                else:
                    logger.info(f"Frame at {t}s in fuzzy zone (0.85 <= SSIM <= 0.90) - running OCR for validation")
                    should_ocr = True
            except Exception as ssim_err:
                logger.error(f"Error calculating SSIM for frame at {t}s: {ssim_err}")
                should_ocr = True
        else:
            logger.info(f"First frame at {t}s triggers OCR")
            should_ocr = True
            
        # Keep current frame for next transition calculation
        prev_frame = frame.copy()
        
        if not should_ocr:
            continue
            
        # Convert BGR to RGB
        frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        pil_img = Image.fromarray(frame_rgb)
        
        # Save to memory bytes
        img_byte_arr = io.BytesIO()
        pil_img.save(img_byte_arr, format='PNG')
        image_bytes = img_byte_arr.getvalue()
        
        # Run OCR
        try:
            t0 = time.perf_counter()
            words_data = pipeline._vision_ocr(image_bytes)
            duration_ms = round((time.perf_counter() - t0) * 1000, 2)
            logger.info(f"Frame at {t}s: OCR took {duration_ms} ms, found {len(words_data)} words")
            
            # Form text sources
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
                
            # Merge multiline OCR within this frame
            merged_frame_sources = pipeline.merge_multiline_ocr(text_sources)
            
            # Concatenate text to get full frame text representation
            current_frame_text = " ".join(block["text"] for block in merged_frame_sources).strip()
            
            # Stricter text comparison in fuzzy zone
            if prev_frame is not None and 0.85 <= ssim_val <= 0.90:
                text_sim = fuzz.ratio(current_frame_text.lower(), prev_kept_text.lower())
                logger.info(f"Frame at {t}s fuzzy zone text similarity vs previous kept: {text_sim:.2f}%")
                if text_sim > 95.0:
                    logger.info(f"Frame at {t}s discarded (Text similarity {text_sim:.2f}% > 95.0%)")
                    continue
            
            # If kept, apply timestamp offsets and update prev_kept_text
            offset_y = float(t) * 10000.0
            for block in merged_frame_sources:
                x0, y0, x1, y1 = block["bbox"]
                block["bbox"] = [x0, y0 + offset_y, x1, y1 + offset_y]
                block["metadata"] = {
                    "timestamp": float(t),
                    "bbox": [x0, y0, x1, y1]  # Original spatial bbox
                }
                
            frame_sources.extend(merged_frame_sources)
            prev_kept_text = current_frame_text
            
        except Exception as e:
            logger.error(f"Error running OCR on frame at {t}s: {e}")
            
    cap.release()
    return frame_sources, width, height


def process_audio_branch(pipeline, video_path):
    """
    Strips the audio from the video, saves to a temporary wav,
    runs it through process_audio(), updates bboxes for precleaning sorting,
    and cleans up the temp file.
    """
    temp_dir = os.path.dirname(video_path)
    temp_wav_path = os.path.join(temp_dir, f"temp_audio_{os.urandom(8).hex()}.wav")
    
    audio_sources = []
    try:
        has_audio = extract_audio_track(video_path, temp_wav_path)
        if has_audio and os.path.exists(temp_wav_path):
            # Run the existing process_audio unchanged
            audio_pages = pipeline.process_audio(temp_wav_path)
            if audio_pages and len(audio_pages) > 0:
                audio_sources = audio_pages[0].get("text_sources", [])
                
                # Apply timestamp offsets to audio bboxes as well
                for block in audio_sources:
                    start_time = block["metadata"].get("start_time", 0.0)
                    block["bbox"] = [0.0, start_time * 10000.0, 0.0, start_time * 10000.0]
                    block["metadata"]["start_time"] = start_time
    except Exception as e:
        logger.error(f"Error transcribing video audio track: {e}")
    finally:
        # Guarantee cleanup of temporary audio track
        if os.path.exists(temp_wav_path):
            try:
                os.remove(temp_wav_path)
                logger.info(f"Cleaned up temporary audio track: {temp_wav_path}")
            except Exception as clean_err:
                logger.warning(f"Failed to clean up temporary audio track: {clean_err}")
                
    return audio_sources


def process_video(pipeline, file_path, extraction_mode="both"):
    """
    Main entry point for processing video files.
    """
    logger.info(f"Entering Video Branch for: {file_path} (mode={extraction_mode})")
    
    t_start = time.perf_counter()
    
    frame_sources = []
    audio_sources = []
    width = 0.0
    height = 0.0
    
    if extraction_mode == "frames":
        frame_sources, width, height = process_frames(pipeline, file_path)
    elif extraction_mode == "audio":
        audio_sources = process_audio_branch(pipeline, file_path)
    elif extraction_mode == "both":
        # Run parallel extraction
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            future_frames = executor.submit(process_frames, pipeline, file_path)
            future_audio = executor.submit(process_audio_branch, pipeline, file_path)
            
            try:
                frame_sources, width, height = future_frames.result()
            except Exception as e:
                logger.error(f"Frame processing thread failed: {e}")
                
            try:
                audio_sources = future_audio.result()
            except Exception as e:
                logger.error(f"Audio processing thread failed: {e}")
    else:
        raise ValueError(f"Unknown extraction mode: {extraction_mode}")
        
    merged_sources = frame_sources + audio_sources
    
    # Sort chronologically by the timestamp offset encoded in bbox[1]
    merged_sources.sort(key=lambda b: b["bbox"][1])
    
    duration_ms = round((time.perf_counter() - t_start) * 1000, 2)
    
    return [{
        "page_number": 1,
        "width": float(width),
        "height": float(height),
        "text_sources": merged_sources,
        "video_duration_ms": duration_ms
    }]
