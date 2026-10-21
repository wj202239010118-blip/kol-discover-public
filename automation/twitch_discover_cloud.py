#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""twitch_discover_cloud.py — 云上 Twitch 发现（GitHub Actions，自包含）

口径（与 kick_discover_cloud.py 一致，2026-10-08 用户逐条确认）：
- **垂类二分**：casino 类目 → casino 表；非casino 类目 → 非casino 表；其余类目**不收**。
- **KOL/KOC 分层**：Twitch KOL ≥5000（高价值国家/专属语言打对折 → ≥2500）；
  **未达标的全部进 KOC**（用户：不丢）。
- **去重**：主键 `twitch:<user_id>`（官方 id，不随改名/换链接变）；历史空 UID 行用 slug 兜底，
  且 **slug 兜底必须按平台隔离**（四表同时有 Twitch/Kick 记录，裸 slug 会跨平台误撞）。
- **列**：写表时按各表**真实表头**动态取用 —— `钓KOC` 是 24 列、第 3 列叫「用户名」、index 15 多一列
  「来源」，共用一套行会错位。

与 Kick 的差异（Twitch 更简单）：
- **语种**：Twitch 自带 `language`（BCP-47，如 `en`/`es`/`pt-br`）→ 直接映射中文，
  **不需要聊天采样、不需要 LLM**（Kick 才需要）。
- **类目**：Twitch 类目 ID 与 Kick **完全不同** → 各用各的清单（本文件用 Twitch 的）。
- **粉丝数**：`helix/streams` 不带 → 批量调 `helix/channels`（100 个 id/次）。
- **认证**：App Access Token（client_credentials）。

env（Secrets）：GSPREAD_SA_JSON / DISCOVER_SHEET_ID / TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET
用法：python3 twitch_discover_cloud.py [--apply]（不加 = 干跑，不写表）
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

TWITCH_ID = "https://id.twitch.tv/oauth2/token"
HELIX = "https://api.twitch.tv/helix"
SHEET_ID = os.environ.get("DISCOVER_SHEET_ID", "")
APPLY = ("--apply" in sys.argv) or (os.environ.get("TWITCH_APPLY") == "1")
# 限流：每轮最多处理多少个"新候选"（详情+写表都要时间，且 30 分钟周期内必须跑完）
MAX_NEW = int(os.environ.get("TWITCH_MAX_NEW", "60"))
MAX_PAGES = int(os.environ.get("TWITCH_MAX_PAGES", "3"))    # 每个类目最多翻几页（100/页）
# 2026-10-21(消化效率): KOL/KOC 配额分开 + 详情失败重试（同 kick 腿的口径）。
KOL_QUOTA = int(os.environ.get("TWITCH_KOL_QUOTA", "140"))
KOC_QUOTA = int(os.environ.get("TWITCH_KOC_QUOTA", "60"))
RETRY_P = os.environ.get("TWITCH_RETRY_STATE") or os.path.join(
    os.environ.get("RUNNER_TEMP") or "/tmp", "twitch_retry.json")


