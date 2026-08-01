# iBotEz

[`English`](README.md) | [`中文`](README.zh-CN.md) | `日本語`

**macOS 向けのミニマルな iMessage ⇄ [Pi](https://pi.dev) ブリッジ。**

iBotEz はローカルの `~/Library/Messages/chat.db` を監視し、**ホワイトリストの連絡先**から届いた新しい iMessage を取り出してローカルの [Pi](https://pi.dev) エージェント（RPC モード）へ転送し、Pi の返答を Messages.app 経由で送り返します。iBotEz は*ただの橋*であり、考えるのは（モデル・スキル・ツールとも）Pi の役割です。

## 主な機能

- **テキストブリッジ**：受信 iMessage → Pi → 返信に加え、Pi の結果を連絡先へ自動送信する **cron スケジュールタスク**。
- **電話番号 / メールでホワイトリスト**を設定。対話型 `chats` コマンドで選択可能。
- chat.db の**アダプティブポーリング**（2s → 15s バックオフ）。**WAL 対応**で新着取りこぼしなし。
- **ヘルスウォッチドッグ**：Pi プロセスが死んだ場合やワーカーが止まった場合に自己再起動。
- **長時間ターンの処理**：定期的な進捗報告と、Pi が止まった際の上限付きリトライ。
- **連絡先ごとの Pi セッション**（再起動後も復元）。
- **ランタイム依存なし**——Python 3.11+ 標準ライブラリのみ。

## 必要環境

- **macOS**（26.x で構築/テスト）。**Messages.app** が iMessage にサインイン済みであること
- **Python 3.11+**
- [Pi](https://pi.dev) がインストール済みで、モデルプロバイダが設定済み（`pi config`）
- iBotEz を実行する Python インタプリタに**フルディスクアクセス**を付与（chat.db の読み取りに必要）
- Messages.app 操作の**オートメーション**権限（初回送信時にプロンプト表示）

## クイックスタート

```bash
git clone https://github.com/wengzhiwen/iBotEz.git
cd iBotEz
python3.14 -m venv venv        # Python 3.11+ なら何でも可
cp config.example.toml config.toml
venv/bin/python -m ibotez chats   # 会話を一覧し、ホワイトリストに追加するものを選択
venv/bin/python -m ibotez run     # ブリッジを起動
```

（または `pip install -e .` で `ibotez` コマンドを使用。）あとはホワイトリストの連絡先から iMessage を送るだけで、iBotEz が Pi 経由で返信します。

## 仕組み

```
連絡先 ──iMessage──▶ Messages.app ──▶ chat.db
                                  │（ポーリング・WAL 対応）
iBotEz ──prompt──▶ Pi (RPC) ──返信──▶ iBotEz ──osascript──▶ Messages.app ──▶ 連絡先
```

- chat.db を `interval_seconds` ごとに**読み取り専用**でポーリングし、水位（watermark）で差分取得。初回起動は過去分をスキップします。
- ホワイトリストの連絡先ごとに**独立した Pi セッション**を持ち、`state.json` で再起動後に復元します。
- 返信は AppleScript で Messages.app 経由で送信し、iBotEz は iMessage の資格情報には触れません。

## 設定

すべて `config.toml` に記述します（`config.example.toml` を参照）：

| セクション | キー（デフォルト） |
|---|---|
| `[poll]` | `interval_seconds`(2)、`max_interval_seconds`(15)、`backoff_factor`(1.5) |
| `[imessage]` | `db_path`(`~/Library/Messages/chat.db`) |
| `[pi]` | `command`(`["pi","--mode","rpc"]`)、`progress_interval_seconds`(30)、`no_progress_timeout_seconds`(120)、`max_retries`(2)、`append_instruction`(true) |
| `[bridge]` | `allow`(ホワイトリストの電話番号/メール)、`reply_on_error` |
| `[[schedule]]` | `cron`、`prompt`、`to`、`name` |
| `[health]` | `check_seconds`(5)、`stall_seconds`(600)、`max_depth`(100) |
| `[log]` | `file`、`level`(`INFO`) |

ホワイトリスト照合：電話番号は**末尾 10 桁**で比較、メールは小文字化します。`+1 (555) 123-4567` と `5551234567` は同一連絡先とみなされます。

## スケジュールタスク

cron スケジュールで Pi プロンプトを実行し、結果を連絡先へ送信します：

```toml
[[schedule]]
name = "morning-forex"
cron = "0 9 * * *"               # 5 フィールド: 分 時 日 月 曜(0=日)。*、*/N、N、N-M、N,M 対応
prompt = "今日のドル/円為替ニュースをまとめて。"
to = "+8613xxxxxxxx"             # 既存の iMessage 会話がある連絡先
```

## ⚠️ 重要な制限：送信は対話的セッションでのみ機能

iBotEz は**フォアグラウンド / GUI セッション**（ターミナル、tmux）で実行する必要があります。macOS は**バックグラウンドの launchd デーモンによる iMessage のスクリプト送信を黙示的にブロック**します（AppleScript は成功を返すがメッセージは届かない）。また homebrew の venv インタプリタは launchd 下で起動時にハングします。そのため：

- 対話的に実行：`venv/bin/python -m ibotez run`
- 自動再起動させたい場合はループで囲む：`while true; do venv/bin/python -m ibotez run; sleep 5; done`

本ボットは**テキスト専用**です：ファイル添付もプログラム経由では配送できないため、Pi にはファイル生成 / 送信のリクエストを断るよう指示しています。

## セキュリティ

Pi は **Bash/Read/Write/Edit** ツールを持つコーディングエージェントです。iMessage を Pi につなぐということは、ホワイトリストの連絡先が（Pi 経由で）あなたの Mac 上でコマンドを実行できることを意味します。ホワイトリストは自分が管理する番号のみにしてください。

## ライセンス

[MIT](LICENSE)
