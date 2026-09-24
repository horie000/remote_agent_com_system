# Discord Remote Codex Bridge

指定した Discord サーバー・チャンネル・ユーザーから指示を受け取り、この Bot が起動する Codex app-server に作業を依頼する試験用ブリッジです。`!codex ask`、`status`、`stop`、`new`、`approve`、`deny`、`help` を提供します。既存の Codex Desktop や他の Codex CLI セッションを検出して操作する機能ではありません。

Bot は Codex app-server とローカル stdio で通信します。遠隔操作用の待受ポートは追加しません。モデルの既定値は `gpt-6-luna` で、利用できない場合に別モデルへ自動変更しません。

## セキュリティ境界

- サーバー ID、チャンネル ID、許可ユーザー ID が一致するメッセージだけを受け付けます。DM、Bot、Webhook、許可外ユーザーの指示は実行しません。
- Codex は `read-only` sandbox と実行前承認で起動し、危険な操作は Discord の承認待ちにします。承認は表示された操作 1 件に限り、期限付きです。セッション全体の許可や永続的なポリシー変更は行いません。
- 承認内容を確認してから承認してください。承認されたコマンドや変更が内部で行うすべての処理を個別に承認する仕組みではありません。
- この設定が保護するのはコンテナ内の作業環境です。Windows ホスト全体の操作権限を与えるものではありません。試験中もコンテナのユーザー、マウント、ソケット、ポートを拡張しないでください。
- `read-only` で読み取り可能なファイルは承認対象外です。作業ディレクトリに秘密情報を置かず、Codex 専用ホームに個人用の Codex 設定、MCP、フックを持ち込まないでください。

## 試験環境へのコピー

Windows 側の `C:\project\remote_agent_com_sys` を原本として使い、次のスクリプトは Git 管理下にある許可対象のソース、テスト、ドキュメント、依存関係・設定例だけを `C:\project\ubuntu_sandbox\remote_agent_com_sys` にコピーします。Git 履歴、未追跡ファイル、`.env`、認証情報、状態ファイル、キャッシュはコピーしません。対象サブディレクトリ内の同じ相対パスにある管理対象ファイルは更新しますが、削除処理は行いません。

```powershell
cd C:\project\remote_agent_com_sys
./scripts/copy-to-sandbox.ps1
```

コピー先は既存の `codex-ubuntu22-sandbox` コンテナから `/workspace` にマウントされています。既存のマウントやサービス設定は変更しません。以降のコマンドはコンテナ内の `codex` ユーザーとして実行します。

```powershell
docker exec --user codex --workdir /workspace/remote_agent_com_sys codex-ubuntu22-sandbox bash -lc 'python3.10 -m venv /home/codex/remote-agent-com-venv && /home/codex/remote-agent-com-venv/bin/pip install -r requirements.txt'
```

専用 Codex CLI は `/home/codex/remote-agent-com-tools/codex-x86_64-unknown-linux-musl` に配置されています。Discord Bot トークンや Codex 認証情報を Windows からコピーしないでください。コンテナ内に専用の設定ファイルを作り、Bot の作業ディレクトリの外に保管します。例えば `/home/codex/.config/remote-agent-com/bridge.env` に必要な環境変数を手動で記入し、所有者だけが読める権限にします。実際の認証方式は別途、専用 `CODEX_HOME` を使って設定してください。普段使いの Codex ホームをコピーまたは再利用しないでください。

必要な環境変数:

| 変数 | 内容 |
| --- | --- |
| `DISCORD_BOT_TOKEN` | この Bot 専用の Discord トークン |
| `DISCORD_GUILD_ID` | 許可するサーバー ID |
| `DISCORD_CHANNEL_ID` | 許可するチャンネル ID |
| `DISCORD_ALLOWED_USER_IDS` | 許可ユーザー ID。複数の場合はカンマ区切り |
| `CODEX_WORKDIR` | Codex が作業する既存ディレクトリ |
| `BRIDGE_CODEX_HOME` | Bot 専用 Codex 設定・認証用ホーム。作業ディレクトリの外に置く |
| `CODEX_BIN` | 専用 Codex CLI の絶対パス |

