import io
import re
import threading
import cv2
import numpy as np
from PIL import Image
import unicodedata
from paddleocr import PaddleOCR

ocr_engine = PaddleOCR(
    lang='japan',
    ocr_version='PP-OCRv5',
    use_textline_orientation=True,
    text_det_limit_side_len=2048
)

# PaddleOCR is not thread-safe; guard calls with a global lock.
_OCR_LOCK = threading.Lock()


def preprocess_image_np(img_np):
    try:
        img = img_np.copy()
        gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)

        clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
        eq = clahe.apply(gray)

        denoised = cv2.bilateralFilter(eq, d=9, sigmaColor=75, sigmaSpace=75)

        th = cv2.adaptiveThreshold(denoised, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                   cv2.THRESH_BINARY, 31, 10)

        proc = cv2.cvtColor(th, cv2.COLOR_GRAY2RGB)
        return proc
    except Exception as e:
        print(f"preprocess_image_np failed: {e}")
        return img_np


def normalize_ocr_text(text: str) -> str:
    if not text or not isinstance(text, str):
        return text

    s = unicodedata.normalize('NFKC', text)

    replace_map = {
        'Z': '2', 'z': '2',
        'Ｏ': '0', 'O': '0', 'o': '0',
        'I': '1', 'l': '1', '|': '1',
        'S': '5', 's': '5',
        'Ｂ': '8', 'B': '8',
        '—': '-', '–': '-', '−': '-', '－': '-', '‐': '-', '‑': '-', '‒': '-',
        '，': ',', '。': '.', '、': ',',
    }

    out_chars = []
    for ch in s:
        out_chars.append(replace_map.get(ch, ch))
    s = ''.join(out_chars)
    s = re.sub(r'(?<=\d)[\s,](?=\d)', '', s)
    s = re.sub(r'(?<=\d)\.(?=\d{3}(?!\d))', '', s)
    s = s.replace('¥ ', '¥')

    return s.strip()


def assemble_rec_texts(raw_result, score_thresh=0.45):
    try:
        if isinstance(raw_result, (list, tuple)) and len(raw_result) > 0 and isinstance(raw_result[0], dict):
            payload = raw_result[0]
        elif isinstance(raw_result, dict):
            payload = raw_result
        else:
            return []

        rec_texts = payload.get('rec_texts') or payload.get('rec_texts', None)
        rec_scores = payload.get('rec_scores')
        rec_boxes = payload.get('rec_boxes')

        if rec_texts is None and 'data' in payload and isinstance(payload['data'], dict):
            rec_texts = payload['data'].get('rec_texts')
            rec_scores = payload['data'].get('rec_scores')
            rec_boxes = payload['data'].get('rec_boxes')

        if not rec_texts:
            return []

        entries = []
        for idx, txt in enumerate(rec_texts):
            s = None
            box = None
            if rec_scores and idx < len(rec_scores):
                try:
                    s = float(rec_scores[idx])
                except Exception:
                    s = None
            if rec_boxes is not None and idx < len(rec_boxes):
                box = rec_boxes[idx]

            if s is not None and s < score_thresh:
                continue
            if txt is None:
                continue
            entries.append((idx, str(txt), s if s is not None else 0.0, box))

        if not entries:
            return []

        def y_of(entry):
            box = entry[3]
            if box is None:
                return 0
            try:
                ys = [int(p[1]) for p in box]
                return min(ys)
            except Exception:
                return 0

        entries.sort(key=lambda e: (y_of(e), e[0]))

        lines = []
        current_line = []
        last_y = None
        for e in entries:
            y = y_of(e)
            if last_y is None:
                current_line = [e]
                last_y = y
                continue
            if abs(y - last_y) <= 12:
                current_line.append(e)
                last_y = int((last_y + y) / 2)
            else:
                try:
                    current_line.sort(key=lambda it: int(min([p[0] for p in (it[3] or [[0,0]])])))
                except Exception:
                    pass
                lines.append(" ".join([it[1] for it in current_line]))
                current_line = [e]
                last_y = y

        if current_line:
            try:
                current_line.sort(key=lambda it: int(min([p[0] for p in (it[3] or [[0,0]])])))
            except Exception:
                pass
            lines.append(" ".join([it[1] for it in current_line]))

        return lines
    except Exception as e:
        print(f"assemble_rec_texts failed: {e}")
        return []

