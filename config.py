import os

# 環境変数で上書き可能にしつつ、既存の値をデフォルトにします。
DISCORD_TOKEN = os.environ.get('DISCORD_TOKEN')

OLLAMA_API_URL = os.environ.get('OLLAMA_API_URL', "http://localhost:11434/api/chat")
MODEL_NAME = os.environ.get('MODEL_NAME', "qwen2.5vl:3b")

# 数値は環境変数があればそれを使い、なければ既存の値
TARGET_CHANNEL_ID = os.environ.get('TARGET_CHANNEL_ID')

GOOGLE_SPREADSHEET_ID = os.environ.get('GOOGLE_SPREADSHEET_ID')

# サービスアカウントJSONのパスは絶対パスを推奨
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get('GOOGLE_SERVICE_ACCOUNT_JSON', './service_account.json')
GOOGLE_SHEET_NAME = os.environ.get('GOOGLE_SHEET_NAME', '家計簿')
