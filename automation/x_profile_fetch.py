#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""x_profile_fetch.py — 用 fxTwitter 公开 API 抓 X(Twitter) 用户资料（bio 里常放商务邮箱）。

通道：https://api.fxtwitter.com/<screen_name>
  · 免登录、免 API key、免 cookie（**不需要 auth_token/ct0**）
  · 实测 2026-10-09 可用；vxTwitter 同款被 Cloudflare 挡（Just a moment...），fxTwitter 通
  · 返回 user.description（bio）/ followers / location 等
安全：限速（默认 3s/个 + 抖动）+ 断点续跑 + 只读
输出：~/scripts/.state/x_profile.json
用法：
  python3 x_profile_fetch.py --users theburntpeanut --max 2
  python3 x_profile_fetch.py --users-file /tmp/x_targets.txt --max 100
"""
import argparse
import json
import os
import random
import re
import sys
import time
import urllib.request

STATE = os.path.expanduser("~/scripts/.state")
OUT = os.path.join(STATE, "x_profile.json")
API = "https://api.fxtwitter.com/%s"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
MAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
NOISE = ("noreply", "no-reply", "@youtube.com", "@google.com", "@example.", "@twitter.com", "@x.com")
# 2026-10-09: bio 里常带其它平台链接 → 一并提取，能顺带补表的 IG/TikTok/FB 列
LINK_RE = re.compile(r"https?://(?:www\.)?"
                     r"(instagram\.com|instagr\.am|tiktok\.com|x\.com|twitter\.com|facebook\.com|fb\.me|"
                     r"t\.me|telegram\.me|discord\.gg|discord\.com|linktr\.ee|beacons\.ai|youtube\.com|youtu\.be|twitch\.tv)"
                     r"/[^\s)\]\u3000\"'<>]+", re.I)


def clean_mails(t):
    out = []
    for m in MAIL_RE.findall(t or ""):
        ml = m.lower()
        if any(n in ml for n in NOISE):
            continue
        if ml not in out:
            out.append(ml)
    return out


def load_state():
    try:
        return json.load(open(OUT, encoding="utf-8"))
    except Exception:
        return {}


def save_state(d):
    os.makedirs(STATE, exist_ok=True)
    tmp = OUT + ".tmp"
    json.dump(d, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    os.replace(tmp, OUT)


def fetch(user):
    req = urllib.request.Request(API % user, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read().decode("utf-8", "ignore") or "{}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--users", default="")
    ap.add_argument("--users-file", default="")
    ap.add_argument("--max", type=int, default=20)
    ap.add_argument("--sleep", type=float, default=3.0)
    a = ap.parse_args()

    users = [x.strip().lstrip("@") for x in (a.users.split(",") if a.users else []) if x.strip()]
    if a.users_file:
        p = os.path.expanduser(a.users_file)
        if os.path.exists(p):
            users += [l.strip().lstrip("@") for l in open(p, encoding="utf-8") if l.strip()]
    seen, todo = set(), []
    for u in users:
        if u.lower() not in seen:
            seen.add(u.lower())
            todo.append(u)
    st = load_state()
    # 2026-10-09 事故：必须**先过滤已缓存、再截断 --max**。原顺序（先截断后过滤）会让清单前 400 个
    # 永远占满名额，后面的 2000+ 一个也轮不到（表现为每轮都"待抓 0 个"）。
    todo = [u for u in todo if u.lower() not in {k.lower() for k in st}][: a.max]
    print("待抓 %d 个（已缓存 %d，清单共 %d）" % (len(todo), len(st), len(users)), flush=True)
    if not todo:
        return 0

    hit = 0
    fail_streak = 0
    for u in todo:
        try:
            d = fetch(u)
            us = (d or {}).get("user") or {}
            if not us:
                fail_streak += 1
                print("⚠️ %-22s 无 user 字段（code=%s）连续=%d" % (u, (d or {}).get("code"), fail_streak),
                      file=sys.stderr, flush=True)
                if fail_streak >= 8:
                    print("🚨 连续 %d 次无 user（疑似 fxTwitter 限流）→ 熔断退出，已保存进度", fail_streak, flush=True)
                    save_state(st)
                    return 6
                time.sleep(a.sleep * 3)     # 失败后加倍等待，别硬刚
                continue
            fail_streak = 0
            # 2026-10-09 优化：把 raw_description（原始未转义 bio）也纳入提取 —— 有的账号
            # 邮箱/链接只出现在原始文本里；website 字段（profile 网站）同样常放商务联系方式。
            desc = us.get("description") or ""
            rawd = us.get("raw_description") or ""
            # fxTwitter 的字段类型不固定（实测 raw_description 有时是 dict）→ 统一成字符串，
            # 否则 "str + dict" 直接抛异常、整轮 0 产出（2026-10-09 踩过）。
            if not isinstance(desc, str):
                desc = json.dumps(desc, ensure_ascii=False) if isinstance(desc, (dict, list)) else str(desc)
            if not isinstance(rawd, str):
                rawd = json.dumps(rawd, ensure_ascii=False) if isinstance(rawd, (dict, list)) else str(rawd)
            blob = desc + "\n" + rawd
            mails = clean_mails(blob)
            links = []
            for mm in re.finditer(LINK_RE, blob):
                uu = mm.group(0).rstrip(".,;")
                if uu not in links:
                    links.append(uu)
            wb = us.get("website")
            if isinstance(wb, dict):
                wb = wb.get("url") or wb.get("display_url") or ""
            if wb and str(wb) not in links:
                links.append(str(wb))
            st[u.lower()] = {
                "screen_name": us.get("screen_name") or u,
                "name": us.get("name") or "",
                "followers": us.get("followers"),
                "location": us.get("location") or "",
                "description": desc,
                "mails": mails,
                "links": links,
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            if mails:
                hit += 1
            print("✅ %-22s fans=%-8s mail=%s" % (u, us.get("followers"), mails[:2] or "-"), flush=True)
        except Exception as e:
            msg = str(e)[:90]
            print("⚠️ %-22s %s" % (u, msg), file=sys.stderr, flush=True)
            # 2026-10-09 事故：失败也必须落缓存 —— 否则清单里的 404/不存在账号会让每轮都从同一处重试
            # （实测 7 轮卡在同样的两个 404 handle 上，累计数纹丝不动）。
            st[u.lower()] = {"error": msg, "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
        save_state(st)
        time.sleep(a.sleep + random.uniform(0, 1.5))

    save_state(st)
    print("完成：本轮 %d 个，拿到邮箱 %d；累计 %d → %s" % (len(todo), hit, len(st), OUT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