def load_retry():
    try:
        with open(RETRY_P, encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def save_retry(d):
    try:
        with open(RETRY_P, "w", encoding="utf-8") as f:
            json.dump(d, f)
    except Exception:
        pass

BATCH_CH = 100                                              # helix/channels 批量上限
BATCH_USER = 100                                            # helix/users 批量上限（一次可带 100 个 login）

# 联系方式提取（纯正则，不调 AI）—— 与 kick_discover_cloud.py 同一套口径。
RE_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
RE_TG = re.compile(r"(?:t\.me/|telegram[:\s]+@?)([A-Za-z0-9_]{4,32})", re.I)
RE_WA = re.compile(r"wa\.me/(\d{7,15})")

# ── 类目（照 discover-runner.py，别自己编；Twitch 与 Kick 的 id 完全不同）──
CASINO_IDS = ['498566', '582382910', '488190', '490344', '202076418',
              '760679498', '147119461', '29452', '1346169551',
              '493887283',
              '499634']   # Slots/Casino/Poker/Roulette/Blackjack/Baccarat/Craps/Virtual Casino/Casino Simulator(+Casino 同名双类目) / Crypto
# 2026-10-09（用户定）：补 Crypto(499634)。实测榜上有一批 crypto 直播达人，
#   原名单没有这个独立类目 → 收人时被 skip_cat 丢掉（一次干跑就跳掉几十个）。
# 2026-10-08 修复：Twitch 有**同名双类目 "Casino"** —— 582382910 与 493887283。实测活跃的是后者
#   （在播主播更多；streamscharts casino 榜的候选全落在它下面）。漏掉它 = 整整一个 casino 类目收不到。
NONCASINO_IDS = {'518203': 'sports', '509658': 'just-talking', '66082': 'games-demos',
                 '509672': 'irl'}  # Sports / Just Chatting / Games+Demos / IRL
# 2026-10-09（用户定）：补 IRL(509672)。Twitch 的 IRL 与 Just Chatting(509658) 是**两个并行类目**，
#   原名单只有后者 → 榜上 IRL 达人当前在播 509672 时被整批 skip_cat（一次干跑跳掉 8000+）。
# game_id → 表「分类」列口径（2026-10-08 统一）：历史主口径是**官方类目 slug 小写连字符**，
#   而 /helix/streams 给的是展示名 game_name（'Slots'/'Just Chatting'/'Games + Demos'）→ 直接写会与历史不齐。
CATEGORY_SLUG = {"498566": "slots", "582382910": "casino", "493887283": "casino",
                 "488190": "poker", "490344": "roulette", "202076418": "blackjack",
                 "760679498": "baccarat", "147119461": "craps", "29452": "virtual-casino",
                 "1346169551": "casino-simulator", "499634": "crypto", **NONCASINO_IDS}


def category_slug(m):
    """「分类」列口径：优先 id→slug；未登记的类目回落小写 game_name（不编造 slug）。"""
    gid = str((m or {}).get("game_id") or "")
    return CATEGORY_SLUG.get(gid) or str((m or {}).get("game_name") or "").strip().lower()


TAB = {"casino": {"kol": "casinoKOL", "koc": "钓KOC"},
       "noncasino": {"kol": "非casinoKOL", "koc": "非casinoKOC"}}

# ── 门槛（照 sheet_common._qualifies_for_kol）──
KOL_MIN_TWITCH = 5000
HIGH_VALUE_FACTOR = 0.5
HIGH_VALUE_COUNTRIES = {"挪威", "瑞典", "丹麦", "芬兰", "冰岛", "瑞士", "奥地利", "卢森堡",
                        "爱尔兰", "新加坡", "卡塔尔", "阿联酋", "科威特", "巴林", "文莱", "新西兰"}
# 语言(ISO) → 国家：单一国家专属语言享折扣（与 sheet_common 一致）
DISCOUNT_LANG_TO_COUNTRY = {"no": "挪威", "sv": "瑞典", "da": "丹麦", "fi": "芬兰",
                            "is": "冰岛", "lb": "卢森堡", "ga": "爱尔兰", "mi": "新西兰"}

# Twitch language（BCP-47）→ 中文（未命中保留原码，不编造）
LANG_CN = {"en": "英语", "es": "西班牙语", "pt": "葡萄牙语", "pt-br": "葡萄牙语(巴西)", "de": "德语",
           "fr": "法语", "it": "意大利语", "ru": "俄语", "tr": "土耳其语", "pl": "波兰语",
           "ja": "日语", "ko": "韩语", "zh": "中文", "zh-tw": "中文(繁体)", "ar": "阿拉伯语",
           "nl": "荷兰语", "sv": "瑞典语", "no": "挪威语", "da": "丹麦语", "fi": "芬兰语",
           "is": "冰岛语", "cs": "捷克语", "sk": "斯洛伐克语", "hu": "匈牙利语", "ro": "罗马尼亚语",
           "uk": "乌克兰语", "el": "希腊语", "th": "泰语", "vi": "越南语", "id": "印尼语",
           "fil": "菲律宾语", "hi": "印地语", "other": "其他"}


def lang_cn(code):
    c = (code or "").strip().lower()
    if not c:
        return ""
    return LANG_CN.get(c) or LANG_CN.get(c.split("-")[0]) or c


UA = {"User-Agent": "kol-discover-cloud/1.0 (+GitHub Actions)", "Accept": "application/json"}
_TOKEN = {"v": "", "exp": 0.0}


def _req(url, headers=None, timeout=20, data=None):
    h = dict(UA)
    if headers:
        h.update(headers)
    try:
        rq = urllib.request.Request(url, data=data, headers=h)
        with urllib.request.urlopen(rq, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception:
        return 0, ""


def _json(url, headers=None, timeout=20):
    st, body = _req(url, headers, timeout)
    if st != 200 or not body:
        print("  [warn] %s → HTTP %s" % (url[:96], st), file=sys.stderr)
        return None
    try:
        return json.loads(body)
    except Exception:
        return None


def token():
    """App Access Token（client_credentials）。"""
    if _TOKEN["v"] and time.time() < _TOKEN["exp"] - 120:
        return _TOKEN["v"]
    cid, csec = os.environ.get("TWITCH_CLIENT_ID", ""), os.environ.get("TWITCH_CLIENT_SECRET", "")
    if not cid or not csec:
        print("  [warn] 缺 TWITCH_CLIENT_ID/TWITCH_CLIENT_SECRET", file=sys.stderr)
        return ""
    q = urllib.parse.urlencode({"client_id": cid, "client_secret": csec,
                                "grant_type": "client_credentials"})
    # 2026-10-08 实测：Twitch token 端点【不支持 GET query】（返回 404 not_found）→ 必须 POST
    st, body = _req(TWITCH_ID, {"Content-Type": "application/x-www-form-urlencoded"},
                    data=q.encode(), timeout=20)
    try:
        d = json.loads(body or "{}")
        _TOKEN["v"] = str(d.get("access_token") or "")
        _TOKEN["exp"] = time.time() + float(d.get("expires_in") or 3600)
    except Exception:
        _TOKEN["v"] = ""
    if not _TOKEN["v"]:
        print("  [warn] Twitch token 获取失败", file=sys.stderr)
    return _TOKEN["v"]


def hdr():
    return {"Client-Id": os.environ.get("TWITCH_CLIENT_ID", ""), "Authorization": "Bearer %s" % token()}


def live_for(game_id, max_pages=MAX_PAGES):
    """某类目在播（分页，100/页）。返回 {user_id: {...}}"""
    out, cursor, pages = {}, "", 0
    while pages < max_pages:
        url = "%s/streams?game_id=%s&first=100" % (HELIX, game_id)
        if cursor:
            url += "&after=" + urllib.parse.quote(cursor)
        d = _json(url, hdr())
        if not d:
            break
        data = d.get("data") or []
        for it in data:
            uid = str(it.get("user_id") or "")
            if not uid:
                continue
            out[uid] = {"uid": uid,
                        "login": str(it.get("user_login") or "").lower(),
                        "name": str(it.get("user_name") or ""),
                        "game_id": str(it.get("game_id") or game_id),
                        "game_name": str(it.get("game_name") or ""),
                        "viewers": it.get("viewer_count") or 0,
                        "lang": str(it.get("language") or "")}
        cursor = str((d.get("pagination") or {}).get("cursor") or "")
        pages += 1
        if not cursor or len(data) < 100:
            break
    return out


def channels_info(ids):
    """批量取频道信息 + 粉丝数。返回 {uid: {...}}"""
    out = {}
    for i in range(0, len(ids), BATCH_CH):
        chunk = [str(x) for x in ids[i:i + BATCH_CH]]
        q = "&".join("broadcaster_id=" + urllib.parse.quote(x) for x in chunk)
        d = _json("%s/channels?%s" % (HELIX, q), hdr())
        for it in ((d or {}).get("data") or []):
            out[str(it.get("broadcaster_id") or "")] = it
    # 2026-10-08 修复：/helix/channels **不返回 follower_count**（实测字段里没有）→
    #   之前粉丝数恒为 0 → is_kol() 恒 False → 新主播**全部被写进 KOC**（KOL 表收不到新数据）。
    #   必须用 /helix/channels/followers（app token 可用，返回 {total: N}）。
    for uid in list(out):
        try:
            d = _json("%s/channels/followers?broadcaster_id=%s" % (HELIX, uid), hdr())
            out[uid]["follower_count"] = int((d or {}).get("total") or 0)
        except Exception as e:
            out[uid]["follower_count"] = 0
            print("  [warn] followers %s: %s" % (uid, str(e)[:60]), file=sys.stderr)
        time.sleep(0.2)
    return out


def users_desc(logins):
    """批量取用户简介 —— `/helix/users` 的 `description` 字段。

    2026-10-12 修（用户指出）：本腿原先只调 `/streams` + `/channels` + `/channels/followers`，
    **唯独漏了 `/users`** → 简介一个都没拿到 → `metas()` 里「联系方式」只能用空串占位。
    实测后果：twitch-cloud 写进去的 4265 行里，联系方式非空只有 2 行（0.05%）。
    而 `/helix/users` 一次可带 100 个 login、秒回、纯 API —— **根本不需要抓页面**。

    返回 ``{login(小写): description}``（查不到的 login 不在返回里）。
    """
    out = {}
    todo = [str(x).strip() for x in (logins or []) if str(x or "").strip()]
    for i in range(0, len(todo), BATCH_USER):
        chunk = todo[i:i + BATCH_USER]
        q = "&".join("login=" + urllib.parse.quote(x.lower()) for x in chunk)
        try:
            d = _json("%s/users?%s" % (HELIX, q), hdr())
        except Exception as e:
            print("  [warn] users %s…: %s" % (chunk[:3], str(e)[:60]), file=sys.stderr)
            continue
        for it in ((d or {}).get("data") or []):
            out[str(it.get("login") or "").lower()] = str(it.get("description") or "")
    return out


def is_kol(followers, country, lang_code):
    """阈值判定（照 sheet_common）：高价值国家 或 专属语言 → 打对折。"""
    f = int(followers or 0)
    factor = 1.0
    if country in HIGH_VALUE_COUNTRIES or DISCOUNT_LANG_TO_COUNTRY.get((lang_code or "").lower()) in HIGH_VALUE_COUNTRIES:
        factor = HIGH_VALUE_FACTOR
    return f >= int(KOL_MIN_TWITCH * factor)


def _active_days(uid, days=30):
    """30 天活跃天数（有开播/有存档 VOD 的不同日期数）。

    2026-10-21(用户): TwitchTracker summary 不返回活跃天数（只有 7 个字段），
    报价页那个「活跃天数」是本机按 VOD 日期去重算的。云端没有本机记录，
    所以直接用 **Twitch 官方 Helix** 的 archive 视频列表推：取最近 100 场，
    日期去重。<30 天超 100 场的主播会低估，属已知上限（Helix 单页上限 100）。
    """
    if not uid:
        return ""
    try:
        d = _json("%s/videos?user_id=%s&type=archive&first=100" % (HELIX, urllib.parse.quote(str(uid))),
                  headers=hdr()) or {}
        from datetime import datetime, timedelta, timezone as _tz
        cut = (datetime.now(_tz.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        dates = set()
        for v in (d.get("data") or []):
            ct = str(v.get("created_at") or "")
            if ct and ct >= cut:
                dates.add(ct[:10])
        return str(len(dates))
    except Exception:
        return ""


def _tracker_stats(slug):
    """TwitchTracker 30 天统计（实测可用，无需登录）。

    返回 minutes_streamed / avg_viewers / max_viewers / hours_watched / followers(30天涨粉) / followers_total。
    注意：`max_viewers` 是 **30 天峰值**，不是历史峰值；summary 也不给「活跃天数」——这两列宁可不填，不编造。
    """
    if os.environ.get("TWITCH_TRACKER", "1") in ("0", "", "false", "False"):
        return {}
    try:
        from curl_cffi import requests as cr
        r = cr.get("https://twitchtracker.com/api/channels/summary/" + urllib.parse.quote(slug),
                   impersonate="chrome124", timeout=15,
                   headers={"Accept": "application/json"})
        return (r.json() or {}) if (r.status_code == 200 and r.text) else {}
    except Exception:
        return {}


def metas(m, ch, desc=""):
    """把「列名 → 值」一次算齐（写表时按各表真实表头取用）。

    ``desc`` = `/helix/users` 的 `description`（用户简介）。2026-10-12 新增 ——
    邮箱/TG/WA **只能**从这里提；此前没传这个参数 → 「联系方式」恒为空。
    """
    login = m.get("login") or ""
    uid = str(m.get("uid") or "")
    followers = (ch or {}).get("follower_count") or 0
    lang_code = str(m.get("lang") or "").lower()
    country = ""          # Twitch 不提供国家；官方语言字段只给语种 → 国家留空（不编造）
    why = "Twitch 无国家字段；语种取自 broadcaster_language"
    _bio = str(desc or "")
    _trk = _tracker_stats(login) if login else {}
    _ad = _active_days(uid) if uid else ""
    mails, tgs, was = RE_EMAIL.findall(_bio), RE_TG.findall(_bio), RE_WA.findall(_bio)
    # 格式与表里历史样本（discover-runner 写的 `邮箱: mailto:x@y`）保持一致 —— 同列别有两种写法。
    contact = ("邮箱: mailto:%s" % mails[0]) if mails else ""
    others = " / ".join(["TG:" + t for t in tgs[:2]] + ["WA:" + w for w in was[:2]])
    # 2026-10-21(用户): IG / X / TikTok / YouTube / Facebook 都是联系路径 ——
    #   Twitch 没有结构化社交字段，只能从简介里解析；没有邮箱/TG/WA 时用它兜底。
    _links = {}
    try:
        import kol_identity as _ki
        _links = _ki.parse_links(_bio) or {}
    except Exception:
        _links = {}
    _social = []
    for _k, _pref in (("twitter", "X"), ("instagram", "IG"), ("tiktok", "TikTok"),
                      ("youtube", "YouTube"), ("facebook", "FB")):
        _v = _links.get(_k) or []
        if _v:
            _social.append("%s: %s" % (_pref, str(_v[0]).lstrip("@")))
    if not contact and not others and _social:
        contact = " / ".join(_social[:3])
    return {
        "时间戳": _local_ts(), "平台": "Twitch", "主播名": m.get("name") or login,
        "用户名/Slug": login, "用户名": login, "平台UID": uid,
        "分类": category_slug(m), "语种": lang_cn(lang_code),
        "观众数": str(m.get("viewers") or ""), "粉丝数": str(followers or ""),
        "主页链接": "https://twitch.tv/%s" % login,
        "Kick主页": "", "Twitch主页": "https://twitch.tv/%s" % login,
        "国家": country, "状态": "待确认",
        "联系方式": contact, "其他社交账号": others, "国家判断理由": why,
        "30天累计观看时长": (("%d" % _trk["hours_watched"]) if _trk.get("hours_watched") is not None else ""),
        "30天峰值观众": (str(_trk["max_viewers"]) if _trk.get("max_viewers") is not None else ""),
        "30天平均观众": (str(_trk["avg_viewers"]) if _trk.get("avg_viewers") is not None else ""),
        "30天涨粉": (str(_trk["followers"]) if _trk.get("followers") is not None else ""),
        "活跃天数": _ad,
        "历史峰值观众": "",       # summary 的 max_viewers 是 30 天口径 → 留空
        "简介": _bio,
        "IG": ((_links.get("instagram") or [""])[0]),
        "X": ((_links.get("twitter") or [""])[0]),
        "TikTok": ((_links.get("tiktok") or [""])[0]),
        "YouTube": ((_links.get("youtube") or [""])[0]),
        "Facebook": ((_links.get("facebook") or [""])[0]), "Discord": "",
        "william备注": "", "建联腿": "twitch-cloud", "来源": "twitch-cloud",
        "_followers": int(followers or 0), "_lang_code": lang_code,
    }


def load_seen(sh):
    """去重索引：`twitch:<uid>` 主键 + slug 兜底（按平台隔离）。"""
    seen, slugs = set(), set()
    names = [TAB["casino"]["kol"], TAB["casino"]["koc"],
             TAB["noncasino"]["kol"], TAB["noncasino"]["koc"]]
    for nm in names:
        try:
            ws = sh.worksheet(nm)
            # 2026-10-21(提速): 3 次 col_values → 1 次 batchGet（B/D/E），口径不变。
            plat_c, slug_c, uid_c = ws.batch_get(["B:B", "D:D", "E:E"])
            for row in uid_c[1:]:
                v = row[0] if row else ""
                if str(v).strip():
                    seen.add("twitch:" + str(v).strip())
            for i in range(1, min(len(plat_c), len(slug_c))):
                pv = plat_c[i][0] if plat_c[i] else ""
                sv = slug_c[i][0] if slug_c[i] else ""
                if "twitch" in str(pv).lower() and str(sv).strip():
                    slugs.add(str(sv).strip().lower())
        except Exception as e:
            print("  [warn] 读 %s 索引失败: %s" % (nm, str(e)[:70]), file=sys.stderr)
    print("[去重] 已登记 Twitch UID %d / Twitch slug %d" % (len(seen), len(slugs)), file=sys.stderr)
    return seen, slugs


# ── 2026-10-21: D1 状态层（去重 / 状态机 / 指标）。未配置时全部静默回落旧路径。──
D1_API = (os.environ.get("DISCOVER_API") or "").rstrip("/")
D1_TOK = os.environ.get("DISCOVER_TOKEN") or ""
# D1 写失败（例如免费层日额度用尽）时的本地兜底：把「已写表但没记进 D1」的 uid 存文件，
# 下一轮去重时并进来，避免重复写表产生脏数据。
RECONCILE_P = os.environ.get("TWITCH_RECONCILE") or os.path.join(
    os.environ.get("RUNNER_TEMP") or "/tmp", "twitch_reconcile.txt")


def load_recon():
    try:
        with open(RECONCILE_P, encoding="utf-8") as f:
            return {ln.strip() for ln in f if ln.strip()}
    except Exception:
        return set()


def save_recon(ids):
    try:
        with open(RECONCILE_P, "w", encoding="utf-8") as f:
            for u in sorted(ids):
                f.write(u + "\n")
    except Exception:
        pass



def _d1(path, payload=None, method="POST"):
    if not D1_API or not D1_TOK:
        return None
    try:
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(D1_API + path, data=data, method=method,
            headers={"Authorization": "Bearer " + D1_TOK,
                     "Content-Type": "application/json", "User-Agent": "twitch-discover/1.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        print("  [warn] D1 %s 失败: %s" % (path, str(e)[:70]), file=sys.stderr)
        return None


def d1_known(uids):
    """批量查 D1 已知 uid。D1 不可用 → None（调用方回落读表）。"""
    if not D1_API or not D1_TOK:
        return None
    out = set()
    # 2026-10-21: 一次发 1000 个（Worker 内部再按 D1 参数上限分批）——
    #   实测 90/批 × 7 次往返要 15.5s；改 1 次往返约 1s。
    for i in range(0, len(uids), 1000):
        r = _d1("/discover/check", {"platform": "twitch", "uids": uids[i:i + 1000]})
        if r is None:
            return None
        for it in (r.get("known") or []):
            out.add(str(it.get("uid")))
    return out


def main():
    if not SHEET_ID:
        print("缺 DISCOVER_SHEET_ID", file=sys.stderr)
        return 2
    if not token():
        return 0
    # ① 拉各类目在播 → 分流
    buckets = {"casino": {}, "noncasino": {}, "skip": 0}
    ids = [(i, "casino") for i in CASINO_IDS] + [(i, "noncasino") for i in NONCASINO_IDS]
    for gid, kind in ids:
        live = live_for(gid)
        if live:
            print("[%s] %s: 在播 %d" % (kind, gid, len(live)), file=sys.stderr)
        buckets[kind].update(live)
    _scope = (os.environ.get("TWITCH_SCOPE") or "all").strip().lower()
    if _scope == "casino":
        buckets["noncasino"] = {}
    elif _scope == "noncasino":
        buckets["casino"] = {}
    print("[范围] TWITCH_SCOPE=%s" % _scope, file=sys.stderr)
    print("[分流] casino 在播 %d / 非casino 在播 %d（类目 %d 个）"
          % (len(buckets["casino"]), len(buckets["noncasino"]), len(ids)), file=sys.stderr)
    if not (buckets["casino"] or buckets["noncasino"]):
        print("在播为空 → 本轮不写", file=sys.stderr)
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
    # 2026-10-21: 去重优先查 D1（毫秒级，不再读 4 张表 10 万行）；D1 不可用时回落读表。
    _cand_uids = [str(u) for kind in ("casino", "noncasino") for u in buckets[kind]]
    _t_dedup = time.time()
    _d1k = d1_known(_cand_uids) if _cand_uids else None
    if _d1k is not None:
        seen = {"twitch:" + u for u in _d1k}
        _recon = load_recon()
        seen |= {"twitch:" + u for u in _recon}
        if _recon:
            print("[去重] 本地兜底 %d 条（D1 写失败遗留）" % len(_recon), file=sys.stderr)
        slugs_seen = set()
        print("[去重] D1 命中 %d / 候选 %d（%.1fs，未读表）"
              % (len(_d1k), len(_cand_uids), time.time() - _t_dedup), file=sys.stderr)
    else:
        seen, slugs_seen = load_seen(sh)

    # 目标表表头（动态：钓KOC 24 列、第3列叫「用户名」、多「来源」列）
    hdrs = {}
    for kind, t in TAB.items():
        for tier, nm in t.items():
            hdrs[(kind, tier)] = (nm, sh.worksheet(nm).row_values(1))

    # ② 先挑"新候选"（未登记），再批量补粉丝数（比逐个省配额）
    retry = load_retry()
    _CAND_CAP = KOL_QUOTA + KOC_QUOTA + 60
    todo = []
    st = {"slug": 0, "uid": 0}
    for kind in ("casino", "noncasino"):
        # 2026-10-21: 上一轮详情失败的 login **优先**，其余按观众数降序
        items = sorted(buckets[kind].items(),
                       key=lambda kv: (0 if (kv[1].get("login") or "") in retry else 1,
                                       -int((retry.get(kv[1].get("login") or "") or {}).get("fails", 0)),
                                       -int(kv[1].get("viewers") or 0)))
        for uid, m in items:
            if len(todo) >= _CAND_CAP:
                break
            if ("twitch:" + uid) in seen:
                st["uid"] += 1
                continue
            if m.get("login") and m["login"] in slugs_seen:
                st["slug"] += 1
                continue
            todo.append((kind, m))
    print("[去重明细] uid跳过=%d slug跳过=%d 待处理=%d（上限 %d）"
          % (st["uid"], st["slug"], len(todo), MAX_NEW), file=sys.stderr)
    if not todo:
        return 0
    info = channels_info([m["uid"] for _, m in todo])
    print("[详情] helix/channels 命中 %d/%d" % (len(info), len(todo)), file=sys.stderr)
    _failed = []
    for _kind, _m in todo:
        if _m["uid"] not in info and _m.get("login"):
            _r = retry.get(_m["login"]) or {}
            retry[_m["login"]] = {"fails": int(_r.get("fails", 0)) + 1,
                                  "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
            _failed.append({"platform": "twitch", "uid": _m["uid"],
                            "status": "failed", "last_error": "detail_miss"})
    # 2026-10-12: 批量取用户简介（联系方式提取的唯一来源）—— 一次 100 个 login，纯 API。
    descs = users_desc([m.get("login") for _, m in todo])
    print("[简介] helix/users 命中 %d/%d" % (
        sum(1 for _, m in todo if descs.get(str(m.get("login") or "").lower())), len(todo)),
        file=sys.stderr)

    # ③ 分层 + 按真实表头填行
    out_rows, n_kol, n_koc = {}, 0, 0
    _recon_fail = []
    _tier_done = {"kol": 0, "koc": 0}
    _over_quota = 0
    for kind, m in todo:
        mm = metas(m, info.get(m["uid"]), descs.get(str(m.get("login") or "").lower(), ""))
        tier = "kol" if is_kol(mm["_followers"], mm["国家"], mm["_lang_code"]) else "koc"
        _q = KOL_QUOTA if tier == "kol" else KOC_QUOTA
        if _tier_done[tier] >= _q:
            _over_quota += 1
            continue                             # 超配额：不写、不标 seen → 下一轮还会遇到
        _tier_done[tier] += 1
        n_kol += (tier == "kol")
        n_koc += (tier == "koc")
        nm, hdr = hdrs[(kind, tier)]
        row = [str(mm.get(c, "")) for c in hdr]      # ← 按真实表头取列，绝不共用一套行
        out_rows.setdefault(nm, {"hdr": hdr, "rows": [], "keys": []})
        out_rows[nm]["rows"].append(row)
        out_rows[nm]["keys"].append((m.get("login") or "", m["uid"], tier, kind))

    total = 0
    for nm, pack in out_rows.items():
        rows = pack["rows"]
        total += len(rows)
        print("[%s] 待写 %d 行（表头 %d 列）" % (nm, len(rows), len(pack["hdr"])), file=sys.stderr)
        for r in rows[:3]:
            print("     + " + " | ".join(str(x)[:14] for x in r[:9]), file=sys.stderr)
        if APPLY and rows:
            # 2026-10-09 修复：加 429 重试。本机脚本与云腿**共用同一个 service account**，
            # Sheets「读请求/分钟/用户」配额会被抢 → 之前整轮直接失败（实测 10-09 15:17 UTC 那次）。
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
            if _ok:
                for _login, _uid, _tier, _kind in pack.get("keys", []):
                    if _login:
                        slugs_seen.add(_login)
                        retry.pop(_login, None)
                    if _uid:
                        seen.add("twitch:" + _uid)
                # 2026-10-21: 写表成功 → D1 状态机标 exported（失败的下一轮靠 pending 重新取）
                _items = [{"platform": "twitch", "uid": _uid, "slug": _login,
                           "status": "exported", "grp": _tier, "vertical": _kind}
                          for (_login, _uid, _tier, _kind) in pack.get("keys", []) if _uid]
                if _items:
                    if _d1("/discover/done", {"items": _items}) is None:
                        _recon_fail.extend([u for (_lg, u, _t, _k) in pack.get("keys", []) if u])
                print("     [写入] %s +%d" % (nm, len(rows)), file=sys.stderr)
    if _over_quota:
        print("     [配额] 超 KOL/KOC 配额未写 %d 个（下一轮继续）" % _over_quota, file=sys.stderr)
    if len(retry) > 500:
        for _k, _ in sorted(retry.items(), key=lambda x: -int((x[1] or {}).get("fails", 0)))[500:]:
            retry.pop(_k, None)
    save_retry(retry)
    # 2026-10-21: D1 收尾 —— 失败项批量标 failed（自动退避），本轮指标落库。
    if _failed:
        _d1("/discover/done", {"items": _failed})
    if _recon_fail:
        save_recon(set(locals().get("_recon", set())) | set(_recon_fail))
        print("     [warn] D1 写失败 %d 条 → 本地兜底记账（下轮仍能去重）" % len(_recon_fail),
              file=sys.stderr)
    _d1("/discover/metrics", {"items": [
        {"name": "twitch.candidates", "value": len(_cand_uids)},
        {"name": "twitch.written", "value": total},
        {"name": "twitch.detail_failed", "value": len(_failed)},
        {"name": "twitch.dedup_ms", "value": int((time.time() - _t_dedup) * 1000)},
        {"name": "twitch.kol", "value": n_kol},
        {"name": "twitch.koc", "value": n_koc},
    ]})
    print("[结果] KOL=%d KOC=%d 合计 %d 行%s" % (n_kol, n_koc, total, "" if APPLY else "（dry-run 未写）"),
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
