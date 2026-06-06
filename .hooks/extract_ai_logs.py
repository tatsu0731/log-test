#!/usr/bin/env python3
"""
AI アシスタントの会話ログを抽出し、CSV に記録してスプレッドシートへ差分同期する。
対応ツール: GitHub Copilot Chat / Claude CLI / OpenAI Codex

CSV列: timestamp, user_name, user_email, tool, session_title, user_prompt, ai_response, synced
synced が "false" の行だけをコミット時にスプレッドシートへ送信し、送信後に "true" に更新する。
"""

import csv
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

CSV_COLUMNS = [
    "timestamp", "user_name", "user_email",
    "tool", "session_title",
    "user_prompt", "ai_response",
    "synced",
]


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
    try:
        return int(datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp())
    except Exception:
        return 0


def safe_read_sqlite(db_path, query, params=()):
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
    parts = []
    for item in response_items:
        if "kind" not in item and "value" in item:
            parts.append(item["value"])
    return "".join(parts).strip()


def extract_copilot(repo_path, since_ts):
    results = []
    base = Path.home() / "Library/Application Support/Code/User/workspaceStorage"
    if not base.exists():
        return results

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
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "".join(
            c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"
        ).strip()
    return ""


def _is_claude_user_prompt(record):
    content = record.get("message", {}).get("content", "")
    if isinstance(content, list):
        if any("tool_use_id" in c for c in content):
            return False
    return bool(_claude_text(content))


def _find_claude_assistant_text(records, start_idx):
    texts = []
    for r in records[start_idx:]:
        if r.get("type") == "user" and _is_claude_user_prompt(r):
            break
        if r.get("type") == "assistant":
            t = _claude_text(r.get("message", {}).get("content", ""))
            if t:
                texts.append(t)
    return "\n".join(texts)


def extract_claude(repo_path, since_ts):
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

        title = "Untitled"
        for r in records:
            if r.get("type") == "ai-title":
                title = r.get("aiTitle", title)
                break

        messages = []
        for idx, r in enumerate(records):
            if r.get("type") != "user" or not _is_claude_user_prompt(r):
                continue
            ts_sec = parse_iso_ts(r.get("timestamp", ""))
            if ts_sec <= since_ts:
                continue
            user_text = _claude_text(r.get("message", {}).get("content", ""))
            ai_text = _find_claude_assistant_text(records, idx + 1)
            dt = datetime.fromtimestamp(ts_sec, tz=timezone.utc).astimezone()
            messages.append({
                "timestamp": dt.strftime("%Y-%m-%d %H:%M:%S %Z"),
                "user": user_text,
                "ai": ai_text,
            })

        if messages:
            results.append({"tool": "Claude CLI (Claude Code)", "title": title, "messages": messages})

    return results


# ─── OpenAI Codex ─────────────────────────────────────────────────────────────

def _codex_assistant_text(records, turn_id):
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
    results = []
    sessions_dir = Path.home() / ".codex" / "sessions"
    if not sessions_dir.exists():
        return results

    for session_file in sorted(sessions_dir.rglob("rollout-*.jsonl")):
        try:
            records = [json.loads(l) for l in session_file.read_text().splitlines() if l.strip()]
        except Exception:
            continue

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
            dt = datetime.fromtimestamp(ts_sec, tz=timezone.utc).astimezone()
            messages.append({
                "timestamp": dt.strftime("%Y-%m-%d %H:%M:%S %Z"),
                "user": payload.get("message", "").strip(),
                "ai": ai_text,
            })

        if messages:
            results.append({"tool": "OpenAI Codex", "title": title, "messages": messages})

    return results


# ─── CSV ──────────────────────────────────────────────────────────────────────

def read_csv(csv_file):
    if not csv_file.exists():
        return []
    with csv_file.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_csv(csv_file, rows):
    with csv_file.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, quoting=csv.QUOTE_ALL)
        writer.writeheader()
        writer.writerows(rows)


# ─── スプレッドシート送信 ─────────────────────────────────────────────────────

def load_config():
    config_file = Path(__file__).parent / "config.json"
    if not config_file.exists():
        return {}
    try:
        return json.loads(config_file.read_text())
    except Exception:
        return {}


def post_to_spreadsheet(webhook_url, rows):
    payload = json.dumps({"rows": rows}).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


# ─── エントリポイント ─────────────────────────────────────────────────────────

def main():
    repo_root, user_name, user_email, since_ts = get_git_info()

    sections = []
    sections += extract_copilot(repo_root, since_ts)
    sections += extract_claude(repo_root, since_ts)
    sections += extract_codex(repo_root, since_ts)

    log_dir = repo_root / ".ai-logs"
    log_dir.mkdir(exist_ok=True)
    safe_name = user_name.replace(" ", "_").replace("/", "_")
    csv_file = log_dir / f"{safe_name}.csv"

    # 新規行を追記
    new_rows = [
        {
            "timestamp":     msg["timestamp"],
            "user_name":     user_name,
            "user_email":    user_email,
            "tool":          section["tool"],
            "session_title": section["title"],
            "user_prompt":   msg["user"],
            "ai_response":   msg["ai"],
            "synced":        "false",
        }
        for section in sections
        for msg in section["messages"]
    ]

    if new_rows:
        existing = read_csv(csv_file)
        write_csv(csv_file, existing + new_rows)
        print(f"[ai-logs] {len(new_rows)} 件を CSV に追記しました: {csv_file.name}")
    elif not csv_file.exists():
        sys.exit(0)

    # 未同期行をスプレッドシートへ送信
    config = load_config()
    webhook_url = config.get("spreadsheet_webhook_url", "").strip()

    all_rows = read_csv(csv_file)
    unsynced = [r for r in all_rows if r.get("synced") == "false"]

    if unsynced and webhook_url:
        try:
            rows_to_send = [
                {k: r[k] for k in CSV_COLUMNS if k != "synced"}
                for r in unsynced
            ]
            result = post_to_spreadsheet(webhook_url, rows_to_send)
            for r in all_rows:
                if r.get("synced") == "false":
                    r["synced"] = "true"
            write_csv(csv_file, all_rows)
            print(f"[ai-logs] {result.get('count', len(unsynced))} 件をスプレッドシートに同期しました")
        except Exception as e:
            print(f"[ai-logs] スプレッドシート同期失敗（次回コミット時に再試行します）: {e}")
    elif unsynced and not webhook_url:
        print(f"[ai-logs] webhook 未設定のため {len(unsynced)} 件は未同期のまま保持します")

    subprocess.run(["git", "add", str(csv_file)], check=True)


if __name__ == "__main__":
    main()
