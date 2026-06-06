#!/usr/bin/env python3
"""
AI アシスタントの会話ログを自動抽出するスクリプト。
対応ツール: GitHub Copilot Chat / Claude CLI / OpenAI Codex
"""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


# ─── Git情報の取得 ────────────────────────────────────────────────────────────

def get_git_info():
    repo_root = Path(subprocess.check_output(
        ["git", "rev-parse", "--show-toplevel"], text=True
    ).strip())

    try:
        user_name = subprocess.check_output(["git", "config", "user.name"], text=True).strip()
        user_email = subprocess.check_output(["git", "config", "user.email"], text=True).strip()
    except Exception:
        user_name = "unknown"
        user_email = ""

    try:
        last_ts_str = subprocess.check_output(
            ["git", "log", "-1", "--format=%ct"], text=True
        ).strip()
        since_ts = int(last_ts_str) if last_ts_str else None
    except Exception:
        since_ts = None

    if since_ts is None:
        since_ts = int(datetime.now(timezone.utc).timestamp()) - 86400

    return repo_root, user_name, user_email, since_ts


# ─── 共通ユーティリティ ───────────────────────────────────────────────────────

def parse_iso_ts(ts_str):
    """ISO 8601文字列をUnixタイムスタンプ(秒)に変換する。"""
    try:
        return int(datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp())
    except Exception:
        return 0


