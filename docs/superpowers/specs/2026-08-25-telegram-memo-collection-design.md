# Telegram メモ収集の分離と反映漏れの機械的検証

- 作成日: 2026-08-25
- 対象リポジトリ: Ryun7979/memoryex（public）

## 背景

日次のブログ自動投稿において、Telegram に送ったメモが記事に反映されないことが頻発している。
調査の結果、症状は「記事生成時に無視される」のではなく **そもそも Telegram からメモを取得できていない** であることが確認された。

## 根本原因

`post.py` の `get_today_messages()` は 1 日 1 回（23:00 JST）しか `getUpdates` を呼ばない。
これが Telegram Bot API の以下の制約と噛み合っていない。

1. **未受信 update の保持期限は最大 24 時間**。取得ウィンドウの下限が「前日 23:00」であり、保持期限の境界と一致する。cron の遅延や 30 分後リトライが発生した時点で、その日の早い時間帯のメモは既にサーバーから消えている。
2. **`offset=-100` は「それより前の update を全て破棄する」仕様**。1 日の更新が 100 件を超えると古い側が永久に失われる。
3. **`allowed_updates` に `edited_message` が含まれていない**。送信後に編集したメモは取得対象外になる。

いずれも「1 日 1 回しか取りに行かない」ことに起因する。取得ウィンドウを広げてもサーバー側に残っていないため解決しない。

## 目的

- Telegram メモの取りこぼしをゼロにする
- 記事生成側の反映漏れ検証を、LLM の主観判定から機械的照合に置き換える
- Gemini API の使用量を増やさない（Google AI Pro の契約範囲内に収める）

## 非目的

- 記事の文体・構成の変更
- 写真掲載機能（別途保留中の課題）
- 記事監視用の追加エージェントの導入（後述の理由により採用しない）

## アーキテクチャ

収集と記事化を分離する。

```
collect.py   2 時間ごと     Telegram getUpdates  → Secret Gist に追記
post.py      23:00 JST      Secret Gist から読み出し → 記事生成 → JUGEM 投稿
```

`collect.py` は Gemini を一切呼ばないため、AI 使用量は増加しない。
リポジトリが public であるため GitHub Actions の実行時間は無料・無制限であり、追加費用は発生しない。

メモ本文は public リポジトリにコミットしない。保存先は Secret Gist とする。

## Gist の状態ファイル

- Gist: secret gist を 1 つ使用
- ファイル名: `memoryex-state.json`（固定）

```json
{
  "offset": 123456790,
  "messages": [
    {
      "update_id": 123456789,
      "message_id": 4567,
      "ts": 1756100000,
      "date": "2026-08-25",
      "text": "メモ本文",
      "location": {"lat": 35.0, "lon": 135.7}
    }
  ]
}
```

- `offset`: 次回 `getUpdates` に渡す値（保存済み update の最大 `update_id` + 1）
- `messages`: 収集済みメッセージ。`date` は JST 基準の日付文字列
- `location` はキーごと存在しない場合がある
- 保持期間は直近 3 日分。それより古い要素は書き込み時に削除する（Gist の肥大化防止）

## collect.py の仕様

### 処理順序（取りこぼしゼロの保証）

1. Gist から state を読む（初回や空の場合は `{"offset": 0, "messages": []}` として扱う。`offset` が 0 のときは `offset` パラメータ自体を省略して呼ぶ）
2. `getUpdates?offset={offset}&limit=100&timeout=0&allowed_updates=["message","channel_post","edited_message"]` を呼ぶ
3. `TELEGRAM_CHAT_ID` に一致する更新のみ抽出し、`message_id` 単位で重複排除して `messages` に追記する
   - 同じ `message_id` が複数ある場合（メモを編集した場合）は `update_id` が大きい方＝編集後の内容を残す
4. `offset = max(update_id) + 1` を計算する
5. Gist に PATCH する
6. **PATCH が成功した場合にのみ** 新しい `offset` が永続化される

保存に失敗した場合は `offset` が進まないため、次回実行で同じ範囲を再取得できる。
`offset` を渡した時点でそれより前の update は確定（confirm）されるが、その範囲は既に Gist に保存済みであるため損失は発生しない。

`offset=-100` は使用しない。

### 抽出ルール

- `text` または `caption` を本文として扱う（写真キャプションもメモとみなす）
- `location` を持つメッセージは座標を保存する
- 本文も座標も無いメッセージは保存しない

### 実行頻度

- cron: `0 */2 * * *`（2 時間ごと、UTC 指定だが 2 時間間隔のため時差の考慮不要）
- 保持期限 24 時間に対して十分な余裕を持たせる。GitHub Actions の cron は遅延・スキップが起こり得るため、間隔を短く取ることで耐性を確保する

## post.py の変更

1. 起動時に `collect.py` と同じ収集処理を 1 回実行する（直前 2 時間分を確実に回収するため）。収集ロジックは共通関数として共有し、`post.py` からの実行時も Gist への書き込みと `offset` の更新を行う
   - `post.py` は失敗時に 30 分後リトライを繰り返す。各試行の冒頭で収集が走るため、リトライ中に届いたメモも取りこぼさない
