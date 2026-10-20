#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""youtube_discover_cloud.py — 云上 YouTube 发现（GitHub Actions，自包含）

口径（2026-10-08 与用户确认）：
- **发现方式**：**关键词搜索**（`search.list`，100 units/次）—— 与本平台"拉在播列表"不同，
  关键词来自 `automation/yt_keywords.json`（从老板表 `YT搜索:` 标记导出，含 market + 垂类预分）。
- **门槛（用户定）**：YouTube KOL **≥20000 subs**；高价值国家/专属语言打对折 → ≥10000；
  **未达标全部进 KOC**。
- **分流（用户定）**：**按关键词垂类** —— casino 词 → casino 表；noncasino（sports/预测/crypto）→ 非casino 表。
- **去重**：主键 `youtube:<channel_id>`（UC… 不变）＋ slug 兜底（按平台隔离）。
- **列**：写表按各表**真实表头**动态取用（钓KOC 24 列、第3列叫「用户名」、多「来源」列）。

env（Secrets）：GSPREAD_SA_JSON / DISCOVER_SHEET_ID / YOUTUBE_API_KEY
用法：python3 youtube_discover_cloud.py [--apply] [--limit N]（不加 --apply = 干跑）
配额：每关键词 ≈ 100(search) + ~1(channels 批量) units；默认 12 个/轮 ≈ 1.2k units（日上限 10000）。
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# 2026-10-09: 表内时间戳统一为本地时间（IST, UTC+5:30）—— 与本机写表脚本口径一致。
def _local_ts(fmt="%Y-%m-%d %H:%M"):
    return time.strftime(fmt, time.gmtime(time.time() + 5 * 3600 + 30 * 60))

API = "https://www.googleapis.com/youtube/v3"
HERE = os.path.dirname(os.path.abspath(__file__))
SHEET_ID = os.environ.get("DISCOVER_SHEET_ID", "")
APPLY = ("--apply" in sys.argv) or (os.environ.get("YT_APPLY") == "1")
# 每轮搜多少个关键词（配额保护：100 units/词）
MAX_KW = int(os.environ.get("YT_MAX_KW", "12"))

# 联系方式提取（纯正则，不调 AI）—— 输入是 channels.list 的 `snippet.description`。
# 2026-10-12 修（用户指出）：`enrich()` 早就把 description 取回来了，但 `metas()` 里
#   「联系方式」一直写死空串 → 实测 youtube-cloud 写的 175 行里没有一行有联系方式。
RE_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
RE_TG = re.compile(r"(?:t\.me/|telegram[:\s]+@?)([A-Za-z0-9_]{4,32})", re.I)
RE_WA = re.compile(r"wa\.me/(\d{7,15})")

# ── 门槛（用户定：YouTube 20000，高价值国家对折 → 10000）──
KOL_MIN_YT = int(os.environ.get("YT_KOL_MIN", "20000"))
HIGH_VALUE_FACTOR = 0.5
HIGH_VALUE_COUNTRIES = {"挪威", "瑞典", "丹麦", "芬兰", "冰岛", "瑞士", "奥地利", "卢森堡",
                        "爱尔兰", "新加坡", "卡塔尔", "阿联酋", "科威特", "巴林", "文莱", "新西兰"}
# 国家码 → 中文（YouTube snippet.country 是 ISO-2）
CC_CN = {"US": "美国", "GB": "英国", "CA": "加拿大", "AU": "澳大利亚", "IE": "爱尔兰", "NZ": "新西兰",
         "MX": "墨西哥", "AR": "阿根廷", "CL": "智利", "CO": "哥伦比亚", "PE": "秘鲁", "VE": "委内瑞拉",
         "EC": "厄瓜多尔", "BR": "巴西", "ES": "西班牙", "PT": "葡萄牙", "DE": "德国", "AT": "奥地利",
         "CH": "瑞士", "FR": "法国", "IT": "意大利", "NL": "荷兰", "BE": "比利时", "SE": "瑞典",
         "NO": "挪威", "DK": "丹麦", "FI": "芬兰", "IS": "冰岛", "PL": "波兰", "CZ": "捷克",
         "RO": "罗马尼亚", "HU": "匈牙利", "TR": "土耳其", "RU": "俄罗斯", "UA": "乌克兰", "NG": "尼日利亚",
         "ZA": "南非", "KE": "肯尼亚", "GH": "加纳", "EG": "埃及", "MA": "摩洛哥", "IN": "印度",
         "PK": "巴基斯坦", "BD": "孟加拉", "ID": "印尼", "MY": "马来西亚", "PH": "菲律宾", "TH": "泰国",
         "VN": "越南", "SG": "新加坡", "AE": "阿联酋", "SA": "沙特", "QA": "卡塔尔", "KW": "科威特",
         "BH": "巴林", "IL": "以色列", "JP": "日本", "KR": "韩国", "TW": "台湾", "HK": "香港", "CN": "中国"}
