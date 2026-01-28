import re
import json
import os
from ddgs import DDGS
from ocr_utils import normalize_ocr_text

# Load extracted area codes (if available) to validate OCR phone candidates.
AREA_CODES_FILE = os.path.join(os.path.dirname(__file__), "wikipedia_data", "wikipedia_jp_area_codes_extracted.json")
AREA_CODES = set()
try:
    if os.path.isfile(AREA_CODES_FILE):
        with open(AREA_CODES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list):
                for item in data:
                    code = item.get('code') if isinstance(item, dict) else None
                    if not code and isinstance(item, str):
                        code = item
                    if code:
                        code_digits = re.sub(r'[^0-9]', '', str(code))
                        if code_digits:
                            # keep only plausible area-code-like prefixes: start with 0, length 2-5, not all zeros, second digit not 0
                            if code_digits.startswith('0') and 2 <= len(code_digits) <= 5 and not all(c == '0' for c in code_digits) and code_digits[1] != '0':
                                AREA_CODES.add(code_digits)
except Exception:
    AREA_CODES = set()


def load_area_codes_from_file():
    """(Re)load area codes from the JSON file into AREA_CODES set."""
    global AREA_CODES
    try:
        if os.path.isfile(AREA_CODES_FILE):
            with open(AREA_CODES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                codes = set()
                if isinstance(data, list):
                    for item in data:
                        code = item.get('code') if isinstance(item, dict) else None
                        if not code and isinstance(item, str):
                            code = item
                        if code:
                            code_digits = re.sub(r'[^0-9]', '', str(code))
                            if code_digits:
                                if code_digits.startswith('0') and 2 <= len(code_digits) <= 5 and not all(c == '0' for c in code_digits) and code_digits[1] != '0':
                                    codes.add(code_digits)
                AREA_CODES = codes
    except Exception:
        AREA_CODES = set()


def normalize_phone_digits(s: str) -> str:
    if not s or not isinstance(s, str):
        return ''
    s = normalize_ocr_text(s)
    digits = re.sub(r'[^0-9]', '', s)
    return digits


def is_likely_phone_number(digits: str) -> bool:
    """
    簡易判定: 日本の電話番号として妥当性が低い数字列は検索をスキップする。
    - 必ず数字のみの文字列を受け取ること（呼び出し側で正規化済みとする）。
    - 電話番号は通常 `0` で始まり、長さは 10 または 11 が一般的。
    これに該当しないものは電話番号ではない可能性が高く、検索を行わない。
    """
    if not digits or not digits.isdigit():
        return False
    l = len(digits)
    if digits.startswith('0') and l in (10, 11):
        return True
    # If we have loaded area codes, allow numbers that start with a known area code
    if AREA_CODES:
        for code in AREA_CODES:
            if digits.startswith(code) and l >= len(code) + 4:
                return True
    # まれに0120などのフリーダイヤル（10桁）や市外局番の違いがあるため上記で網羅
    return False


def find_all_numeric_sequences(text: str, min_len: int = 6, max_len: int = 15) -> list:
    """Return all numeric-like sequences in text after OCR normalization.
    We include sequences that may contain hyphens or spaces before normalization.
    """
    if not text:
        return []
    s = normalize_ocr_text(text)
    # find sequences containing digits, hyphens, spaces, plus signs, parentheses
    raw_seqs = re.findall(r'[0-9ＯＯoOIlL\-\s＋+()（）]{%d,%d}' % (min_len, max_len), s)
    # fallback: find pure digit sequences of certain lengths
    raw_seqs += re.findall(r'\d{%d,%d}' % (min_len, max_len), s)
    # dedupe preserving order
    seen = set()
    out = []
    for r in raw_seqs:
        rstr = r.strip()
        if not rstr:
            continue
        if rstr in seen:
            continue
        seen.add(rstr)
        out.append(rstr)
    return out


def matches_known_area_code(digits: str) -> bool:
    """Return True if digits start with a known AREA_CODE (allowing for leading 0 missing).
    We check prefixes of length 2..5 (common area code lengths).
    """
    if not digits or not digits.isdigit():
        return False
    # direct match
    for length in range(2, 6):
        if len(digits) >= length:
            prefix = digits[:length]
            if prefix in AREA_CODES:
                return True
    # do not attempt to reconstruct a missing leading zero here to avoid false positives
    return False


def duckduckgo_lookup_store_by_phone(digits: str):
    try:
        if not digits or not re.fullmatch(r"\d+", digits):
            return None

        with DDGS() as ddgs:
            res_iter = ddgs.text(
                query=f"{digits} 電話番号",
                region='jp-jp',
                safesearch='off',
                max_results=7
            )
            results = list(res_iter)

        if not results:
            return None

        return results

    except Exception as e:
        print(f"duckduckgo_lookup_store_by_phone failed: {e}")
        return None


def find_store_by_phone(text):
    try:
        if not text or not isinstance(text, str):
            return None
        # ensure area codes loaded (in case file was generated after import)
        if not AREA_CODES:
            load_area_codes_from_file()

        text_norm = normalize_ocr_text(text)
        text_norm = re.sub(r'[‐‑‒–—−－]+', '-', text_norm)
        text_norm = re.sub(r'[()（）\[\]{}<>]', '', text_norm)

        # collect all numeric-like sequences from text
        candidates = find_all_numeric_sequences(text_norm, min_len=6, max_len=15)

        # explicitly prefer sequences labeled with TEL/電話
        tel_match = re.search(r'(?:tel|TEL|電話|Tel|ＴＥＬ)[:：\s]*([0-9OZz\-\s＋+()（）]{6,30})', text_norm, flags=re.I)
        if tel_match:
            tel_raw = tel_match.group(1)
            if tel_raw and tel_raw not in candidates:
                candidates.insert(0, tel_raw)

        if not candidates:
            return None

        chosen = None
        chosen_digits = None

        # Evaluate each candidate and pick the best match that aligns with known area codes
        for cand in candidates:
            digits = normalize_phone_digits(cand)
            if not digits:
                continue

            # trim unrealistic long strings
            if digits.startswith('0120') and len(digits) > 10:
                digits = digits[:10]
            elif len(digits) > 11:
                digits = digits[:11]

            # If we have area codes, require match; otherwise use heuristic
            if AREA_CODES:
                if not matches_known_area_code(digits):
                    continue
            else:
                if not is_likely_phone_number(digits):
                    continue

            # prefer 10 or 11 digit numbers
            if len(digits) in (10, 11):
                chosen = cand
                chosen_digits = digits
                break

            # otherwise take first acceptable candidate
            if chosen is None:
                chosen = cand
                chosen_digits = digits

        if not chosen_digits:
            return None

        digits = chosen_digits

        # format for readability
        if digits.startswith('0120') and len(digits) == 10:
            formatted = f"{digits[0:4]}-{digits[4:7]}-{digits[7:10]}"
        elif len(digits) == 10:
            formatted = f"{digits[0:3]}-{digits[3:6]}-{digits[6:10]}"
        elif len(digits) > 10:
            formatted = f"{digits[0:3]}-{digits[3:7]}-{digits[7:]}"
        else:
            formatted = digits

        result = {'phone': formatted, 'store': None, 'source': 'extracted', 'raw_results': None}

        try:
            digits_only = re.sub(r'[^0-9]', '', digits)
            if is_likely_phone_number(digits_only):
                ddg = duckduckgo_lookup_store_by_phone(digits_only)
                if ddg:
                    result['raw_results'] = ddg
                    result['source'] = 'duckduckgo'
            else:
                # 明らかに電話番号でない数字列は検索せずノイズを与えない
                print(f"Skipping phone lookup for unlikely phone digits: {digits_only}")
        except Exception as e:
            print(f"phone lookup failed: {e}")

        return result
    except Exception as e:
        print(f"find_store_by_phone failed: {e}")
        return None