def extract_text_from_image(image_bytes):
    image = Image.open(io.BytesIO(image_bytes)).convert('RGB')
    image_np = np.array(image)

    h, w = image_np.shape[:2]
    if max(h, w) > 800:
        scale = 800 / max(h, w)
        image_np = cv2.resize(image_np, None, fx=scale, fy=scale)

    raw_result = None
    try:
        with _OCR_LOCK:
            raw_result = ocr_engine.predict(image_np)
    except Exception:
        try:
            with _OCR_LOCK:
                raw_result = ocr_engine.ocr(image_np)
        except Exception as e:
            print(f"PaddleOCR both predict and ocr failed: {e}")
            return ""

    try:
        preview = str(raw_result)[:1000]
    except Exception:
        preview = repr(type(raw_result))

    try:
        assembled = assemble_rec_texts(raw_result, score_thresh=0.4)
        if assembled:
            return "\n".join(assembled)
    except Exception as e:
        print(f"assemble_rec_texts early attempt failed: {e}")

    def extract_texts_from_result(rr):
        texts = []
        if rr is None:
            return texts

        if isinstance(rr, dict):
            rr = rr.get('data') or rr.get('result') or rr

        if isinstance(rr, (list, tuple)):
            for item in rr:
                if isinstance(item, (list, tuple)) and len(item) >= 2:
                    second = item[1]
                    if isinstance(second, (list, tuple)) and len(second) > 0:
                        texts.append(str(second[0]))
                    elif isinstance(second, str):
                        texts.append(second)
                    else:
                        texts.extend(extract_texts_from_result(item))
                else:
                    if isinstance(item, (list, tuple)):
                        texts.extend(extract_texts_from_result(item))
                    else:
                        texts.append(str(item))
        elif isinstance(rr, str):
            texts.append(rr)

        return texts

    parsed_texts = extract_texts_from_result(raw_result)
    parsed_texts = [t.strip() for t in parsed_texts if t and t.strip()]

    if not parsed_texts or (parsed_texts and all(len(t) <= 2 for t in parsed_texts) and len(parsed_texts) > 5):
        try:
            proc = preprocess_image_np(image_np)
            raw_proc = None
            try:
                with _OCR_LOCK:
                    raw_proc = ocr_engine.predict(proc)
            except Exception:
                with _OCR_LOCK:
                    raw_proc = ocr_engine.ocr(proc)

            assembled2 = assemble_rec_texts(raw_proc, score_thresh=0.35)
            if assembled2:
                return "\n".join(assembled2)

            parsed_proc = extract_texts_from_result(raw_proc)
            parsed_proc = [t.strip() for t in parsed_proc if t and t.strip()]
            if parsed_proc and any(len(t) > 1 for t in parsed_proc):
                return "\n".join(parsed_proc)
        except Exception as e:
            print(f"preprocessed OCR attempt failed: {e}")

    if parsed_texts and all(len(t) == 1 for t in parsed_texts) and len(parsed_texts) > 5:
        try:
            image_bgr = cv2.cvtColor(image_np, cv2.COLOR_RGB2BGR)
            raw2 = None
            try:
                with _OCR_LOCK:
                    raw2 = ocr_engine.predict(image_bgr)
            except Exception:
                with _OCR_LOCK:
                    raw2 = ocr_engine.ocr(image_bgr)
            parsed2 = extract_texts_from_result(raw2)
            parsed2 = [t.strip() for t in parsed2 if t and t.strip()]
            if parsed2 and any(len(t) > 1 for t in parsed2):
                return "\n".join(parsed2)
        except Exception as e:
            print(f"Retry with BGR failed: {e}")

    normalized_lines = [normalize_ocr_text(line) for line in parsed_texts]

    if not normalized_lines:
        return ""

    return "\n".join(normalized_lines)
