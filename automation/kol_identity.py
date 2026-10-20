#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""跨平台身份解析 —— 从 bio / 主页描述 / link-in-bio 页面解析达人在各平台的账号。

第三方平台（Modash/Favikon/HypeAuditor）的核心能力就是这层「跨平台身份图谱」：
靠 bio 自贴链接、link-in-bio 中间页、同 handle 匹配，把一个人在各平台的账号串起来。
本脚本做小量版：纯文本解析 + 低频抓 link-in-bio（不碰官方 API、不耗配额）。

用法:
  python3 kol_identity.py --text "@slotgacor | linktr.ee/xyz follow me"        # 直接解析一段文本
  python3 kol_identity.py --from-tsv ~/Downloads/tiktok_discover.tsv          # 批量解析（列名自动识别）
"""
import argparse, json, os, re, sys, time, urllib.request, urllib.error

PLAT = {
    "instagram": r"instagram\.com/([A-Za-z0-9_.]{2,30})",
    "twitter":   r"(?:twitter|x)\.com/([A-Za-z0-9_]{2,20})",
    "youtube":   r"youtube\.com/(@[A-Za-z0-9_.-]{2,40}|channel/[A-Za-z0-9_-]{10,30}|c/[A-Za-z0-9_-]{2,40})",
    "tiktok":    r"tiktok\.com/@([A-Za-z0-9_.]{2,30})",
    "telegram":  r"t\.me/([A-Za-z0-9_]{4,32})",
    "kick":      r"kick\.com/([A-Za-z0-9_.-]{2,30})",
    "twitch":    r"twitch\.tv/([A-Za-z0-9_]{2,30})",
    "discord":   r"discord\.(?:gg|com/invite)/([A-Za-z0-9_-]{2,30})",
    "facebook":  r"facebook\.com/(?:gaming/|pages/|profile\.php\?id=)?([A-Za-z0-9_.]{2,40})",
    "linktree":  r"linktr\.ee/([A-Za-z0-9_.-]{2,40})",
    "whatsapp":  r"(?:wa\.me/|api\.whatsapp\.com/send\?phone=)(\d{6,15})",
    "email":     r"([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})",
}
# link-in-bio 中间页（第三方平台公认最强信号之一）
LINKINBIO = ("linktr.ee", "beacons.ai", "komi.io", "bio.link", "link.me", "pillar.io",
            "tap.bio", "allmylinks.com", "carrd.co", "stan.store", "withkoji.com")
SKIP_HANDLE = {"p", "reel", "reels", "explore", "stories", "share", "watch", "shorts", "channel",
               "home", "videos", "video", "directory", "browse", "about", "subscriptions",
               "followers", "following", "search", "results", "settings", "pages", "groups",
               "gaming", "marketplace", "profile.php", "login", "signup"}
# 已知平台域名（用于把「自建站/个人域名」从账号里分出来 —— 个人域名也是身份线索）
PLATFORM_HOSTS = ("instagram.com", "twitter.com", "x.com", "youtube.com", "youtu.be", "tiktok.com",
                  "t.me", "telegram.me", "kick.com", "twitch.tv", "discord.gg", "discord.com",
                  "facebook.com", "fb.com", "linktr.ee", "wa.me", "whatsapp.com")


def parse_links(text: str) -> dict:
    """从任意文本里抽出各平台账号。"""
    if not text:
        return {}
    out = {}
    for plat, pat in PLAT.items():
        hits = []
        for m in re.findall(pat, text, re.I):
            h = (m if isinstance(m, str) else m[0]).strip().strip("/")
            if h and h.lower() not in SKIP_HANDLE and h not in hits:
                hits.append(h)
        if hits:
            out[plat] = hits
    return out


def fetch_links(url: str, timeout=20) -> str:
    """抓 link-in-bio 页面文本（低频、只读）。"""
    req = urllib.request.Request(url, headers={
        "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36"),
        "Accept-Language": "en-US,en;q=0.9"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(400_000).decode("utf-8", "replace")


def expand(text: str, do_fetch=True, gap=2.0) -> dict:
    """解析文本；若含 link-in-bio，则抓该页并合并里面的链接。"""
    res = parse_links(text)
    if not do_fetch:
        return res
    for dom in LINKINBIO:
        for slug in res.get("linktree_holder", []):
            pass
    m = re.search(r"(https?://)?(?:www\.)?(%s)/([A-Za-z0-9_.-]+)" %
                  "|".join(x.replace(".", r"\.") for x in LINKINBIO), text or "", re.I)
    if m:
        url = m.group(0)
        if not url.startswith("http"):
            url = "https://" + url
        try:
            page = fetch_links(url)
            more = parse_links(page)
            for k, v in more.items():
                merged = res.setdefault(k, [])
                for x in v:
                    if x not in merged:
                        merged.append(x)
            res["_via_linkinbio"] = url
            time.sleep(gap)
        except Exception as e:
            res["_linkinbio_err"] = "%s %s" % (type(e).__name__, str(e)[:60])
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--text", default="")
    ap.add_argument("--from-tsv", default="")
    ap.add_argument("--col", default="", help="从哪列取文本（默认自动找 sig/nick/desc/bio）")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--no-fetch", action="store_true", help="不抓 link-in-bio（纯文本解析）")
    ap.add_argument("--out", default="")
    a = ap.parse_args()

    if a.text:
        print(json.dumps(expand(a.text, not a.no_fetch), ensure_ascii=False, indent=2)); return

    import csv
    rows = list(csv.DictReader(open(os.path.expanduser(a.from_tsv), encoding="utf-8"),
                               delimiter="\t", quoting=csv.QUOTE_NONE))
    if a.limit:
        rows = rows[:a.limit]
    cols = [a.col] if a.col else [c for c in (rows[0].keys() if rows else [])
                                 if c.lower() in ("sig", "signature", "bio", "desc", "description", "nick")]
    found, hit = [], 0
    for i, r in enumerate(rows, 1):
        blob = " ".join(str(r.get(c) or "") for c in cols)
        res = expand(blob, not a.no_fetch)
        plats = {k: v for k, v in res.items() if not k.startswith("_")}
        if plats:
            hit += 1
            found.append({"handle": r.get("handle") or r.get("channel") or "", "market": r.get("market", ""),
                          **plats})
            print("  [%d] %-22s -> %s" % (i, (r.get('handle') or '')[:22],
                                          {k: v[:1] for k, v in plats.items()}))
    print("命中 %d/%d 行有跨平台线索" % (hit, len(rows)))
    if a.out and found:
        open(os.path.expanduser(a.out), "w").write(json.dumps(found, ensure_ascii=False, indent=1))
        print("已写:", a.out)


if __name__ == "__main__":
    main()