2. `get_today_messages()` を、Gist から対象日のメッセージを読む実装に置き換える
   - 対象日の決定ロジック（深夜 0〜6 時実行なら前日扱い）は現行を踏襲する
   - 取得ウィンドウの下限「対象日の前日 23:00」も踏襲する
3. URL 要約・逆ジオコーディング・TMDB 映画情報の取得は現行どおり記事化時に行う（Gist には生データのみ保存する）
4. Gist の読み取りに失敗した場合は、`offset` を指定しない `getUpdates`（サーバー側を破棄しない読み取り専用）にフォールバックし、記事の生成・投稿自体は継続する

## 反映漏れ検証の機械化

### 現行の問題

`check_article_coverage()` には 3 つの構造的な欠陥がある。

1. 判定に 2 回失敗すると空リスト（＝漏れなし）を返すため、Gemini の 429/503/JSON パース失敗が「合格」に化ける
2. 生成したモデル自身が緩い基準で採点しているため、雰囲気だけ吸収された文でも「反映済み」と判定されやすい
3. 検証のために Gemini を追加で 1〜3 回呼び出しており、使用量を圧迫する

### 新方式

記事生成のプロンプトでデータ項目に ID を振る。

- メモ: `M1`, `M2`, ...
- 予定: `C1`, `C2`, ...
- GitHub 活動: `G1`, ...
- 共有 URL: `U1`, ...
- 訪問場所: `L1`, ...
- 映画: `V1`, ...
- 健康データ: `H1`, ...

出力 JSON を拡張する。

```json
{
  "title": "記事タイトル",
  "body": "<p>...</p>",
  "coverage": {"M1": "本文からそのまま抜粋した該当箇所", "M2": "..."}
}
```

Python 側で機械的に検証する。

1. `body` から HTML タグを除去し、空白を正規化した平文を作る
2. 各 ID について `coverage` に値が存在するかを確認する
3. その抜粋（同様に空白正規化したもの）が平文に部分文字列として実在するかを照合する
4. 存在しない ID を「未反映」とする

`coverage` キーの欠落、空文字、body に実在しない捏造した抜粋は、いずれも未反映として扱う。

### 未反映時の処理

既存の `missing_feedback` 機構を流用し、最大 2 回まで再生成する。
2 回の再生成後も残る項目は、現行どおり記事末尾に「そのほかの記録」として `<ul>` で追記する（データが失われないことを保証する）。

### 影響

`check_article_coverage()` は削除する。判定用の Gemini 呼び出しが不要になるため、記事 1 本あたりの呼び出し回数は現行より 1〜3 回減る。

## 記事監視エージェントを採用しない理由

- 今回の主因は取得漏れであり、記事を監視しても「取得できなかったメモ」は検出できない
- 監視役を増やすほど Gemini の呼び出しが増え、使用量を抑えるという制約に反する
- 検証は「LLM に採点させる」より「機械的に照合する」ほうが確実かつ安価である

## ワークフロー

### 新規: `.github/workflows/collect.yml`

- トリガー: `schedule` (`0 */2 * * *`) および `workflow_dispatch`
- 環境変数: `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`, `GIST_TOKEN`, `GIST_ID`
- 実行: `python collect.py`
- `timeout-minutes` は短く設定する（例: 10）

### 変更: `.github/workflows/daily_post.yml`

- `GIST_TOKEN`, `GIST_ID` を env に追加する

## 設定（登録済み）

| Secret 名 | 内容 |
| --- | --- |
| `GIST_ID` | secret gist の ID（URL 末尾の 32 桁） |
| `GIST_TOKEN` | classic PAT（`gist` スコープのみ） |

## エラー処理

- Gist の読み書き失敗、Telegram API エラーは `notify_telegram_error()` で Telegram に通知する
- `collect.py` の失敗時は `offset` を進めないため、次回実行で自動的に復旧する
- `GIST_TOKEN` 失効（401）時も同様に通知される。24 時間以内に対処すれば実質的な損失は発生しない
- `post.py` は Gist が読めない場合もフォールバック経路で記事を生成・投稿する

## テスト方針

- `collect.py --dry-run`: Gist に書き込まず、取得内容と算出した `offset` を標準出力に表示する
- `post.py` の `TEST_MODE` は現行どおり維持する
- 反映漏れ検証は、`coverage` 照合ロジックを純粋関数として切り出し、API を呼ばずに検証できる形にする

## 移行手順

1. secret gist を作成し `memoryex-state.json` に `{}` を入れる（完了済み）
2. `GIST_ID`, `GIST_TOKEN` を Secrets に登録する（完了済み）
3. `collect.py` と `collect.yml` を追加し、`workflow_dispatch` で手動実行して Gist に書き込まれることを確認する
4. `post.py` を Gist 読み出しに切り替える
5. 数日運用し、ログの診断出力でメモ件数が期待どおりかを確認する
