#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""contact_enrich.py — 批量挖联系方式（多路合并）

为什么要有它（2026-10-20 用户）
  单路能力早就有，但散在三个脚本里、各跑各的、结果不汇总、不落表。
  真正缺的是**编排 + 批量 + 落表**。

借鉴 nanoinfluencer.ai 的 Email Match 三路设计
  ① 媒体平台 bio     平台 API 的社交字段 + 简介文本正则
  ② link-in-bio 中间页   Linktree / beacons 这类聚合页（kol_identity 已有）
  ③ 社交主页          X 抓 bio（fxTwitter 免登录）/ YouTube 抓视频描述（比 About 页更全）

复用（不重写）
  kol_identity.expand()    —— 文本 → 各平台 handle + 抓 link-in-bio
  x_profile_fetch.fetch() —— fxTwitter 拿 bio
  yt_desc_fetch.fetch_desc() —— yt-dlp 拿视频描述
  MAIL_RE / NOISE          —— 与上面两个脚本一致的邮箱正则和噪声表

用法
  python3 contact_enrich.py --limit 20                    # 试跑 20 个（从候选池挑观众最高的）
  python3 contact_enrich.py --slug recerena --explain     # 单个，逐路显示
  python3 contact_enrich.py --limit 100 --apply           # 写回「联系筛选」表
  python3 contact_enrich.py --no-fetch                    # 只用平台 bio，不抓外部页（最快）

设计原则
  · 只读抓取；--apply 才写表
  · 每路之间 sleep，不并发打同一站点
  · 结果缓存 7 天（.state/contact_enrich_cache.json），重复跑不重复抓
  · 拿不到就写空，不编造、不猜
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import kol_identity
import x_profile_fetch
import yt_desc_fetch

CAND_FILE = os.environ.get("DISCOVER_CAND_FILE") or os.path.join(HERE, "discover-candidates.json")
CACHE_FILE = os.environ.get("CONTACT_CACHE_FILE") or os.path.join(HERE, ".state", "contact_enrich_cache.json")
# 已合作库快照（503 条，字段 puid/slug/stage/uid）——**有 slug**，能跟候选池对上。
#   coop_members.json 全是 TG 数字 uid，用不了（2026-10-20 实证）。
COOP_SNAPSHOT = (os.environ.get("COOP_SNAPSHOT_FILE")
                 or os.path.join(HERE, ".state", "coop_master_brief_snapshot.json"))
CACHE_DAYS = 7

# 邮箱：与 x_profile_fetch / yt_desc_fetch 保持同一套
MAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
NOISE = ("noreply", "no-reply", "@youtube.com", "@google.com", "@example.",
         "@twitter.com", "@x.com", "support@", "@yt.com", "@instagram.com")

# 商务邮箱优先级：命中这些词排前面（我们只认商务用途）
BIZ_HINTS = ("business", "biz", "contact", "contacto", "comercial", "pr",
             "partnership", "collab", "colab", "info", "hola", "negocios")

# 2026-10-20 修：原来 `@` 分支会把邮箱里的 @gmail 抓成 TG handle。
#   现在要求 @ 前面不是邮箱字符（负向后视），或走 t.me / telegram.me 显式链接。
TG_RE = re.compile(r"(?:t\.me/|telegram\.me/)([A-Za-z0-9_]{4,32})|(?<![A-Za-z0-9._%+-])@([A-Za-z0-9_]{4,32})\b")
WA_RE = re.compile(r"(?:wa\.me/|chat\.whatsapp\.com/|whatsapp\.com/send\?phone=)(\+?\d{7,15})")
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36")


def split_key(key: str, meta: dict) -> tuple[str, str]:
    """候选池的 key 混两种格式：纯 slug（kurtismatt）/ platform:slug（kick:xxx）。
    2026-10-20 实测 27364 条里 11412 条带冒号 —— 不解析就会拿 'kick:xxx' 去请求 → 404。"""
    k = str(key or "")
    if ":" in k:
        p, _, sl = k.partition(":")
        if p in ("kick", "twitch", "youtube") and sl:
            return p, sl
    return str(meta.get("platform") or "kick"), k


def clean_mails(text: str) -> list[str]:
    out = []
    for m in MAIL_RE.findall(text or ""):
        ml = m.lower().strip(".")
        if any(n in ml for n in NOISE):
            continue
        if ml not in out:
            out.append(ml)
    # 商务邮箱优先
    out.sort(key=lambda x: 0 if any(h in x for h in BIZ_HINTS) else 1)
    return out


def load_coop() -> set:
    """已合作 slug 集合 —— 这些人的联系方式不用再挖（已经合作了，重复劳动）。"""
    out: set[str] = set()
    try:
        snap = json.load(open(COOP_SNAPSHOT))
        for it in (snap.get("items") or []):
            v = str(it.get("slug") or "").strip().lower()
            if v and not v.isdigit():
                out.add(v)
    except Exception as e:
        print(f"[warn] 读不到合作库快照（{str(e)[:50]}）—— 不排除已合作的人", file=sys.stderr)
    return out