def safe_read_sqlite(db_path, query, params=()):
    """SQLiteをread-onlyでコピーしてから読む（VS Code起動中ロック対策）。"""
    with tempfile.NamedTemporaryFile(suffix=".vscdb", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        shutil.copy2(db_path, tmp_path)
        conn = sqlite3.connect(tmp_path)
        cur = conn.execute(query, params)
        rows = cur.fetchall()
        conn.close()
        return rows
    except Exception:
        return []
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass


# ─── GitHub Copilot Chat ──────────────────────────────────────────────────────

def _copilot_response_text(response_items):
    """Copilot応答からMarkdownテキストを抽出する。"""
    parts = []
    for item in response_items:
        # kindフィールドがないアイテムがMarkdownテキスト本文
        if "kind" not in item and "value" in item:
            parts.append(item["value"])
    return "".join(parts).strip()


def extract_copilot(repo_path, since_ts):
    """GitHub Copilot Chat のセッションを抽出する。"""
    results = []
    base = Path.home() / "Library/Application Support/Code/User/workspaceStorage"
    if not base.exists():
        return results

    # リポジトリに対応するワークスペースストレージを探す
    workspace_dir = None
    for d in base.iterdir():
        wf = d / "workspace.json"
        if not wf.exists():
            continue
        try:
            folder = json.loads(wf.read_text()).get("folder", "")
            if folder.replace("file://", "") == str(repo_path):
                workspace_dir = d
                break
        except Exception:
            continue

    if not workspace_dir:
        return results

    chat_dir = workspace_dir / "chatSessions"
    if not chat_dir.exists():
        return results

    for session_file in sorted(chat_dir.glob("*.json")):
        try:
            data = json.loads(session_file.read_text())
        except Exception:
            continue

        messages = []
        for req in data.get("requests", []):
            # msec → sec に変換
            ts = req.get("timestamp", 0)
            ts_sec = ts / 1000 if ts > 1e10 else ts
            if ts_sec <= since_ts:
                continue

            user_text = req.get("message", {}).get("text", "").strip()
            ai_text = _copilot_response_text(req.get("response", []))
            dt = datetime.fromtimestamp(ts_sec, tz=timezone.utc).astimezone()
            messages.append({
                "timestamp": dt.strftime("%Y-%m-%d %H:%M:%S %Z"),
                "user": user_text,
                "ai": ai_text,
            })

        if messages:
            results.append({
                "tool": "GitHub Copilot Chat",
                "title": data.get("customTitle") or "Untitled",
                "messages": messages,
            })

    return results


# ─── Claude CLI (Claude Code) ─────────────────────────────────────────────────

def _claude_text(content):
    """Claude応答のcontentからテキストを抽出する。"""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                parts.append(c.get("text", ""))
        return "".join(parts).strip()
    return ""


def _is_claude_user_prompt(record):
    """Claudeのuserレコードが実際のユーザー入力かどうか判定する。
    tool_result（ツール実行結果）のレコードは除外する。"""
    content = record.get("message", {}).get("content", "")
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        # tool_use_idを持つアイテムはtool_result → 除外
        for c in content:
            if "tool_use_id" in c:
                return False
        # テキストが抽出できれば実ユーザー入力
        return bool(_claude_text(content))
    return False


def _find_claude_assistant_text(records, start_idx):
    """start_idx以降の最初のテキストを持つassistantレコードからテキストを収集する。"""
    texts = []
    for r in records[start_idx:]:
        rtype = r.get("type")
        if rtype == "user" and _is_claude_user_prompt(r):
            break  # 次のユーザー入力に到達したら終了
        if rtype == "assistant":
            t = _claude_text(r.get("message", {}).get("content", ""))
            if t:
                texts.append(t)
    return "\n".join(texts)


def extract_claude(repo_path, since_ts):
    """Claude CLI のセッションを抽出する。"""
    results = []
    project_hash = str(repo_path).replace("/", "-")
    project_dir = Path.home() / ".claude" / "projects" / project_hash

    if not project_dir.exists():
        return results

    for session_file in sorted(project_dir.glob("*.jsonl")):
        try:
            records = [json.loads(l) for l in session_file.read_text().splitlines() if l.strip()]
        except Exception:
            continue

        # ai-title を取得
        title = "Untitled Session"
        for r in records:
            if r.get("type") == "ai-title":
                title = r.get("aiTitle", title)
                break

        # 実ユーザー入力のみを抽出してペアを作る
        messages = []
        for idx, r in enumerate(records):
            if r.get("type") != "user":
                continue
            if not _is_claude_user_prompt(r):
                continue

            ts_sec = parse_iso_ts(r.get("timestamp", ""))
            if ts_sec <= since_ts:
                continue

            user_text = _claude_text(r.get("message", {}).get("content", ""))
            ai_text = _find_claude_assistant_text(records, idx + 1)
            messages.append({
                "timestamp": datetime.fromtimestamp(ts_sec, tz=timezone.utc)
                    .astimezone().strftime("%Y-%m-%d %H:%M:%S %Z"),
                "user": user_text,
                "ai": ai_text,
            })

        if messages:
            results.append({
                "tool": "Claude CLI (Claude Code)",
                "title": title,
                "messages": messages,
            })

    return results


# ─── OpenAI Codex ─────────────────────────────────────────────────────────────

def _codex_assistant_text(records, turn_id):
    """指定turn_idのCodexアシスタント応答テキストを収集する。"""
    parts = []
    in_turn = False
    for r in records:
        if r.get("type") == "event_msg":
            payload = r.get("payload", {})
            if payload.get("type") == "task_started" and payload.get("turn_id") == turn_id:
                in_turn = True
            elif payload.get("type") == "task_complete" and payload.get("turn_id") == turn_id:
                break
        if in_turn and r.get("type") == "response_item":
            payload = r.get("payload", {})
            if payload.get("role") == "assistant":
                for c in payload.get("content", []):
                    if c.get("type") == "output_text":
                        parts.append(c.get("text", ""))
    return "".join(parts).strip()


def extract_codex(repo_path, since_ts):
    """OpenAI Codex のセッションを抽出する。"""
    results = []
    sessions_dir = Path.home() / ".codex" / "sessions"
    if not sessions_dir.exists():
        return results

    for session_file in sorted(sessions_dir.rglob("rollout-*.jsonl")):
        try:
            records = [json.loads(l) for l in session_file.read_text().splitlines() if l.strip()]
        except Exception:
            continue

        # cwd でリポジトリを確認
        session_cwd = None
        title = session_file.stem
        for r in records:
            if r.get("type") == "session_meta":
                payload = r.get("payload", {})
                session_cwd = payload.get("cwd", "")
                title = payload.get("thread_name") or title
                break

        if session_cwd != str(repo_path):
            continue

        # user_message イベントを収集
        messages = []
        for r in records:
            if r.get("type") != "event_msg":
                continue
            payload = r.get("payload", {})
            if payload.get("type") != "user_message":
                continue

            ts_sec = parse_iso_ts(r.get("timestamp", ""))
            if ts_sec <= since_ts:
                continue

            turn_id = payload.get("turn_id", "")
            ai_text = _codex_assistant_text(records, turn_id)
            messages.append({
                "timestamp": datetime.fromtimestamp(ts_sec, tz=timezone.utc)
                    .astimezone().strftime("%Y-%m-%d %H:%M:%S %Z"),
                "user": payload.get("message", "").strip(),
                "ai": ai_text,
            })

        if messages:
            results.append({
                "tool": "OpenAI Codex",
                "title": title,
                "messages": messages,
            })

    return results


# ─── Markdownフォーマット ─────────────────────────────────────────────────────

def format_markdown(repo_path, user_name, user_email, commit_date, sections):
    lines = [
        f"# AI アシスタント ログ",
        f"",
        f"- **Author:** {user_name} <{user_email}>",
        f"- **Commit Date:** {commit_date}",
        f"- **Repo:** {repo_path}",
        f"",
        "---",
        "",
    ]

    for section in sections:
        lines.append(f"## {section['tool']}")
        lines.append("")
        lines.append(f"### {section['title']}")
        lines.append("")

        for msg in section["messages"]:
            lines.append(f"**[User]** _{msg['timestamp']}_")
            lines.append("")
            lines.append(msg["user"] or "_（テキストなし）_")
            lines.append("")
            if msg["ai"]:
                lines.append("**[AI]**")
                lines.append("")
                lines.append(msg["ai"])
                lines.append("")
            lines.append("---")
            lines.append("")

    return "\n".join(lines)


# ─── エントリポイント ─────────────────────────────────────────────────────────

def main():
    repo_root, user_name, user_email, since_ts = get_git_info()

    sections = []
    sections += extract_copilot(repo_root, since_ts)
    sections += extract_claude(repo_root, since_ts)
    sections += extract_codex(repo_root, since_ts)

    if not sections:
        print("[ai-logs] 新しいAI会話ログは見つかりませんでした。")
        sys.exit(0)

    commit_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    safe_name = user_name.replace(" ", "_").replace("/", "_")
    date_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_dir = repo_root / ".ai-logs"
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / f"{date_str}_{safe_name}.md"

    content = format_markdown(repo_root, user_name, user_email, commit_date, sections)
    log_file.write_text(content, encoding="utf-8")

    # ステージに追加
    subprocess.run(["git", "add", str(log_file)], check=True)
    print(f"[ai-logs] ログを保存しました: {log_file.name}")

    total_msgs = sum(len(s["messages"]) for s in sections)
    print(f"[ai-logs] 合計 {total_msgs} メッセージ ({len(sections)} セッション) を記録")


if __name__ == "__main__":
    main()
