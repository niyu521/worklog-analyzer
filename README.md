# worklog-analyzer

Claude Code (`~/.claude`)・Codex (`~/.codex`)・ブラウザ操作履歴（同梱の
`browser-activity-logger` Chrome拡張が書き出すエクスポートJSON）のローカルログを読み取り、
直近1週間の作業内容を時系列で整理して単一のHTMLレポートに可視化する、完全ローカル動作の解析ツールです。

**外部には一切送信しません。** ネットワークアクセスは行わず、認証情報・秘密情報らしきファイルは
中身を読まずにスキップし、それ以外のテキストに含まれる秘密情報らしき文字列（APIキー・トークン・
Bearer/JWT・秘密鍵ブロック・URLのトークンパラメータなど）は正規表現でマスクしてから出力します。

## リポジトリ構成

このリポジトリは、AI/ブラウザでの作業ログを「収集」する部分と「解析・可視化」する部分の
2コンポーネントで構成されています。

```
worklog-analyzer/               ← このリポジトリのルート = 解析エンジン
├─ analyze_worklogs.py          解析本体（Python標準ライブラリのみ）
├─ README.md                    このファイル
└─ browser-activity-logger/     ブラウザ操作履歴の収集用 Chrome 拡張（データ収集側）
   ├─ src/                      拡張のソース（TypeScript）
   └─ README.md                 拡張の詳細
```

### データの流れ

```
[ Claude Code ~/.claude ] ─┐
[ Codex       ~/.codex   ] ─┼─▶  analyze_worklogs.py  ─▶  worklog_report_last_7_days.html
[ ブラウザ拡張 の Export JSON ]─┘   (正規化→分類→区切り→          parsed_worklog_last_7_days.json
                                     再統合→繰り返し検出→可視化)
```

- **収集側** `browser-activity-logger/`: ブラウザでの閲覧・検索・クリック等をローカルに記録し、
  「Export JSON」でエクスポートする Chrome 拡張。Claude Code / Codex が自動でログを残すのと同じことを
  ブラウザに対して行うためのもの。詳細は
  [browser-activity-logger/README.md](browser-activity-logger/README.md)。
- **解析側** `analyze_worklogs.py`: `~/.claude` と `~/.codex` を自動探索し、上記拡張のエクスポートJSONも
  取り込んで、3ソースをまとめて解析・可視化する。以下はこの解析側の説明。

## 入力（Input）

| ソース | 具体的な入力 | 形式 |
|---|---|---|
| Claude Code | `~/.claude/projects/**/*.jsonl`, `~/.claude/history.jsonl` | JSON Lines（1行1レコード） |
| Codex | `~/.codex/sessions/**/*.jsonl`, `~/.codex/archived_sessions/**` | JSON Lines |
| ブラウザ | `browser-activity-logger` の「Export JSON」で書き出した `browser-activity-*.json` | JSON（`ExportBundle`: `{exportedAt, schemaVersion, sessionId, settings, events[]}`） |
| 指示・記憶 | `CLAUDE.md`, `AGENTS.md`, `MEMORY.md` | Markdown |

- Claude / Codex ログは**自動探索**します（`~/.claude`, `~/.codex`, カレント配下の `.claude` / `.codex`）。
- ブラウザのエクスポートJSONは `~/Downloads`・カレントディレクトリ・スクリプトと同じ場所・
  `browser-exports/` サブフォルダを自動スキャンします。任意のパスを引数で明示指定も可能:
  `python3 analyze_worklogs.py /path/to/browser-activity-20260705.json`
- いずれも「直近7日間」に更新されたもの・その期間内のイベントだけを対象にします。

## 出力（Output）

| ファイル | 形式 | 中身 |
|---|---|---|
| `worklog_report_last_7_days.html` | 自己完結HTML（外部CDN/フォント不使用、CSS埋め込み） | メインの成果物。ブラウザでそのまま開ける可視化レポート |
| `parsed_worklog_last_7_days.json` | JSON | 下記の構造化データ一式（機械可読の中間成果物） |

`parsed_worklog_last_7_days.json` のトップレベル構造:

```jsonc
{
  "generated_at": "…Z",
  "lookback_days": 7,
  "summary": { "events_detected": …, "category_minutes": {…},
               "source_event_counts": { "claude_code": …, "codex": …, "browser": … }, … },
  "events":     [ /* 共通イベント: event_id,timestamp,source,event_type,prompt_or_text,… */ ],
  "activities": [ /* atomic activity: label,category,intent,related_files,… */ ],
  "segments":   [ /* タスクセグメント: title,category,duration_minutes,files_touched,… */ ],
  "merged_tasks":[ /* 再統合タスク: 分断された同一作業をまとめたもの */ ],
  "routines":   [ /* 繰り返しパターン: occurrences,common_steps,automation_potential,… */ ],
  "automation_candidates": [ /* スキル化・自動化候補 */ ],
  "parsed_files_log": [ /* 読み込んだファイルの一覧 */ ]
}
```