HIGH_VALUE_CC = {"NO", "SE", "DK", "FI", "IS", "CH", "AT", "LU", "IE", "SG", "QA", "AE", "KW", "BH", "BN", "NZ"}

# 语言码 → 中文（channels.list 的 defaultLanguage；常为空）
LANG_CN = {"en": "英语", "es": "西班牙语", "pt": "葡萄牙语", "de": "德语", "fr": "法语", "it": "意大利语",
           "ru": "俄语", "tr": "土耳其语", "pl": "波兰语", "id": "印尼语", "ja": "日语", "ko": "韩语",
           "zh": "中文", "ar": "阿拉伯语", "nl": "荷兰语", "sv": "瑞典语", "no": "挪威语", "da": "丹麦语",
           "fi": "芬兰语", "hi": "印地语", "fil": "菲律宾语", "th": "泰语", "vi": "越南语"}
# 分区码 → 中文（search/channels 的 snippet.defaultAudioLanguage 用）
DEFAULT_LANG_BY_MARKET = {"MX": "西班牙语", "ID": "印尼语", "NG": "英语", "GL": "英语"}

# 相关性/规模过滤（照本机 yt_discover.py：否则 search 会塞一堆无关大号）
REL = re.compile(
    r"(slot|casino|kasino|bet|betting|predic|apuesta|tragamoneda|tragaperra|bandar|judi|togel"
    r"|parlay|odds|cuota|pronostic|roulette|ruleta|jackpot|gambl|crypto|usdt|btc|binance|forex"
    r"|trading|stake|bc\.game|bet9ja|sportybet|1xbet|melbet|rainbet|roobet|qzino|big\s?win"
    r"|football|futbol|soccer|liga|nfl|sports)", re.I)
MIN_SUBS, MAX_SUBS = 1000, 2_000_000

TAB = {"casino": {"kol": "casinoKOL", "koc": "钓KOC"},
       "noncasino": {"kol": "非casinoKOL", "koc": "非casinoKOC"}}

UA = {"User-Agent": "kol-discover-cloud/1.0 (+GitHub Actions)", "Accept": "application/json"}


def key():
    k = os.environ.get("YOUTUBE_API_KEY", "").strip()
    if k:
        return k
    # 本机干跑兜底（云端只用 Secrets）
    for p in ("~/scripts/.secrets/youtube_key",):
        try:
            v = open(os.path.expanduser(p)).read().strip()
            if v:
                return v
        except Exception:
            pass
    return ""


def api(path, params, timeout=25):
    q = urllib.parse.urlencode(dict(params, key=key()))
    rq = urllib.request.Request(API + path + "?" + q, headers=UA)
    try:
        with urllib.request.urlopen(rq, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:180]
        except Exception:
            pass
        print("  [warn] %s → HTTP %s %s" % (path, e.code, body), file=sys.stderr)
        return None
    except Exception as e:
        print("  [warn] %s → %s" % (path, type(e).__name__), file=sys.stderr)
        return None


def load_keywords():
    p = os.path.join(HERE, "yt_keywords.json")
    if not os.path.exists(p):
        p = os.path.expanduser("~/scripts/github-actions-discover/automation/yt_keywords.json")
    d = json.load(open(p, encoding="utf-8"))
    return [k for k in (d.get("keywords") or []) if k.get("kw")]


def discover(kws):
    """逐关键词 search.list → {channel_id: {...}}（含 kind/market 来源）"""
    found = {}
    used = 0
    for item in kws[:MAX_KW]:
        kw, mk, kind = item["kw"], item.get("market") or "", item.get("kind") or "noncasino"
        params = {"part": "snippet", "type": "channel", "q": kw, "maxResults": "50"}
        if re.fullmatch(r"[A-Z]{2}", mk):
            params["regionCode"] = mk
        d = api("/search", params)
        used += 100
        if not d:
            continue
        for it in (d.get("items") or []):
            cid = str((it.get("id") or {}).get("channelId") or (it.get("snippet") or {}).get("channelId") or "")
            if not cid:
                continue
            if cid not in found:
                found[cid] = {"cid": cid, "kind": kind, "market": mk, "kw": kw}
        print("  [search] %-38s [%s] → 累计候选 %d" % (kw, mk or "--", len(found)), file=sys.stderr)
    return found, used


