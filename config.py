import os
import dotenv

dotenv.load_dotenv()

# 環境変数で上書き可能にしつつ、既存の値をデフォルトにします。
DISCORD_TOKEN = os.environ.get('DISCORD_TOKEN')

OLLAMA_API_URL = os.environ.get('OLLAMA_API_URL', "http://localhost:11434/api/chat")
MODEL_NAME = os.environ.get('MODEL_NAME', "qwen2.5vl:3b")

# 数値は環境変数があればそれを使い、なければ既存の値
_raw_target_channel = os.environ.get('TARGET_CHANNEL_ID')
try:
	TARGET_CHANNEL_ID = int(_raw_target_channel) if _raw_target_channel else None
except Exception:
	# keep as None on parse error
	TARGET_CHANNEL_ID = None

GOOGLE_SPREADSHEET_ID = os.environ.get('GOOGLE_SPREADSHEET_ID')

# サービスアカウントJSONのパスは絶対パスを推奨
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get('GOOGLE_SERVICE_ACCOUNT_JSON', './service_account.json')
GOOGLE_SHEET_NAME = os.environ.get('GOOGLE_SHEET_NAME', '家計簿')

print("Configuration Loaded:")
print(f"DISCORD_TOKEN: {'Set' if DISCORD_TOKEN else 'Not Set'}")
print(f"OLLAMA_API_URL: {OLLAMA_API_URL}")
print(f"MODEL_NAME: {MODEL_NAME}")
print(f"TARGET_CHANNEL_ID: {TARGET_CHANNEL_ID}")
print(f"GOOGLE_SPREADSHEET_ID: {GOOGLE_SPREADSHEET_ID}")
print(f"GOOGLE_SERVICE_ACCOUNT_JSON: {GOOGLE_SERVICE_ACCOUNT_JSON}")
print(f"GOOGLE_SHEET_NAME: {GOOGLE_SHEET_NAME}")
