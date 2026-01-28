# receipt-bot

レシート画像をOCRして解析し、Googleスプレッドシートへ記録する自動化ボットです。Discord経由で受け渡したり、ローカルで実行することができます。

## 主な機能
- 画像からのOCR抽出（`ocr_utils.py`）
- 抽出結果の整形・解析（`llm_utils.py` などの補助ユーティリティ）
- Google スプレッドシートへの追記（`sheets_utils.py`）
- Discord連携での受け取り・通知（`discord_bot.py`）

## 前提条件
- Python 3.8 以上
- Google Sheets API を有効化したサービスアカウントと `service_account.json`

## インストール（簡易）
1. リポジトリをクローン
   ```bash
   git clone <repo-url>
   cd receipt-bot
   ```
2. 仮想環境を作成して有効化（付属スクリプトあり）
   ```bash
   bash scripts/setup_venv.sh
   source .venv/bin/activate
   ```
3. 依存関係をインストール
   ```bash
   pip install -r requirements.txt
   ```

## 環境変数（`config.py` を参照）
以下の環境変数で挙動を制御します。空欄の場合はソース内のデフォルトが使われます。

- `DISCORD_TOKEN` : Discord ボット用トークン（Discord 連携を使う場合）
- `OLLAMA_API_URL` : LLM API のエンドポイント（デフォルト: `http://localhost:11434/api/chat`）
- `MODEL_NAME` : 使用するモデル名（デフォルト: `qwen2.5vl:3b`）
- `TARGET_CHANNEL_ID` : Discord の対象チャンネルID（任意）
- `GOOGLE_SPREADSHEET_ID` : 書き込み先のスプレッドシートID（必須）
- `GOOGLE_SERVICE_ACCOUNT_JSON` : サービスアカウントJSONのパス（デフォルト: `./service_account.json`）
- `GOOGLE_SHEET_NAME` : シート名（デフォルト: `家計簿`）

注意: Google Sheets に書き込む場合、作成したスプレッドシートをサービスアカウントのメールアドレスと共有してください。

## 実行方法
- ローカルで一時的に動かすには:
  ```bash
  python bot.py
  ```
- Discord ボットとして動かすには:
  ```bash
  python discord_bot.py
  ```

## systemd での常駐実行
`systemd/receipt-bot.service` と `systemd/receipt-bot.env.example` が含まれています。環境変数ファイルを作成し、サービスを配置して起動してください。

例:
```bash
cp systemd/receipt-bot.env.example /etc/receipt-bot.env
# 編集して環境変数を設定
sudo cp systemd/receipt-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now receipt-bot.service
```

## 主要なファイル
- `bot.py` - メインの実行ファイル（ユーティリティ連携）
- `discord_bot.py` - Discord 連携のエントリ
- `ocr_utils.py`, `llm_utils.py`, `sheets_utils.py` - 各種ユーティリティ
- `service_account.json` - Google サービスアカウント（機密情報を含むため管理注意）

## データ
- `wikipedia_data/` - 参照用の抽出済みデータ

## ライセンス
MIT License
`wikipedia_data/` におけるWikipediaのデータは [Creative Commons Attribution-ShareAlike License](https://creativecommons.org/licenses/by-sa/4.0/) に準拠します。