## 使い方

```bash
python3 analyze_worklogs.py                 # 自動探索のみ
python3 analyze_worklogs.py ~/Downloads/browser-activity-20260705.json  # ブラウザ書き出しを明示指定
```

Python 3.9 以上、標準ライブラリのみで動作します（追加パッケージのインストール不要）。
出力はこのディレクトリ直下に上書き生成されます。

## 何をしているか

1. **探索**: `~/.claude`, `~/.codex`, カレントディレクトリ以下の `.claude` / `.codex`、
   およびブラウザのエクスポートJSON（`~/Downloads` など）を走査し、直近7日間に更新された
   ファイルのみを対象にします。認証・秘密情報らしきファイル名
   （`auth.json`, `credentials`, `token`, `secret`, `api_key`, `key`, `.env`, `.npmrc`,
   `.pypirc`, SSH鍵など）は中身を一切読みません。バイナリファイル・25MB超のファイルも除外します。
2. **正規化**: Claude Codeのセッショントランスクリプト(`projects/**/*.jsonl`)、プロンプト履歴
   (`history.jsonl`)、Codexのセッションログ(`sessions/**/*.jsonl`, `archived_sessions/**`)、
   **ブラウザ操作履歴**(`browser-activity-logger` のエクスポート `ExportBundle` JSON)、
   メモリ/指示ドキュメント(`CLAUDE.md`, `AGENTS.md`, `MEMORY.md` など)を、共通のイベント形式
   （`event_id`, `timestamp`, `source`, `event_type`, ...）に変換します。ブラウザイベントは
   ドメイン（`github.com` など）を「リポジトリ」相当として扱い、`page_view`/`search_query` を
   作業の起点、クリックや入力を証拠として正規化します。URLのクエリ文字列（トークンを含みやすい）は
   除去し、ドメインを強いカテゴリ推定シグナルとして使います（例: `github.com`→コーディング、
   `docs.google.com`→ドキュメント、`freee.co.jp`→経理）。
3. **atomic activity 分解**: 1つのプロンプトに複数の依頼が含まれる場合（「Aして、そのあとBして」
   「1. ... 2. ... 3. ...」など）、接続表現や箇条書き構造で分割し、それぞれを独立した作業単位
   として扱います。
4. **業務カテゴリ分類**: `classify_category()` という単一の純粋関数で、キーワード・ファイル
   拡張子・コマンド・リポジトリ名から分類します。将来LLM分類に差し替えられるよう、他の処理から
   独立させています。
5. **タスクセグメント化**: 同一セッション内で、時間差だけでなくカテゴリ一致・ファイル一致・
   文章の類似度・明示的な話題転換表現（「ところで」「別件ですが」等）を組み合わせて、作業の
   区切りを判定します。
6. **タスクの再統合（stitching）**: 同じリポジトリ/プロジェクトで、ファイルやタイトルが類似する
   セグメントは、時間が離れていても（例: 午前の修正と午後のPR作成）同一タスクとして束ねます。
7. **繰り返しパターン検出**: 再統合後のタスクをカテゴリとタイトル類似度でクラスタリングし、
   2回以上出現したものを「ルーティン」として検出します。
8. **自動化候補の提案**: 繰り返し頻度・手順の安定性（同じファイル/コマンドの再利用度）・
   カテゴリごとの入出力の明確さ・必要とされる人間の判断量からスコアリングし、high/medium/low
   を判定します。
9. **HTML/JSON出力**: 単一の自己完結型HTML（外部CDN・外部フォント不使用、CSSは埋め込み）と、
   根拠付きのJSONを書き出します。

## 出力の見方

`worklog_report_last_7_days.html` には次のセクションがあります。

1. サマリー（対象期間、読み込みファイル数、検出イベント/タスク数、主なプロジェクト）
2. カテゴリ別作業量（棒グラフ）
3. 日別タイムライン（日ごとに開始/終了時刻・タイトル・カテゴリ・関連ファイル）
4. 作業フロー（複数セグメントにまたがる主要タスクをステップ表示）
5. 繰り返し作業パターン（出現回数・共通ステップ・自動化可能性）
6. スキル化・自動化候補（想定入出力・手順・リスク）
7. 生ログへの根拠（イベントIDとログファイル名・行番号。内容はマスク済み）
8. 解析の限界（下記参照）

## 解析の限界（重要）

- ログ形式はアプリの内部仕様であり非公開です。将来のアップデートで構造が変わるとパーサーが
  追従できなくなる可能性があります。JSON/JSONL/Markdownを幅広く読めるよう柔軟に実装していますが、
  完全な網羅を保証するものではありません。
