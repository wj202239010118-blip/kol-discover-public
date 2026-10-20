#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""lark_notify.py — Lark 自定义机器人 v2 webhook 发送（内部通知，中文可用）

2026-10-09：给云端自检/日报用。webhook 从 env LARK_WEBHOOK 读（GitHub Secrets）。
注意：自定义机器人发出去的消息**撤不回** —— 所以只用于「内部通知群」，且内容必须准确。
"""
import json
import os
import sys
import urllib.request

DEFAULT_WEBHOOK = ""  # 不写死；一律走 env


def send(text, webhook=None):
    """往 Lark 群里发一条纯文本。返回 True/False。"""
    url = webhook or os.environ.get("LARK_WEBHOOK", "") or DEFAULT_WEBHOOK
    if not url:
        print("[lark] 未配置 LARK_WEBHOOK，跳过发送", file=sys.stderr)
        return False
    body = json.dumps({"msg_type": "text", "content": {"text": text}}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            resp = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        print("[lark] 发送失败: %s" % e, file=sys.stderr)
        return False
    ok = resp.get("code") == 0 or resp.get("StatusCode") == 0 or resp.get("StatusMessage") == "success"
    print("[lark] 发送结果: %s" % json.dumps(resp, ensure_ascii=False))
    return bool(ok)


if __name__ == "__main__":
    txt = sys.argv[1] if len(sys.argv) > 1 else "test"
    print("ok" if send(txt) else "failed")
