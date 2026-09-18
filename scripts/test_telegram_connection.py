#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Diagnostic script to test and verify Telegram notification settings."""

import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo
import requests


def main() -> int:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    thread_id = os.getenv("TELEGRAM_MESSAGE_THREAD_ID")
    summary_file = os.getenv("GITHUB_STEP_SUMMARY")

    if not token or not chat_id:
        msg = f"Telegram 凭据未配置完整: token={'已配置' if token else '未配置'}, chat_id={'已配置' if chat_id else '未配置'}"
        print(f"❌ {msg}", file=sys.stderr)
        if summary_file:
            with open(summary_file, "a", encoding="utf-8") as f:
                f.write(f"### ❌ Telegram 诊断结果\n- {msg}\n")
        return 1

    # 1. 验证 Bot 身份
    try:
        me_res = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=10)
        me_json = me_res.json()
    except Exception as e:
        print(f"❌ 请求 Telegram API 异常: {e}", file=sys.stderr)
        return 1

    if me_res.status_code != 200 or not me_json.get("ok"):
        msg = f"Telegram Bot Token 无效: {me_res.text}"
        print(f"❌ {msg}", file=sys.stderr)
        if summary_file:
            with open(summary_file, "a", encoding="utf-8") as f:
                f.write(f"### ❌ Telegram 诊断结果\n- {msg}\n")
        return 1

    bot_data = me_json.get("result", {})
    bot_name = bot_data.get("first_name", "")
    bot_user = bot_data.get("username", "")

    # 2. 验证目标聊天
    try:
        chat_res = requests.get(
            f"https://api.telegram.org/bot{token}/getChat",
            params={"chat_id": chat_id},
            timeout=10,
        )
        chat_json = chat_res.json()
    except Exception as e:
        print(f"❌ 获取聊天信息异常: {e}", file=sys.stderr)
        chat_json = {}

    chat_data = chat_json.get("result", {}) if chat_json.get("ok") else {}
    chat_title = (
        chat_data.get("title")
        or chat_data.get("username")
        or chat_data.get("first_name")
        or "个人会话"
    )
    chat_type = chat_data.get("type", "unknown")

    # 3. 发送测试消息（使用纯文本避免下划线等特殊符号破坏 Markdown 解析）
    sh_time = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")
    test_text = (
        f"🔔 Daily Stock Analysis 机器人连通性测试\n\n"
        f"• 状态：✅ 正常连通\n"
        f"• 机器人：@{bot_user} ({bot_name})\n"
        f"• 目标：{chat_title} ({chat_type})\n"
        f"• 发送时间：{sh_time}（北京时间）"
    )
    payload = {"chat_id": chat_id, "text": test_text}
    if thread_id:
        payload["message_thread_id"] = thread_id

    send_res = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json=payload,
        timeout=10,
    )
    send_ok = send_res.status_code == 200 and send_res.json().get("ok")

    print(f"Bot: @{bot_user} ({bot_name})")
    print(f"Chat: {chat_title} (type={chat_type})")
    print(f"Send status: {'成功' if send_ok else '失败: ' + send_res.text}")

    if summary_file:
        with open(summary_file, "a", encoding="utf-8") as f:
            f.write(
                f"### 📊 Telegram 通知诊断测试结果\n\n"
                f"| 检查项 | 结果 |\n"
                f"| :--- | :--- |\n"
                f"| 🤖 Bot 账号 | @{bot_user} ({bot_name}) |\n"
                f"| 💬 目标聊天 | {chat_title} (类型: {chat_type}) |\n"
                f"| 🧵 话题 ID | {thread_id or '未设置（通用默认）'} |\n"
                f"| 🚀 发送测试 | {'✅ 成功' if send_ok else '❌ 失败: ' + send_res.text} |\n\n"
            )

    if not send_ok:
        return 1

    os.makedirs("logs", exist_ok=True)
    msg_id = send_res.json().get("result", {}).get("message_id")
    with open("logs/telegram_test.log", "w", encoding="utf-8") as f:
        f.write(
            f"Telegram 消息发送成功 (chat: {chat_title}, id: {chat_id}, msg_id: {msg_id})\n"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
