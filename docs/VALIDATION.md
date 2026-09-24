# 検証記録

## 環境

- 実装担当: GPT-6-Luna。レビュー・検証実行: 主担当Codex。
- Windows: Python 3.11.9、Codex CLI 0.156.1。
- Ubuntu試験先: C:\project\ubuntu_sandbox / codex-ubuntu22-sandbox。
- コンテナPython: 3.10.12。専用venv: /home/codex/remote-agent-com-venv。
- 専用CLI: /home/codex/remote-agent-com-tools/codex-x86_64-unknown-linux-musl（0.156.1）。

## 2026-09-24: 実CLIの認証不要プロトコル試験

専用の空のCODEX_HOMEでapp-serverをstdio起動し、initialize → initialized → thread/startを送信した。thread/startはephemeral=true、cwd=/tmpとし、モデルへのturn/startは送信していない。

要求と一致する実効設定を確認:

```json
{
  "approvalPolicy": "untrusted",
  "approvalsReviewer": "user",
  "sandbox": {"type": "readOnly", "networkAccess": false},
  "model": "gpt-6-luna"
}
```

結果: 成功。試験終了後にapp-serverを終了。これは起動・設定のプロトコル互換性の確認であり、モデル利用権、実モデルのツール承認、Discordへの送受信の確認ではない。

## 後続検証

実装完了後の自動試験、コピー先での動作確認、実Discord接続の結果を追記する。