def enrich(cids):
    """批量 channels.list（50/次）：名称/国家/粉丝/观看/视频数/语言"""
    out = {}
    cids = list(cids)
    for i in range(0, len(cids), 50):
        d = api("/channels", {"part": "snippet,statistics", "id": ",".join(cids[i:i + 50]),
                              "maxResults": "50"})
        for it in ((d or {}).get("items") or []):
            sn = it.get("snippet") or {}
            st = it.get("statistics") or {}
            out[str(it.get("id") or "")] = {
                "title": sn.get("title") or "", "desc": sn.get("description") or "",
                "country": str(sn.get("country") or "").upper(),
                "lang": str(sn.get("defaultAudioLanguage") or sn.get("defaultLanguage") or ""),
                "subs": int(st.get("subscriberCount") or 0),
                "views": int(st.get("viewCount") or 0),
                "videos": int(st.get("videoCount") or 0),
                "custom": sn.get("customUrl") or "",
            }
    return out


def is_kol(subs, cc):
    factor = HIGH_VALUE_FACTOR if cc in HIGH_VALUE_CC else 1.0
    return int(subs or 0) >= int(KOL_MIN_YT * factor)


def metas(c, e):
    """列名 → 值（写表按真实表头取用）"""
    cid = c["cid"]
    cc = (e or {}).get("country") or ""
    country = CC_CN.get(cc, cc)
    lang = LANG_CN.get(str((e or {}).get("lang") or "").lower()) or DEFAULT_LANG_BY_MARKET.get(c["market"], "")
    handle = str((e or {}).get("custom") or "").lstrip("@") or cid
    # 联系方式：从频道 description 提 —— 这段文本 `enrich()` 早就取回来了（e["desc"]），
    # 2026-10-12 修（用户指出）：原先这里直接写死空串，等于白拿了简介不用。
    _bio = str((e or {}).get("desc") or "")
    mails, tgs, was = RE_EMAIL.findall(_bio), RE_TG.findall(_bio), RE_WA.findall(_bio)
    contact = ("邮箱: mailto:%s" % mails[0]) if mails else ""
    others = " / ".join(["TG:" + t for t in tgs[:2]] + ["WA:" + w for w in was[:2]])
    return {
        "时间戳": _local_ts(), "平台": "YouTube",
        "主播名": (e or {}).get("title") or handle, "用户名/Slug": handle, "用户名": handle,
        "平台UID": cid, "分类": c["kw"], "语种": lang,
        "观众数": "", "粉丝数": str((e or {}).get("subs") or ""),
        "主页链接": "https://www.youtube.com/channel/%s" % cid,
        "国家": country, "状态": "待确认",
        "联系方式": contact, "其他社交账号": others,
        "国家判断理由": "YouTube channels.list snippet.country（%s）" % (cc or "无"),
        "IG": "", "X": "", "TikTok": "", "YouTube": ("@" + handle) if handle != cid else "",
        "Facebook": "", "Discord": "", "william备注": "",
        "建联腿": "youtube-cloud", "来源": "youtube-cloud:%s" % c["kw"],
        "_subs": int((e or {}).get("subs") or 0), "_cc": cc,
    }


def load_seen(sh):
    seen, slugs = set(), set()
    names = [TAB["casino"]["kol"], TAB["casino"]["koc"], TAB["noncasino"]["kol"], TAB["noncasino"]["koc"]]
    for nm in names:
        try:
            ws = sh.worksheet(nm)
            for v in ws.col_values(5)[1:]:
                if str(v).strip():
                    seen.add("youtube:" + str(v).strip())
            plat, slug = ws.col_values(2), ws.col_values(4)
            for i in range(1, min(len(plat), len(slug))):
                if "youtube" in str(plat[i]).lower() and str(slug[i]).strip():
                    slugs.add(str(slug[i]).strip().lower())
        except Exception as e:
            print("  [warn] 读 %s 索引失败: %s" % (nm, str(e)[:70]), file=sys.stderr)
    print("[去重] 已登记 YouTube UID %d / slug %d" % (len(seen), len(slugs)), file=sys.stderr)
    return seen, slugs


