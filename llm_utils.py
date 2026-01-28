import base64
import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, Dict, List, Optional

import requests

from config import MODEL_NAME, OLLAMA_API_URL
from ocr_utils import extract_text_from_image

try:
    from ddgs import DDGS
except Exception:
    DDGS = None


_EXECUTOR = ThreadPoolExecutor(max_workers=6)


CATEGORY_OPTIONS = [
    "外食",
    "食費",
    "日用品(消耗品)",
    "日用品(非消耗品)",
    "交通費",
    "趣味",
    "光熱費",
    "その他",
]


def _encode_image(image_bytes: bytes) -> str:
    return base64.b64encode(image_bytes).decode("utf-8")


def _truncate(text: str, limit: int = 2000) -> str:
    if not text:
        return ""
    return text[:limit]


def _duckduckgo_search(query: str, max_results: int = 4) -> List[str]:
    if DDGS is None:
        return []
    try:
        with DDGS() as ddgs:
            results = ddgs.text(query=query, region="jp-jp", safesearch="moderate", max_results=max_results)
            snippets = []
            for item in results:
                if not isinstance(item, dict):
                    continue
                title = item.get("title", "") or ""
                body = item.get("body", "") or ""
                href = item.get("href", "") or ""
                snippet = f"{title}: {body}"
                if href:
                    snippet += f" ({href})"
                snippets.append(snippet.strip())
            return snippets
    except Exception as e:
        print(f"duckduckgo search failed: {e}")
        return []


def _extract_registration_numbers(text: str) -> List[str]:
    if not text:
        return []
    norm = text.replace(" ", "").replace("\n", "")
    candidates = set()

    pattern_with_label = re.compile(r"(?:登録番号|事業者登録番号|適格請求書|インボイス|登録番)\s*[:：]?\s*([TtＴ]?\d{13})")
    for m in pattern_with_label.finditer(norm):
        candidates.add(m.group(1))

    pattern_plain = re.compile(r"[TtＴ]?\d{13}")
    for m in pattern_plain.finditer(norm):
        candidates.add(m.group(0))

    out = []
    for c in candidates:
        digits = re.sub(r"\D", "", c)
        if len(digits) == 13:
            out.append(f"T{digits}")
    return out[:3]


def _build_rag_notes(registration_numbers: List[str]) -> str:
    if not registration_numbers:
        return ""
    notes = []
    for rid in registration_numbers:
        notes.append(f"{rid}: インボイス登録番号検出")
        ddg = _duckduckgo_search(f"{rid} 登録番号 事業者", max_results=3)
        if ddg:
            joined = " | ".join(ddg)
            notes.append(f"{rid} 検索結果: {joined}")
    return "\n".join(notes)


def _normalize_amount(value: Any) -> Optional[str]:
    if value is None:
        return None
    try:
        s = str(value)
        s = s.replace("¥", "").replace(",", "").strip()
        if not s:
            return None
        s = s.split()[0]
        m = re.match(r"(-?\d+(\.\d+)?)", s)
        if not m:
            return None
        num = m.group(1)
        if "." in num:
            return str(round(float(num), 2))
        return str(int(float(num)))
    except Exception:
        return None


def _call_ollama_json(system_prompt: str, user_prompt: str, image_base64: Optional[str] = None) -> Dict[str, Any]:
    messages: List[Dict[str, Any]] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    user_msg: Dict[str, Any] = {"role": "user", "content": user_prompt}
    if image_base64:
        user_msg["images"] = [image_base64]
    messages.append(user_msg)

    payload = {
        "model": MODEL_NAME,
        "messages": messages,
        "stream": False,
        "format": "json",
        "options": {"temperature": 0},
    }

    response = requests.post(OLLAMA_API_URL, json=payload)
    response.raise_for_status()
    data = response.json()
    content = data.get("message", {}).get("content")
    if content is None:
        return {"error": "No content returned", "raw": data}

    if isinstance(content, (dict, list)):
        return content

    try:
        return json.loads(content)
    except Exception:
        try:
            import re as _re

            m = _re.search(r"\{[\s\S]*\}", str(content))
            if m:
                return json.loads(m.group(0))
        except Exception:
            pass
    return {"raw": str(content)}


def _llm_extract_store(image_b64: str, ocr_text: str, today_str: str, rag_notes: str) -> Dict[str, Any]:
    system_prompt = (
        "あなたはレシートから店名を抽出するエージェントです。"
        "決済手段名は店名にしないでください。"
        "支店名や末尾の「店」「支店」は省略してシンプルな店名にしてください。"
    )

    user_prompt = f"""
以下の情報から店名を1つ特定してください。
- ロゴやヘッダの大きな文字を優先
- 決済手段 (PayPay, QUICPay, VISA など) は店名ではありません
- ECサイトの場合は Amazon/楽天市場/PayPay(割り勘) の判定ルールを考慮
- 本日の日付は {today_str}

出力は JSON で以下の形:
{{{{"store": "店名", "confidence": 0.0~1.0, "reason": "根拠を短く"}}}}

"""
    if rag_notes:
        user_prompt += f"\n検索ヒント:\n{rag_notes}\n"
    if ocr_text:
        user_prompt += f"\nOCRテキスト:\n{_truncate(ocr_text, 1800)}\n"

    return _call_ollama_json(system_prompt, user_prompt, image_b64)


