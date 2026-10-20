#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""yt_desc_fetch.py — 抓 YouTube 频道最近 N 个视频的**描述**，提取邮箱 + 社交链接。

为什么需要它（2026-10-09 实测）：很多主播把**商务邮箱和全部社交链接放在视频描述里**
（比频道 About 页更全）；YouTube 自己的「商务邮箱」按钮反而要登录+验证码。
实例：YoSoyRick 的描述里有 "Correo de contacto: Contactoyosoyrick@gmail.com"。

通道：yt-dlp --print "%(description)s"（免登录、免 API key；本机已装）
安全：限速（默认 6s/频道，可调）+ 断点续跑 + 只读（绝不登录/评论/订阅）
输出：~/scripts/.state/yt_desc.json
用法：
  python3 yt_desc_fetch.py --ids-file /tmp/yt_targets.txt --per-ch 3 --max 20
  python3 yt_desc_fetch.py --url https://www.youtube.com/@someone --per-ch 2
"""
import argparse
import json
import os
import random
import re
import subprocess
import sys
import time

STATE = os.path.expanduser("~/scripts/.state")
OUT = os.path.join(STATE, "yt_desc.json")
YTDLP = "/opt/homebrew/bin/yt-dlp"

MAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
LINK_RE = re.compile(r"https?://(?:www\.)?"
                     r"(instagram\.com|instagr\.am|tiktok\.com|x\.com|twitter\.com|facebook\.com|fb\.me|"
                     r"t\.me|telegram\.me|discord\.gg|discord\.com|linktr\.ee|beacons\.ai|youtube\.com|youtu\.be)"
                     r"/[^\s)\]\u3000\"'<>]+", re.I)
# 常见噪声邮箱（平台/工具自动加的，不是本人的）
NOISE = ("noreply", "no-reply", "@youtube.com", "@google.com", "@example.", "support@", "@yt.com")


def clean_mails(text):
    out = []
    for m in MAIL_RE.findall(text or ""):
        ml = m.lower()
        if any(n in ml for n in NOISE):
            continue
        if ml not in out:
            out.append(ml)
    return out


def links_of(text):
    out = []
    for m in LINK_RE.findall(text or ""):
        pass
    for m in re.finditer(LINK_RE, text or ""):
        u = m.group(0).rstrip(".,;")
        if u not in out:
            out.append(u)
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


def fetch_desc(url, per_ch):
    # 注意：不能用 --flat-playlist —— 它只列条目、不解析每个视频的详情，%(description)s 会是空的。
    cmd = [YTDLP, "--no-warnings", "--skip-download",
           "--playlist-items", "1-%d" % per_ch, "--print", "%(description)s", url]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        return r.stdout or ""
    except Exception as e:
        return "__ERR__%s" % type(e).__name__


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids-file", default="", help="每行一个 channel_id / 频道 URL")
    ap.add_argument("--url", default="", help="单个频道 URL")
    ap.add_argument("--per-ch", type=int, default=3, help="每频道取最近几个视频的描述")
    ap.add_argument("--max", type=int, default=20)
    ap.add_argument("--sleep", type=float, default=6.0)
    a = ap.parse_args()

    targets = []
    if a.url:
        targets.append(a.url)
    if a.ids_file:
        p = os.path.expanduser(a.ids_file)
        if os.path.exists(p):
            for l in open(p, encoding="utf-8"):
                l = l.strip()
                if not l:
                    continue
                targets.append(l if l.startswith("http") else "https://www.youtube.com/channel/" + l)
    st = load_state()
    # 先过滤已缓存、再截断 --max（反了会让清单前 N 个永远占满名额，后面的轮不到）
    todo = [t for t in targets if t not in st][: a.max]
    print("待抓 %d 个（已缓存 %d，清单共 %d）" % (len(todo), len(st), len(targets)), flush=True)
    if not todo:
        return 0

    hit_mail = 0
    for t in todo:
        txt = fetch_desc(t, a.per_ch)
        if txt.startswith("__ERR__"):
            print("⚠️ %s %s" % (t, txt), file=sys.stderr, flush=True)
            # 失败也落缓存，避免每轮从同一处重试（死循环）
            st[t] = {"error": txt, "mails": [], "links": [], "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
            save_state(st)
            time.sleep(a.sleep)
            continue
        mails = clean_mails(txt)
        links = links_of(txt)
        st[t] = {"mails": mails, "links": links, "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
        if mails:
            hit_mail += 1
        print("✅ %-58s mails=%s links=%d" % (t[-58:], mails[:2] or "-", len(links)), flush=True)
        save_state(st)
        time.sleep(a.sleep + random.uniform(0, 3))

    save_state(st)
    print("完成：本轮 %d 个，其中拿到邮箱 %d；累计 %d → %s" % (len(todo), hit_mail, len(st), OUT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
