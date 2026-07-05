# Dashboard connection prompt

以下を、すでにDashboardを実装しているコーディングエージェントへそのまま渡してください。

---

既存Dashboardの見た目、レイアウト、コンポーネント構成、配色、タイポグラフィ、アニメーションは変更せず、Company Brainの実データ接続だけを完成させてください。モックデータは接続完了後に削除してください。

最初にプロジェクトのフレームワークと既存データ取得方式を確認し、その流儀に合わせて実装してください。Company Brain APIはDashboardと同じVPSの `http://127.0.0.1:8420` で稼働しています。これはloopback限定なので、ブラウザから直接fetchしないでください。Dashboard側のserver route、API route、loader、server actionなどを使って同一オリジンのプロキシを作ってください。

サーバー専用環境変数として次を使用してください。

```text
COMPANY_BRAIN_API_URL=http://127.0.0.1:8420
```

接続するAPIは2つです。

```text
GET /flow-types
GET /flow-types/:flowTypeId?limit=100&offset=0
```

完全なレスポンス仕様は同梱の `FLOW_API_CONTRACT.md` を参照してください。`schema_version` は `1.0` です。

実装要件:

1. Dashboardの「請求書作成」「議事録作成」などのカード／ボタンは、`GET /flow-types` の各要素から生成する。
2. React等のkeyやURLには `flow_type_id` を使い、画面表示には `label` を使う。
3. カードには既存デザインが許す範囲で `instance_count`、`event_count`、`last_activity`、`platforms` を割り当てる。
4. カードを押したら `GET /flow-types/:flowTypeId` を取得し、そのグループに属する `instances` を表示する。
5. 各instanceは一つの成果物の来歴である。見出しには `instance.label`、現在の成果物には `latest_output` を使う。
6. 経路表示は `nodes` と `edges` から組み立てる。ノードは `captured_at` 昇順。edgeの `from` と `to` をevent IDで解決する。
7. relationの表示名は、`revision`＝「改訂」、`cross_platform_continuation`＝「ツール間の継続」、`derived_copy`＝「派生」とする。
8. ノードには `title`、`platform`、`captured_at`、`content_excerpt` を表示し、必要なら `confidence` と `rationale` を補助情報として表示する。
9. `limit` は最大200。100件を超える場合は「さらに読み込む」または既存のページングUIで `offset` を増やす。
10. loading、0件、APIエラー、404を既存Dashboardの状態表示コンポーネントへ接続する。通信失敗時にモックへ黙ってフォールバックしない。
11. TypeScriptを使っている場合はAPIレスポンス型を明示し、未知フィールドは無視できる構造にする。
12. Company Brain APIを外部公開したり、CORSを追加したり、既存APIサーバーのbind先を変更したりしない。

完了条件:

- 一覧にAPI由来の業務分類が表示される。
- 「請求書作成」を押すと、その分類に属する成果物一覧が表示される。
- 一つの成果物を開くと、作成経路がnodes/edgesどおりに表示される。
- ページ再読み込み後も同じ結果になる。
- モックデータが本番画面に残っていない。
- 既存の見た目に意図しない差分がない。
- ビルド、型検査、既存テストが成功する。

実装後、変更ファイル、プロキシURL、確認したflow_type_id、実行した検証コマンドを報告してください。

---