`CODEX_MODEL`、`APPROVAL_TIMEOUT_SECONDS`、`MAX_INPUT_CHARS`、`BRIDGE_STATE_DIR` は任意です。既定モデルは `gpt-6-luna`、承認期限は 120 秒です。許可 ID は Discord の開発者モードで確認し、必要最小限の利用者だけ登録してください。Bot には対象チャンネルの閲覧、メッセージ履歴の閲覧、メッセージ送信に必要な権限だけを付与します。管理者権限は不要です。Prefix コマンドの本文を読むため、Discord Developer Portal で Message Content Intent を有効にします。

`CODEX_BIN` と `BRIDGE_CODEX_HOME` を設定ファイルに指定した後、まず専用ホームで Codex にログインします。`--device-auth` の案内に従って認証を完了してください。通常使用している `CODEX_HOME` や認証ファイルはコピーしません。

```bash
set -a
. /home/codex/.config/remote-agent-com/bridge.env
set +a
mkdir -p "$BRIDGE_CODEX_HOME"
CODEX_HOME="$BRIDGE_CODEX_HOME" "$CODEX_BIN" login --device-auth
```

秘密を含む設定ファイルは例えば次のように読み込めます。ファイルの値はシェルの `KEY=value` 形式で記述します。

```bash
chmod 600 /home/codex/.config/remote-agent-com/bridge.env
```

この設定を使うたびに、同じシェルで `set -a; . /home/codex/.config/remote-agent-com/bridge.env; set +a` を実行してください。トークンをコマンド履歴やチャットへ貼り付けないでください。

Bot には長い差分の添付ファイル送信に使う Attach Files 権限も必要です。

## 起動と操作

コンテナ内でテストを実行し、Bot を起動します。

```powershell
docker exec --user codex --workdir /workspace/remote_agent_com_sys codex-ubuntu22-sandbox /home/codex/remote-agent-com-venv/bin/python -m unittest discover -s tests -v
docker exec --user codex --interactive --tty --workdir /workspace/remote_agent_com_sys codex-ubuntu22-sandbox bash
```

対話シェルで設定ファイルを読み込んで Bot を起動します。

```bash
set -a
. /home/codex/.config/remote-agent-com/bridge.env
set +a
/home/codex/remote-agent-com-venv/bin/python -m remote_agent
```

試験中はフォアグラウンドで起動し、停止は `!codex stop` またはターミナルで `Ctrl+C` を使います。常駐サービスとして登録しないでください。

Discord では許可されたチャンネルで次の形式を使います。

```text
!codex ask 作業内容
!codex status
!codex stop
!codex new
!codex approve 承認コード
!codex deny 承認コード
!codex help
```

承認要求に表示された操作内容・作業場所を確認し、その要求コードに対して承認または拒否します。承認待ちが期限切れ、Discord 切断、通知失敗、Bot/CLI の異常終了、停止になった場合は拒否します。再起動後に要求を自動再実行しません。

## 検証と制約

自動テストの結果と、実際の Discord/Codex 接続の結果を分けて記録します。実接続試験には Discord Bot トークンと専用 Codex 認証が必要です。利用アカウントで `gpt-6-luna` を利用できる必要があり、利用不能時に別モデルへ切り替わりません。現在の確認記録は [docs/VALIDATION.md](docs/VALIDATION.md) を参照してください。

## 参考資料

- [Codex app-server](https://learn.chatgpt.com/docs/app-server)
- [discord.py クイックスタート](https://discordpy.readthedocs.io/en/stable/quickstart.html)
- [仕様](docs/SPEC.md)
- [ソフトウェア設計](docs/SOFTWARE_DESIGN.md)
- [実装計画](docs/IMPLEMENTATION_PLAN.md)