def load_cache() -> dict:
    try:
        return json.load(open(CACHE_FILE))
    except Exception:
        return {}


def save_cache(d: dict):
    os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
    json.dump(d, open(CACHE_FILE, "w"), ensure_ascii=False)


def _fresh(rec: dict) -> bool:
    try:
        t = datetime.strptime(rec.get("at", ""), "%Y-%m-%d %H:%M")
        return (datetime.now() - t).days < CACHE_DAYS
    except Exception:
        return False


def platform_bio(slug: str, platform: str) -> str:
    """平台官方的简介文本（Kick/Twitch）。取不到返回空串。"""
    try:
        if platform == "kick":
            import urllib.request
            req = urllib.request.Request(f"https://kick.com/api/v1/channels/{slug}",
                                         headers={"User-Agent": _UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=20) as r:
                d = json.loads(r.read().decode())
            u = d.get("user") or {}
            parts = [str(d.get("bio") or ""), str(u.get("bio") or "")]
            for k in ("instagram", "twitter", "youtube", "discord", "tiktok", "facebook"):
                if u.get(k):
                    parts.append(str(u[k]))
            return "\n".join(p for p in parts if p)
        if platform == "twitch":
            import urllib.request
            q = json.dumps({"query": 'query($l:String!){user(login:$l){description}}',
                            "variables": {"l": slug}}).encode()
            req = urllib.request.Request("https://gql.twitch.tv/gql", data=q, headers={
                "Client-ID": "kimne78kx3ncx6brgo4mv6wki5h1ko",
                "Content-Type": "application/json", "User-Agent": _UA})
            with urllib.request.urlopen(req, timeout=20) as r:
                d = json.loads(r.read().decode())
            return str(((d.get("data") or {}).get("user") or {}).get("description") or "")
    except Exception as e:
        print(f"    [warn] 取 bio 失败: {str(e)[:50]}", file=sys.stderr)
    return ""


def enrich_one(slug: str, platform: str, do_fetch: bool = True, gap: float = 1.5) -> dict:
    """跑完所有路，返回 {emails, tg, wa, socials, sources}。"""
    res = {"slug": slug, "platform": platform, "emails": [], "tg": [], "wa": [],
           "socials": {}, "sources": [], "at": datetime.now().strftime("%Y-%m-%d %H:%M")}

    # ① 平台 bio
    bio = platform_bio(slug, platform)
    if bio:
        res["sources"].append("platform-bio")
        res["emails"] += clean_mails(bio)
        # 2026-10-20 修：原来排除「与 slug 同名」的 handle —— 但主播的 TG 常常就是他的
        #   平台名（@koasvo / @Nepasta4），排掉等于把最可能的正确联系方式扔了。
        res["tg"] += [_tg for _tg in (next((g for g in m if g), "") for m in TG_RE.findall(bio))
                      if _tg]
        res["wa"] += WA_RE.findall(bio)

    # ② link-in-bio 中间页 + 各平台 handle（kol_identity 自带抓取）
    if bio:
        try:
            exp = kol_identity.expand(bio, do_fetch=do_fetch, gap=gap)
            res["socials"].update({k: v for k, v in exp.items() if v})
            if exp:
                res["sources"].append("link-in-bio" if do_fetch else "bio-links")
        except Exception as e:
            print(f"    [warn] identity 失败: {str(e)[:50]}", file=sys.stderr)

    # ③ X bio（有 twitter handle 才抓）
    x_h = (res["socials"].get("twitter") or res["socials"].get("x") or [])
    if x_h and do_fetch:
        try:
            j = x_profile_fetch.fetch(x_h[0].lstrip("@"))
            desc = ((j.get("user") or {}).get("description") or "")
            if desc:
                res["sources"].append("x-bio")
                res["emails"] += clean_mails(desc)
                res["tg"] += [_tg for _tg in (next((g for g in m if g), "") for m in TG_RE.findall(desc))
                          if _tg and _tg not in res["tg"]]
                res["wa"] += [m for m in WA_RE.findall(desc) if m not in res["wa"]]
        except Exception as e:
            print(f"    [warn] X 失败: {str(e)[:50]}", file=sys.stderr)
        time.sleep(gap)

    # ④ YouTube 视频描述（很多主播把商务邮箱放这里，比 About 更全）
    yt = res["socials"].get("youtube") or []
    yt_url = yt[0] if yt and yt[0].startswith("http") else (
        f"https://www.youtube.com/@{yt[0].lstrip('@')}/videos" if yt else "")
    if yt_url and do_fetch:
        try:
            desc = yt_desc_fetch.fetch_desc(yt_url, 3)
            if desc and not desc.startswith("__ERR__"):
                res["sources"].append("yt-desc")
                res["emails"] += clean_mails(desc)
                res["tg"] += [_tg for _tg in (next((g for g in m if g), "") for m in TG_RE.findall(desc))
                          if _tg and _tg not in res["tg"]]
                res["wa"] += [m for m in WA_RE.findall(desc) if m not in res["wa"]]
        except Exception as e:
            print(f"    [warn] YT 失败: {str(e)[:50]}", file=sys.stderr)

    # 去重 + 商务优先
    res["emails"] = list(dict.fromkeys(res["emails"]))
    res["emails"].sort(key=lambda x: 0 if any(h in x for h in BIZ_HINTS) else 1)
    res["tg"] = list(dict.fromkeys(res["tg"]))[:5]
    res["wa"] = list(dict.fromkeys(res["wa"]))[:5]
    res["sources"] = list(dict.fromkeys(res["sources"]))
    return res


def fmt_contact(r: dict) -> str:
    """拼成一格放进「联系方式」列。"""
    bits = []
    if r["emails"]:
        bits.append("邮箱: " + ", ".join(r["emails"][:3]))
    if r["tg"]:
        bits.append("TG: " + ", ".join("@" + x for x in r["tg"][:2]))
    if r["wa"]:
        bits.append("WA: " + ", ".join(r["wa"][:2]))
    return " | ".join(bits)


def main() -> int:
    ap = argparse.ArgumentParser(description="批量挖联系方式（多路合并）")
    ap.add_argument("--slug", help="只跑一个")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--min-viewers", type=int, default=100, help="候选池里只跑观众≥N的")
    ap.add_argument("--no-fetch", action="store_true", help="不抓外部页（只平台 bio，最快）")
    ap.add_argument("--explain", action="store_true", help="逐路显示")
    ap.add_argument("--apply", action="store_true", help="写回联系筛选表")
    ap.add_argument("--dry-run", action="store_true", help="配合 --apply 只打印")
    ap.add_argument("--gap", type=float, default=1.5, help="每路间隔秒（防限流）")
    a = ap.parse_args()

    cache = load_cache()
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    if a.slug:
        cands = json.load(open(CAND_FILE))
        c = cands.get(a.slug) or {}
        if a.slug.lower() in load_coop():
            print(f"⚠️ {a.slug} 在已合作库里 —— 正常批量跑会跳过它；这次是你手动指定，继续执行")
        r = enrich_one(a.slug, c.get("platform") or "kick", not a.no_fetch, a.gap)
        cache[a.slug] = r; save_cache(cache)
        print(f"\n{a.slug}")
        for k in ("emails", "tg", "wa", "sources"):
            print(f"  {k:8}: {r[k]}")
        print(f"  socials : {json.dumps(r['socials'], ensure_ascii=False)[:200]}")
        return 0

    if not os.path.exists(CAND_FILE):
        print(f"没有候选池: {CAND_FILE}", file=sys.stderr); return 2
    cands = json.load(open(CAND_FILE))
    coop = load_coop()
    print(f"已合作库 {len(coop)} 条（这些会被跳过，不再挖联系方式）")
    pool, skipped = [], 0
    for slug, c in cands.items():
        if c.get("excluded"):
            continue
        if slug.lower() in coop:
            skipped += 1
            continue
        try:
            v = float(str(c.get("viewers") or 0).replace("K", "000"))
        except Exception:
            v = 0
        if v < a.min_viewers:
            continue
        _plat, _slug = split_key(slug, c)
        pool.append((v, _slug, _plat, slug))
    pool.sort(reverse=True)
    todo = pool[:a.limit]
    print(f"候选池 {len(cands)}｜跳过已合作 {skipped}｜观众≥{a.min_viewers} 的 {len(pool)} 个"
          f"｜本轮跑 {len(todo)} 个")

    hit_e = hit_any = 0
    out = []
    for i, (v, slug, plat, _rawkey) in enumerate(todo, 1):
        if slug in cache and _fresh(cache[slug]):
            r = cache[slug]
            print(f"[{i}/{len(todo)}] {slug[:22]:22} 缓存")
        else:
            print(f"[{i}/{len(todo)}] {slug[:22]:22} 观众{int(v):>5} 抓取中…", flush=True)
            r = enrich_one(slug, plat, not a.no_fetch, a.gap)
            cache[slug] = r; save_cache(cache)
            time.sleep(a.gap)
        if r["emails"]:
            hit_e += 1
        if r["emails"] or r["tg"] or r["wa"]:
            hit_any += 1
            out.append(r)
        if a.explain and (r["emails"] or r["tg"] or r["wa"]):
            print(f"      邮箱 {r['emails'][:2]} TG {r['tg'][:2]} WA {r['wa'][:2]} 来源 {r['sources']}")

    print(f"\n{'='*60}")
    print(f"命中：有邮箱 {hit_e}/{len(todo)}｜有任何联系方式 {hit_any}/{len(todo)}")
    for r in out[:15]:
        print(f"  {r['slug'][:22]:22} {fmt_contact(r)[:90]}")

    res_file = os.path.join(HERE, "logs", f"contact_enrich_{datetime.now():%Y%m%d_%H%M}.json")
    os.makedirs(os.path.dirname(res_file), exist_ok=True)
    json.dump(out, open(res_file, "w"), ensure_ascii=False)
    print(f"\n✅ 结果: {res_file}")

    if a.apply:
        print("\n--apply：写回「联系筛选」表 —— 尚未实现，先确认命中率是否可接受")
    return 0


if __name__ == "__main__":
    sys.exit(main())
