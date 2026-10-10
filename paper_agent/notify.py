"""把每日快報與每週報告推播出去：Email、LINE、Webhook（Slack／Discord／Google Chat）、Notion。

設定在 settings.yaml 的 notify 區塊；金鑰一律放環境變數，不寫進設定檔：
- Email：SMTP_HOST、SMTP_PORT（預設 587）、SMTP_USER、SMTP_PASSWORD、SMTP_FROM（選填）
- LINE（Messaging API；LINE Notify 已於 2025 年停止服務）：LINE_CHANNEL_TOKEN、LINE_USER_ID
- Webhook：NOTIFY_WEBHOOK_URL
- Notion：NOTION_TOKEN（並把 integration 加入目標資料庫的連線）
推播失敗只記錄在 log，不影響其他管道，也不會讓排程失敗。
"""

from __future__ import annotations

import logging
import os
import smtplib
from email.message import EmailMessage

import requests

log = logging.getLogger(__name__)
LINE_LIMIT = 5000      # LINE 單則文字上限
DISCORD_LIMIT = 2000   # Discord 單則訊息上限
NOTION_TEXT_LIMIT = 2000
NOTION_BLOCK_LIMIT = 100


def summary(title: str, items: list[dict], warning: bool = False) -> str:
    """短版內容（給 LINE、聊天軟體）：每篇一行標題＋一句話摘要。"""
    lines = [f"📚 {title}"]
    if warning:
        lines.append("⚠ AI 服務連續失敗，本次只處理了部分論文，詳見快報。")
    lines.append(f"共 {len(items)} 篇高相關論文" if items else "今天沒有新的高相關論文。")
    for i, it in enumerate(items, start=1):
        venue = f"（{it['venue']}）" if it.get("venue") else ""
        lines.append(f"\n{i}. {it['title']}{venue} 相關性 {it['relevance']}/5\n   {it['tldr']}\n   {it['url']}")
    return "\n".join(lines)


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 20].rstrip() + "\n…（完整內容見快報）"


def send_email(cfg: dict, title: str, body: str) -> None:
    to = cfg.get("to") or []
    host, user = os.environ.get("SMTP_HOST"), os.environ.get("SMTP_USER")
    if not (to and host and user and os.environ.get("SMTP_PASSWORD")):
        raise RuntimeError("缺少 notify.email.to 或 SMTP_HOST／SMTP_USER／SMTP_PASSWORD")
    msg = EmailMessage()
    msg["Subject"] = title
    msg["From"] = os.environ.get("SMTP_FROM", user)
    msg["To"] = ", ".join(to)
    msg.set_content(body)
    port = int(os.environ.get("SMTP_PORT") or "587")  # GitHub 未設定的 secret 會是空字串
    with (smtplib.SMTP_SSL(host, port, timeout=30) if port == 465 else smtplib.SMTP(host, port, timeout=30)) as s:
        if port != 465:
            s.starttls()
        s.login(user, os.environ["SMTP_PASSWORD"])
        s.send_message(msg)


def send_line(cfg: dict, text: str) -> None:
    token, user_id = os.environ.get("LINE_CHANNEL_TOKEN"), os.environ.get("LINE_USER_ID")
    if not (token and user_id):
        raise RuntimeError("缺少 LINE_CHANNEL_TOKEN 或 LINE_USER_ID")
    resp = requests.post("https://api.line.me/v2/bot/message/push", timeout=30,
                         headers={"Authorization": f"Bearer {token}"},
                         json={"to": user_id, "messages": [{"type": "text", "text": _truncate(text, LINE_LIMIT)}]})
    resp.raise_for_status()


def send_webhook(cfg: dict, text: str) -> None:
    url = os.environ.get("NOTIFY_WEBHOOK_URL")
    if not url:
        raise RuntimeError("缺少 NOTIFY_WEBHOOK_URL")
    if cfg.get("kind", "slack") == "discord":
        payload = {"content": _truncate(text, DISCORD_LIMIT)}
    else:  # Slack、Google Chat、Mattermost 都接受 {"text": ...}
        payload = {"text": text}
    requests.post(url, json=payload, timeout=30).raise_for_status()


def _notion_blocks(markdown: str) -> list[dict]:
    blocks = []
    for line in markdown.splitlines():
        line = line.strip()
        if not line or line == "---":
            continue
        kind, text = "paragraph", line
        if line.startswith("## ") or line.startswith("### "):
            kind, text = "heading_3" if line.startswith("### ") else "heading_2", line.lstrip("#").strip()
        elif line.startswith("# "):
            continue  # 標題已是頁面名稱
        elif line.startswith(("- ", "* ")):
            kind, text = "bulleted_list_item", line[2:]
        rich = [{"type": "text", "text": {"content": text[i:i + NOTION_TEXT_LIMIT]}}
                for i in range(0, len(text), NOTION_TEXT_LIMIT)]
        blocks.append({"object": "block", "type": kind, kind: {"rich_text": rich}})
    return blocks


def send_notion(cfg: dict, title: str, markdown: str) -> None:
    token, database = os.environ.get("NOTION_TOKEN"), cfg.get("database_id")
    if not (token and database):
        raise RuntimeError("缺少 NOTION_TOKEN 或 notify.notion.database_id")
    headers = {"Authorization": f"Bearer {token}", "Notion-Version": "2022-06-28"}
    blocks = _notion_blocks(markdown)
    resp = requests.post("https://api.notion.com/v1/pages", headers=headers, timeout=30, json={
        "parent": {"database_id": database},
        "properties": {cfg.get("title_property", "Name"): {"title": [{"text": {"content": title}}]}},
        "children": blocks[:NOTION_BLOCK_LIMIT],
    })
    resp.raise_for_status()
    page_id = resp.json()["id"]
    for i in range(NOTION_BLOCK_LIMIT, len(blocks), NOTION_BLOCK_LIMIT):  # 每次最多附加 100 個區塊
        requests.patch(f"https://api.notion.com/v1/blocks/{page_id}/children", headers=headers, timeout=30,
                       json={"children": blocks[i:i + NOTION_BLOCK_LIMIT]}).raise_for_status()


def send(cfg: dict, title: str, markdown: str, items: list[dict], warning: bool = False) -> list[str]:
    """依設定推播，回傳成功送出的管道名稱。"""
    ncfg = cfg.get("notify", {})
    if not items and not warning and not ncfg.get("on_empty", False):
        return []
    short = summary(title, items, warning)
    channels = {
        "email": lambda c: send_email(c, title, markdown),
        "line": lambda c: send_line(c, short),
        "webhook": lambda c: send_webhook(c, short),
        "notion": lambda c: send_notion(c, title, markdown),
    }
    sent = []
    for name, fn in channels.items():
        ccfg = ncfg.get(name, {})
        if not ccfg.get("enabled"):
            continue
        try:
            fn(ccfg)
            sent.append(name)
        except Exception as e:
            log.warning("推播失敗（%s）：%s", name, e)
    if sent:
        log.info("已推播：%s", "、".join(sent))
    return sent
