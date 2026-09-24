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

## 2026-09-24: 実装後のローカル・Ubuntu試験

- ホストPython 3.11.9: `python -m unittest discover -s tests -v` → 13件成功。
- UbuntuコンテナPython 3.10.12: 専用venvに `requirements.txt` を導入後、同じ13件が成功。
- `scripts/copy-to-sandbox.ps1` で、Git管理下の16ファイルのみを C:\project\ubuntu_sandbox\remote_agent_com_sys へコピーできた。Git履歴・秘密設定・状態ファイルは含まれない。
- BotのAppServerクライアントからコンテナ内のCodex CLI 0.156.1を起動し、実効設定が `approvalPolicy=untrusted`、`approvalsReviewer=user`、`sandbox=readOnly`、`model=gpt-6-luna` であることを確認。
- Botから会話開始後、同一プロセスでの再利用に成功。会話開始後、最初の指示前に再起動すると保存されたrolloutがまだないため、新しい会話へ安全に置換する処理に成功。
- 実 CLI の設定ファイルには `approval_policy=untrusted` を書けない（起動時に拒否）。設定ファイルは `on-request`、会話とターンの引数では `untrusted` を指定し、会話開始・再開時の実効設定を照合している。CLI 0.156.1に依存するため、将来バージョンで拒否された場合は指示実行前に停止する。

未実施: 実Discord Botトークン・許可サーバー/チャンネル/ユーザーID、専用Codexログインがまだ設定されていないため、Discord経由の実指示、実モデルのコマンド・ファイル変更の承認、外部PCからの操作は未確認。自動試験の承認処理成功と実サービスの承認経路の確認は区別する。
