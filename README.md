# diffview

WinMerge 互換の Linux 用 TUI diff ツール。  
ファイル比較・ディレクトリ比較・Git コミット/ブランチ比較に対応。

## 特徴

| 機能 | 説明 |
|------|------|
| ファイル diff | 2ファイルを左右並列表示で比較 |
| ディレクトリ diff | サブディレクトリを再帰的に比較し、ファイル一覧で差分を表示 |
| Git コミット diff | `HEAD~3 HEAD` や `main feature/xxx` のようなgit refを直接指定 |
| ブランチ diff | 2つのブランチを丸ごと比較 |
| Git mergetool | `git mergetool` から自動起動 |
| マージ操作 | 差分ブロックを左→右 または 右→左にコピー |

## インストール

```bash
bash install.sh
```

または手動:

```bash
cp diffview.py ~/.local/bin/diffview
chmod +x ~/.local/bin/diffview
```

## 使い方

```bash
# ファイル比較
diffview file1.py file2.py

# ディレクトリ比較
diffview src/ dst/
diffview old_version/ new_version/

# Git コミット比較
diffview HEAD~3 HEAD
diffview abc1234 def5678

# Git ブランチ比較
diffview main feature/login
diffview v1.0 v2.0

# Git ツール登録
diffview --install-git

# 読み取り専用モード（マージ不可）
diffview --readonly dir1/ dir2/
```

## Git 連携

`--install-git` を実行すると `~/.gitconfig` に自動登録されます。

```bash
diffview --install-git

# 使用例
git difftool HEAD~1          # 直前のコミットと比較
git difftool main..feature   # ブランチ間のファイル比較
git mergetool                # コンフリクト解消
```

手動で設定する場合:

```ini
[diff]
    tool = diffview
[difftool "diffview"]
    cmd = python3 "/path/to/diffview.py" "$LOCAL" "$REMOTE"
[difftool]
    prompt = false
[merge]
    tool = diffview
[mergetool "diffview"]
    cmd = python3 "/path/to/diffview.py" "$LOCAL" "$REMOTE"
    trustExitCode = false
```

## キー操作（WinMerge 互換）

### ファイル diff ビュー

| キー | 動作 |
|------|------|
| `Alt+↓` / `F8` | 次の差分ブロックへジャンプ |
| `Alt+↑` / `F7` | 前の差分ブロックへジャンプ |
| `Tab` | フォーカスを左 ↔ 右に切り替え |
| `↑` `↓` | カーソル移動 |
| `PgUp` `PgDn` | ページスクロール |
| `Home` / `End` | 先頭 / 末尾へ |
| `Alt+→` | 現在の差分ブロックを右ペインへコピー |
| `Alt+←` | 現在の差分ブロックを左ペインへコピー |
| `Ctrl+S` | フォーカス中のペインを保存 |
| `F5` | ファイルをディスクから再読み込み |
| `F1` / `?` | ヘルプ表示 |
| `Q` / `ESC` | ディレクトリビューへ戻る / 終了 |

### ディレクトリビュー

| キー | 動作 |
|------|------|
| `Enter` | 選択ファイルの diff を開く |
| `H` | 同一ファイルの表示/非表示を切り替え |
| `↑` `↓` / `PgUp` `PgDn` | 移動 |
| `Home` / `End` | 先頭 / 末尾 |
| `F5` | 更新 |
| `Q` | 終了 |

## 色の意味

| 色 | 意味 |
|----|------|
| 白 | 差分なし（同一行） |
| 黄 | 変更行（replace） |
| 赤 | 左のみに存在（delete） |
| 緑 | 右のみに存在（insert） |
| マゼンタ | バイナリファイル（ディレクトリビュー） |

## 依存関係

- Python 3.8+（標準ライブラリのみ、追加インストール不要）
- curses（Linux 標準）
- git（git 連携機能を使う場合のみ）