def _llm_extract_date(image_b64: str, ocr_text: str, today_str: str) -> Dict[str, Any]:
    system_prompt = "あなたはレシートから取引日時を抽出するエージェントです。未来日や不自然な日付は補正してください。"
    user_prompt = f"""
レシート画像と OCR テキストから取引日付を抽出してください。
- 最も信頼できる日付を1つ
- 出力フォーマットは YYYY-MM-DD
- 時刻があれば HH:MM も返す
- 本日: {today_str}

出力 JSON:
{{{{"date": "YYYY-MM-DD", "time": "HH:MM" または null, "confidence": 0.0~1.0}}}}

"""
    if ocr_text:
        user_prompt += f"OCRテキスト:\n{_truncate(ocr_text, 2000)}\n"
    return _call_ollama_json(system_prompt, user_prompt, image_b64)


def _llm_extract_total_amount(image_b64: str, ocr_text: str) -> Dict[str, Any]:
    system_prompt = "あなたはレシートから支払総額を抽出するエージェントです。"
    user_prompt = """
支払金額を1つ決定してください。
- 「合計」「お預り」「クレジット」「ご請求額」「注文合計」の近くにある最大の金額を優先
- 税抜小計や割引、ポイント残高は除外
- 金額は整数の円(JPY)として返してください
- 出力 JSON: {{"total_amount": 1234, "currency": "JPY", "confidence": 0.0~1.0, "reason": "根拠を短く"}}
"""
    if ocr_text:
        user_prompt += f"\nOCRテキスト:\n{_truncate(ocr_text, 2000)}\n"
    return _call_ollama_json(system_prompt, user_prompt, image_b64)


def _llm_classify_category(ocr_text: str, store: str, total_amount: Optional[str]) -> Dict[str, Any]:
    system_prompt = "あなたは家計簿カテゴリ分類のエージェントです。必ず指定カテゴリのいずれか1つを返してください。"
    category_list = ", ".join(CATEGORY_OPTIONS)
    user_prompt = f"""
以下からカテゴリを1つだけ選び、JSONで返してください。
カテゴリ候補: {category_list}

優先ルール:
- 店名と商品行から推測
- 複数カテゴリが混在する場合は金額が大きいと推測されるカテゴリを採用
- 判定が難しい場合は「その他」

出力 JSON: {{{{"category": "候補のどれか", "reason": "短い根拠"}}}}

店名: {store or "不明"}
推定支払金額: {total_amount or "不明"}

"""
    if ocr_text:
        user_prompt += f"OCRテキスト:\n{_truncate(ocr_text, 2000)}\n"
    return _call_ollama_json(system_prompt, user_prompt, None)


def analyze_receipt_with_ollama(image_bytes: bytes) -> str:
    """
    レシート解析を4つのLLM呼び出しに分割し、結果を統合したJSONを返す。
    - 店名抽出（インボイス登録番号のDDG検索をヒントに活用）
    - 取引日時抽出
    - 購入カテゴリ分類
    - 支払金額抽出
    """
    try:
        image_b64 = _encode_image(image_bytes)
    except Exception as e:
        return json.dumps({"error": f"image encode failed: {e}"}, ensure_ascii=False)

    try:
        ocr_text = extract_text_from_image(image_bytes)
    except Exception as e:
        ocr_text = ""
        print(f"OCR failed: {e}")

    today_str = datetime.now().strftime("%Y-%m-%d")
    registration_numbers = _extract_registration_numbers(ocr_text)
    rag_notes = _build_rag_notes(registration_numbers)

    store_future = _EXECUTOR.submit(_llm_extract_store, image_b64, ocr_text, today_str, rag_notes)
    date_future = _EXECUTOR.submit(_llm_extract_date, image_b64, ocr_text, today_str)
    amount_future = _EXECUTOR.submit(_llm_extract_total_amount, image_b64, ocr_text)

    try:
        store_info = store_future.result()
    except Exception as e:
        print(f"store extraction failed: {e}")
        store_info = {}

    try:
        date_info = date_future.result()
    except Exception as e:
        print(f"date extraction failed: {e}")
        date_info = {}

    try:
        amount_info = amount_future.result()
    except Exception as e:
        print(f"amount extraction failed: {e}")
        amount_info = {}

    try:
        total_amount_value = amount_info.get("total_amount") if isinstance(amount_info, dict) else None
        normalized_total = _normalize_amount(total_amount_value)
        category_future = _EXECUTOR.submit(
            _llm_classify_category,
            ocr_text,
            store_info.get("store") if isinstance(store_info, dict) else None,
            normalized_total,
        )
        category_info = category_future.result()
    except Exception as e:
        print(f"category classification failed: {e}")
        category_info = {}

    normalized_amount = _normalize_amount(amount_info.get("total_amount") if isinstance(amount_info, dict) else None)

    result: Dict[str, Any] = {
        "date": (date_info.get("date") if isinstance(date_info, dict) else None) or "",
        "store": (store_info.get("store") if isinstance(store_info, dict) else None) or "",
        "total_amount": normalized_amount or "",
        "category": (category_info.get("category") if isinstance(category_info, dict) else None) or "",
        "meta": {
            "store_confidence": store_info.get("confidence") if isinstance(store_info, dict) else None,
            "date_confidence": date_info.get("confidence") if isinstance(date_info, dict) else None,
            "amount_confidence": amount_info.get("confidence") if isinstance(amount_info, dict) else None,
            "category_reason": category_info.get("reason") if isinstance(category_info, dict) else None,
            "registration_numbers": registration_numbers,
        },
    }

    try:
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e), "partial": result}, ensure_ascii=False)