def main():
    if not SHEET_ID:
        print("缺 DISCOVER_SHEET_ID", file=sys.stderr)
        return 2
    if not key():
        print("缺 YOUTUBE_API_KEY", file=sys.stderr)
        return 2
    kws = load_keywords()
    print("[关键词] %d 个（本轮搜 %d 个）" % (len(kws), min(len(kws), MAX_KW)), file=sys.stderr)
    found, used = discover(kws)
    print("[发现] 候选 %d 个（用 ~%d units）" % (len(found), used), file=sys.stderr)
    if not found:
        return 0

    import gspread
    import _gspread429; _gspread429.patch()   # 429 配额自动重试（读写全覆盖）
    sa_raw = os.environ.get("GSPREAD_SA_JSON") or ""
    if sa_raw:
        p = os.path.join(os.environ.get("RUNNER_TEMP") or "/tmp", "sa.json")
        with open(p, "w") as f:
            f.write(sa_raw)
    else:
        p = os.environ.get("GSPREAD_SA") or ""
    sh = gspread.service_account(filename=p).open_by_key(SHEET_ID)
    seen, slugs_seen = load_seen(sh)

    hdrs = {}
    for kind, t in TAB.items():
        for tier, nm in t.items():
            hdrs[(kind, tier)] = (nm, sh.worksheet(nm).row_values(1))

    # 先按去重过滤，再批量富化（省配额）
    todo = []
    st = {"uid": 0}
    for cid, c in found.items():
        if ("youtube:" + cid) in seen:
            st["uid"] += 1
            continue
        todo.append(c)
    print("[去重] uid 跳过 %d → 待富化 %d" % (st["uid"], len(todo)), file=sys.stderr)
    if not todo:
        return 0
    info = enrich([c["cid"] for c in todo])
    print("[富化] channels 命中 %d/%d" % (len(info), len(todo)), file=sys.stderr)

    out_rows, n_kol, n_koc, n_rel, n_subs = {}, 0, 0, 0, 0
    for c in todo:
        e = info.get(c["cid"])
        if not e:
            continue
        subs = e["subs"]
        if not (MIN_SUBS <= subs <= MAX_SUBS):
            continue
        n_subs += 1                                     # 2026-10-13 诊断：过了规模门槛的
        if not REL.search(e["title"] + " " + e["desc"]):     # 相关性过滤
            continue
        n_rel += 1
        mm = metas(c, e)
        kind = c["kind"] if c["kind"] in ("casino", "noncasino") else "noncasino"
        tier = "kol" if is_kol(subs, e["country"]) else "koc"
        n_kol += (tier == "kol")
        n_koc += (tier == "koc")
        nm, hdr = hdrs[(kind, tier)]
        out_rows.setdefault(nm, {"hdr": hdr, "rows": []})
        out_rows[nm]["rows"].append([str(mm.get(col, "")) for col in hdr])   # ← 按真实表头取列
        seen.add("youtube:" + c["cid"])
        slugs_seen.add(str(mm["用户名/Slug"]).lower())

    total = 0
    for nm, pack in out_rows.items():
        rows = pack["rows"]
        total += len(rows)
        print("[%s] 待写 %d 行（表头 %d 列）" % (nm, len(rows), len(pack["hdr"])), file=sys.stderr)
        for r in rows[:3]:
            print("     + " + " | ".join(str(x)[:16] for x in r[:8]), file=sys.stderr)
        if APPLY and rows:
            # 2026-10-09: 429 重试（与本机脚本共用同一 service account，Sheets 读配额会被抢）
            _ok = False
            for _try in range(9):
                try:
                    sh.worksheet(nm).append_rows(rows, value_input_option="USER_ENTERED")
                    _ok = True
                    break
                except Exception as _e:
                    if "429" not in str(_e) or _try == 4:
                        raise
                    _w = min(240, 30 * (_try + 1))
                    print("     [warn] %s 配额 429，%ds 后重试(%d/9)" % (nm, _w, _try + 1), file=sys.stderr)
                    time.sleep(_w)

            print("     [写入] %s +%d" % (nm, len(rows)), file=sys.stderr)
    print("[结果] 待富化 %d → 过规模门槛 %d → 相关 %d；KOL=%d KOC=%d 合计 %d 行%s"
          % (len(todo), n_subs, n_rel, n_kol, n_koc, total, "" if APPLY else "（dry-run 未写）"), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
