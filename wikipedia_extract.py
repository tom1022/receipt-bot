import logging
import json
import os
import re
import requests
import wikipedia
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.DEBUG)


def fetch_and_save_wikipedia(title: str, out_dir: str = "wikipedia_data") -> dict:
    """Fetch a Japanese Wikipedia page using the `wikipedia` library,
    save the raw text and HTML, and return metadata including the page URL.
    """
    os.makedirs(out_dir, exist_ok=True)
    wikipedia.set_lang("ja")

    page = wikipedia.page(title)
    summary = page.summary
    content = page.content
    url = page.url

    meta = {"title": page.title, "url": url, "summary": summary}

    # write JSON metadata
    json_path = os.path.join(out_dir, "wikipedia_jp_area_codes.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    # write full text
    txt_path = os.path.join(out_dir, "wikipedia_jp_area_codes.txt")
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(content)

    # fetch and save HTML for richer parsing
    html = ""
    html_path = ""
    try:
        headers = {"User-Agent": "receipt-bot/1.0 (https://example.com)"}
        resp = requests.get(url, timeout=10, headers=headers)
        resp.raise_for_status()
        html = resp.text
        html_path = os.path.join(out_dir, "wikipedia_jp_area_codes.html")
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html)
    except Exception as e:
        # fallback: use MediaWiki API to get parsed HTML
        try:
            api_url = "https://ja.wikipedia.org/w/api.php"
            params = {"action": "parse", "page": title, "prop": "text", "format": "json"}
            headers = {"User-Agent": "receipt-bot/1.0 (https://example.com)"}
            api_resp = requests.get(api_url, params=params, timeout=10, headers=headers)
            api_resp.raise_for_status()
            data = api_resp.json()
            html = data.get("parse", {}).get("text", {}).get("*", "")
            if html:
                html_path = os.path.join(out_dir, "wikipedia_jp_area_codes.html")
                with open(html_path, "w", encoding="utf-8") as f:
                    f.write(html)
        except Exception as e2:
            print(f"Failed to fetch HTML (direct and API): {e} | {e2}")

    return {"meta": meta, "json_path": json_path, "txt_path": txt_path, "html_path": html_path, "html": html}


def extract_area_codes_from_html(html: str) -> list:
    """Parse the Wikipedia HTML and extract area codes (市外局番) from tables and nearby text.
    Returns a list of dicts: {code, context}.
    """
    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    candidates = []

    # Look for wikitable or sortable tables which commonly contain lists
    tables = soup.find_all("table", class_=lambda c: c and ("wikitable" in c or "sortable" in c))
    if not tables:
        # fallback: parse all tables
        tables = soup.find_all("table")

    code_pattern = re.compile(r'0\d{1,4}')

    for table in tables:
        for tr in table.find_all("tr"):
            cells = [td.get_text(separator=" ", strip=True) for td in tr.find_all(["td", "th"])]
            row_text = " | ".join(cells)
            matches = code_pattern.findall(row_text)
            for m in matches:
                candidates.append({"code": m, "context": row_text})

    # Also search paragraphs and list items as fallback
    if not candidates:
        for tag in soup.find_all(["p", "li"]):
            text = tag.get_text(" ", strip=True)
            matches = code_pattern.findall(text)
            for m in matches:
                candidates.append({"code": m, "context": text})

    # Normalize codes (remove leading zeros already present, keep as string), dedupe preserving first context
    seen = {}
    for item in candidates:
        code = re.sub(r'[^0-9]', '', item["code"])
        if not code:
            continue
        # common area codes are 2-5 digits starting with 0
        if not (code.startswith('0') and 2 <= len(code) <= 5):
            continue
        if code not in seen:
            seen[code] = item["context"]

    results = [{"code": k, "context": v} for k, v in seen.items()]
    results.sort(key=lambda x: (len(x["code"]), x["code"]))
    return results


def save_extracted_area_codes(codes: list, out_dir: str = "wikipedia_data") -> str:
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "wikipedia_jp_area_codes_extracted.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(codes, f, ensure_ascii=False, indent=2)
    return out_path


if __name__ == "__main__":
    title = "日本の市外局番"
    try:
        fetch_result = fetch_and_save_wikipedia(title)
        print("Saved Wikipedia data:", fetch_result["json_path"], fetch_result["txt_path"], fetch_result.get("html_path"))

        html = fetch_result.get("html", "")
        codes = extract_area_codes_from_html(html)
        out_path = save_extracted_area_codes(codes)
        print(f"Extracted {len(codes)} unique area codes, saved to: {out_path}")
    except Exception as e:
        print("Failed to fetch/parse Wikipedia page:", repr(e))