- `CLAUDE.md` / `AGENTS.md` / `MEMORY.md` などタイムスタンプを持たないファイルは、ファイルの
  更新時刻（mtime）を代用の作業時刻として扱っています。
- 業務カテゴリ分類・atomic activity分解・タスク再統合・繰り返しパターン検出は、いずれも
  キーワードやファイルパス、時間差に基づくルールベースの推定です。人間の意図を完全に正確に
  読み取れているわけではありません。
- 1つのプロンプト内に複数作業が含まれる場合、それぞれのサブタスクにどのツール呼び出しが対応
  するかを厳密には特定できないため、そのターン内の証拠（ファイル操作・コマンド等）をまとめて
  全サブタスクに割り当てています。
- 自動化候補の判定はヒューリスティックなスコアリングであり、実際に自動化してよいかどうかの
  最終判断は人間が行ってください。
- 読み込み対象は直近7日間に更新されたファイルに限定しているため、それより前から続く長期タスクの
  全体像は捉えきれない場合があります。
- **タスクの並行実行（インターリーブ）は扱えません。** 検索クエリログのセッション分割研究
  （Jones &amp; Klinkner, CIKM 2008）では、実際のログの17〜20%が複数タスクの並行/入れ子構造を
  持つと報告されています。本ツールは1セッション内のイベントを時系列の単純な列として扱うため、
  同じ会話の中で複数の作業が交互に進んでいた場合、実際とは異なる区切り方になることがあります。
- **時間差だけによる区切りは意図的に避けています**が、しきい値（90分のギャップ、類似度0.15〜0.30
  など）は経験的に決めたものであり、大規模な人手ラベル付きデータで検証したものではありません。
  同分野の研究（Jones &amp; Klinkner 2008）でも、時間差のみでの区切りは前後関係を無視すると精度が
  頭打ちになる一方、唯一の「正しい」しきい値というものは存在せず、時間差と語彙的な類似度を組み合わせる
  ことで大きく改善することが示されています。本ツールもその考え方（時間差＋カテゴリ一致＋ファイル一致
  ＋テキスト類似度＋明示的な話題転換表現の組み合わせ）に沿っていますが、しきい値そのものの妥当性検証は
  行っていません。
- **TextTiling/C99など古典的な文章分割アルゴリズムは、対話やチャットのような短い発話には
  そのまま向きません。** これらは数百〜数千語の長い文章での語彙の再出現を前提にしており、
  複数の研究（Galley et al. 2003; Riedl &amp; Biemann 2012 ほか）が、会議の書き起こしやチャットlog
  のような短文・対話形式のテキストでは同じ手法の誤り率が2〜4倍程度悪化することを報告しています。
  本ツールはそのため、語彙的な類似度だけに頼らず、カテゴリ一致・ファイル一致・明示的な話題転換表現
  といった「構造的な手がかり」を語彙的類似度と組み合わせて代用しています。
- **繰り返しパターンの類似度判定**（同じ作業かどうかの判定）には、文字bigram/単語/
  `difflib.SequenceMatcher` を組み合わせたテキスト類似度に加えて、各タスクを構成する作業の
  カテゴリ列（例: research→coding→coding）を簡易的な「手順の型」とみなした編集距離
  （プロセスマイニング／系列パターンマイニングにおける trace/variant clustering の簡易版）を
  併用しています。ただし本格的なプロセスマイニング（Alpha algorithm や Heuristics Miner のような
  directly-follows関係に基づくワークフローグラフの復元）は行っていません。

## セキュリティ / プライバシーについて

- ネットワーク呼び出しは一切行いません（`urllib`, `requests`, `socket` 等は使用していません）。
- ファイル名に `credential`, `token`, `secret`, `api_key`, `.env`, `.npmrc`, `.pypirc`,
  `id_rsa` 等の秘密情報を示すパターンが含まれる場合、または `.ssh` / `.aws` / `.gnupg`
  配下にある場合は、そのファイルの中身を一切読み込みません。
- それ以外のテキストであっても、APIキー・Bearerトークン・JWT・AWSアクセスキー・秘密鍵ブロック・
  `password=`/`secret=`のようなキーバリュー形式の値などを正規表現で検出し、`[REDACTED]` に
  置き換えてからJSON/HTMLに書き込みます。
- プラグイン/マーケットプレイスのソースコード、シェルスナップショット、`session-env`、
  `.claude.json.backup` などトークンや環境変数を含みやすいディレクトリは、そもそも走査対象から
  除外しています。

## 再実行について

`analyze_worklogs.py` は毎回フルスキャンして2つの出力ファイルを上書きします。副作用はなく、
何度実行しても安全です。
