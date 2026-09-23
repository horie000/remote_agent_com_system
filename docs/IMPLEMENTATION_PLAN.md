# 実装順序・担当モデル

実装コード・試験コード・スクリプトは全てユーザー指定のGPT-6-Lunaが担当する。主担当Codexは仕様策定・レビュー・試験実行・Git操作を担当する。

| 順序 | 作業 | 担当モデル | Gitの区切り |
| --- | --- | --- | --- |
| 1 | 仕様、承認境界、試験環境、実装計画 | GPT-6（主担当Codex） | docs: specification |
| 2 | 設定・認可・stdio RPC・会話管理 | GPT-6-Luna | 次工程と合わせfeat |
| 3 | Discord指示・状態・停止・応答・承認 | GPT-6-Luna | feat: bridge |
| 4 | 自動試験・README・試験運用手順 | GPT-6-Luna | test/docs |
| 5 | レビュー結果の修正 | GPT-6-Luna | fix |
| 6 | Ubuntu試験・実接続確認 | 主担当Codex＋利用者の認証設定 | docs: validation |

仕様を記録後に実装を開始する。意味のある区切りでcommit/pushし、force pushは使わない。秘密情報を除外する。認証がない場合もローカル試験まで実施し、実接続試験の未実施項目を明記する。設定済みリモート horie000/remote_agent_com_system へのpushはユーザーが明示許可済み。

