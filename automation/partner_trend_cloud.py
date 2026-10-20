#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""partner_trend_cloud.py — 合作主播「每日快照」云版（GitHub Actions，设备关机也能跑）。

口径（对齐本机 `partner_trend_collect.py`，**勿擅自改**）：
  · **不依赖「撞上在播」** —— 只取**离线可得**的数据：
    频道公开接口的 followers + 近 6 场 VOD 的 `viewer_count`（Kick 每场自带平均观众）/ views / 时长。
  · **幂等**：同一 (平台, slug, 日期) 只写一次（写前查表里当天是否已有该 slug）。
  · 缺数据一律**留空，绝不 0 填充**（延续本项目纪律）。

云版相对本机版的差异：
  · 名单内嵌本仓库 `automation/partner_watchlist.json`（不再依赖本机文件 / sheet-server）；
  · 落点：Google 表 tab「合作快照」写**摘要行**（每主播每天 1 行）；原始明细同时打 JSONL 供 artifact；
  · 数据源：Kick 公开 API v2（curl_cffi `impersonate=chrome124`，绕过 Kick 的 CF）。

env：GSPREAD_SA_JSON（或 GSPREAD_SA 文件路径）/ DISCOVER_SHEET_ID
用法：python3 partner_trend_cloud.py --apply    # 不加 = 干跑（只打印不写表）
"""
import json
import os
import re
import sys
import time
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
WATCHLIST = os.path.join(HERE, "partner_watchlist.json")
SHEET_ID = os.environ.get("DISCOVER_SHEET_ID", "")
TAB = os.environ.get("PARTNER_SNAP_TAB", "合作快照")
APPLY = "--apply" in sys.argv
KICK_V2 = "https://kick.com/api/v2"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}

HEADERS = ["日期", "平台", "主播名", "用户名/Slug", "平台UID", "粉丝数", "是否在播",
           "近场平均观众", "最近一场时间", "近场数", "近场总时长(分)", "生成时间"]


def _ist(fmt="%Y-%m-%d %H:%M"):
    """表内时间戳统一 IST(UTC+5:30)，与云腿其它脚本一致（与 runner 时区无关）。"""
    return time.strftime(fmt, time.gmtime(time.time() + 5 * 3600 + 30 * 60))


def _get(url, timeout=20, tries=3):
    """GET → (status, json|None)。curl_cffi 模拟 chrome124 过 Kick 的 CF。

    2026-10-09：首次云端实测 17 个主播有 5 个「取不到」（分布不连续，非纯限流）——
    原实现 status!=200 时**静默**返回，无从定位。改为：非 200 记录状态码 + 退避重试
    （CF 瞬断/软限流常见，重试即可过）。
    """
    last = 0
    for i in range(max(1, tries)):
        try:
            from curl_cffi import requests as cr
            r = cr.get(url, headers=UA, impersonate="chrome124", timeout=timeout)
            if r.status_code == 200:
                return 200, (r.json() if r.text else None)
            last = r.status_code
        except Exception as e:
            last = 0
            print("  [warn] %s → %s" % (url[:80], str(e)[:60]), file=sys.stderr)
        if i + 1 < tries:
            time.sleep(1.0 + i)              # 退避 1s / 2s
    print("  [warn] %s → HTTP %s（重试 %d 次仍失败）" % (url[:90], last, tries), file=sys.stderr)
    return last, None


def snap_kick(slug):
    """Kick 频道快照：followers + 是否在播 + 近 6 场（平均观众 / views / 时长）。"""
    st, ch = _get("%s/channels/%s" % (KICK_V2, urllib.parse.quote(slug)))
    if st != 200 or not isinstance(ch, dict) or not ch.get("id"):
        return None
    st2, vids = _get("%s/channels/%s/videos" % (KICK_V2, urllib.parse.quote(slug)))
    vids = [v for v in (vids or []) if isinstance(v, dict)] if isinstance(vids, list) else []
    series = [{"vod_id": v.get("id"), "start": (v.get("start_time") or "")[:16],
               "viewer_avg": v.get("viewer_count"), "views": v.get("views"),
               "dur_min": round((v.get("duration") or 0) / 60000, 1)} for v in vids[:6]]
    return {"platform": "kick", "uid": str(ch.get("id") or ""),
            "followers": ch.get("followers_count"), "live": bool(ch.get("livestream")),
            "recent_streams": series}


# ── Twitch（Helix 官方 API + TwitchTracker）───────────────────────────────
# 2026-10-09：17 个合作主播里有 4 个其实是 **Twitch** 主播（Kick 上 404）——
#   本机版靠 detect_platform()（Kick 不存在→Twitch）分流，云版首发只实现了 Kick 半边。
_TW = {"v": "", "exp": 0.0}


def _tw_token():
    """Twitch app access token（client_credentials，缓存到过期）。"""
    if _TW["v"] and time.time() < _TW["exp"] - 120:
        return _TW["v"]
    cid, csec = os.environ.get("TWITCH_CLIENT_ID", ""), os.environ.get("TWITCH_CLIENT_SECRET", "")
    if not cid or not csec:
        print("  [warn] 缺 TWITCH_CLIENT_ID/SECRET", file=sys.stderr)
        return ""
    try:
        from curl_cffi import requests as cr
        r = cr.post("https://id.twitch.tv/oauth2/token", timeout=20, params={
            "client_id": cid, "client_secret": csec, "grant_type": "client_credentials"})
        d = r.json() or {}
        _TW["v"] = str(d.get("access_token") or "")
        _TW["exp"] = time.time() + float(d.get("expires_in") or 3600)
    except Exception as e:
        print("  [warn] twitch token 失败: %s" % str(e)[:60], file=sys.stderr)
        _TW["v"] = ""
    return _TW["v"]


def _tw_get(path):
    """Helix GET → json|None（带 Client-Id / Authorization）。"""
    tok, cid = _tw_token(), os.environ.get("TWITCH_CLIENT_ID", "")
    if not tok or not cid:
        return None
    try:
        from curl_cffi import requests as cr
        r = cr.get("https://api.twitch.tv/helix/" + path, timeout=20,
                   headers={"Client-Id": cid, "Authorization": "Bearer " + tok})
        if r.status_code != 200:
            print("  [warn] helix %s → HTTP %s" % (path[:60], r.status_code), file=sys.stderr)
            return None
        return r.json()
    except Exception as e:
        print("  [warn] helix %s → %s" % (path[:60], str(e)[:60]), file=sys.stderr)
        return None


def _dur_min(s):
    """Helix 的 duration 格式 '1h2m3s' → 分钟。"""
    t = str(s or "")

    def _g(u):
        m = re.search(r"(\d+)" + u, t)
        return int(m.group(1)) if m else 0
    return round(_g("h") * 60 + _g("m") + _g("s") / 60.0, 1)


def _tracker(slug):
    """TwitchTracker 频道摘要 —— 提供 Kick 那种「逐场平均观众」（Helix 的 videos 不带观众数）。"""
    try:
        from curl_cffi import requests as cr
        r = cr.get("https://twitchtracker.com/api/channels/summary/" + urllib.parse.quote(slug),
                   impersonate="chrome124", timeout=20, headers={"Accept": "application/json"})
        return (r.json() or {}) if (r.status_code == 200 and r.text) else {}
    except Exception:
        return {}


def snap_twitch(slug):
    """Twitch 快照：Helix(users/followers/videos/streams) + TwitchTracker(平均观众)。

    字段对齐本机 `partner_trend_collect.snap_twitch`：
    followers / live / tracker_avg_viewers / tracker_max_viewers / recent_streams。
    """
    data = ((_tw_get("users?login=" + urllib.parse.quote(slug))) or {}).get("data") or []
    if not data:
        return None
    uid = str(data[0].get("id") or "")
    f = _tw_get("channels/followers?broadcaster_id=" + uid)
    fol = (f or {}).get("total") if f is not None else None
    vids = ((_tw_get("videos?user_id=%s&first=6&type=archive" % uid)) or {}).get("data") or []
    live = bool(((_tw_get("streams?user_id=" + uid)) or {}).get("data") or [])
    trk = _tracker(slug)
    series = [{"vod_id": v.get("id"), "start": (v.get("created_at") or "")[:16],
               "views": v.get("view_count"), "dur_min": _dur_min(v.get("duration"))} for v in vids[:6]]
    return {"platform": "twitch", "uid": uid,
            "followers": fol if fol is not None else trk.get("followers_total"),
            "live": live,
            "tracker_avg_viewers": trk.get("avg_viewers"),
            "tracker_max_viewers": trk.get("max_viewers"),
            "recent_streams": series}


def main():
    if not SHEET_ID:
        print("缺 DISCOVER_SHEET_ID", file=sys.stderr)
        return 2
    try:
        wl = json.load(open(WATCHLIST, encoding="utf-8"))
    except Exception as e:
        print("读不到 watchlist: %s" % e, file=sys.stderr)
        return 1
    today, now = _ist("%Y-%m-%d"), _ist()

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
    try:
        ws = sh.worksheet(TAB)
    except Exception:
        ws = sh.add_worksheet(title=TAB, rows=2000, cols=len(HEADERS))
        ws.append_row(HEADERS)
        print("[建表] 新建 tab「%s」+ 表头" % TAB, file=sys.stderr)
    else:
        try:                                   # 表结构演进（2026-10-09 加「近场总时长(分)」列）
            _hdr = ws.row_values(1)
            if _hdr != HEADERS:
                ws.update("A1", [HEADERS])
                print("[表头] 已更新为 %d 列（原 %d 列）" % (len(HEADERS), len(_hdr)), file=sys.stderr)
        except Exception as e:
            print("  [warn] 表头校验失败: %s" % str(e)[:60], file=sys.stderr)

    # 幂等：读表已有行，收集「今天已写」的 slug
    seen_today = set()
    try:
        vals = ws.get_all_values()
        si, di = HEADERS.index("用户名/Slug"), HEADERS.index("日期")
        for r in vals[1:]:
            if len(r) > max(si, di) and r[di].strip() == today and r[si].strip():
                seen_today.add(r[si].strip().lower())
    except Exception as e:
        print("  [warn] 读已有行失败（幂等可能失效）: %s" % str(e)[:70], file=sys.stderr)

    rows, jl, skipped = [], [], 0
    for pinfo in wl:
        slug = (pinfo.get("slug") or "").strip()
        if not slug:
            continue
        if slug.lower() in seen_today:
            skipped += 1
            print("  = %-16s 今日已写，跳过" % slug, file=sys.stderr)
            continue
        plat = (pinfo.get("platform") or "").lower().strip()
        if plat not in ("kick", "twitch"):
            # 本机同款分流：Kick 上存在 → kick，否则 twitch（2026-10-09 补 Twitch 分支的原因）
            _st, _ch = _get("%s/channels/%s" % (KICK_V2, urllib.parse.quote(slug)), tries=1)
            plat = "kick" if (_st == 200 and isinstance(_ch, dict) and _ch.get("id")) else "twitch"
        d = snap_kick(slug) if plat == "kick" else snap_twitch(slug)
        if not d:
            print("  ✗ %-16s (%s) 取不到" % (slug, plat), file=sys.stderr)
            continue
        rs = d.get("recent_streams") or []
        avg = rs[0].get("viewer_avg") if rs else None
        if avg is None:
            avg = d.get("tracker_avg_viewers")     # Twitch：Helix videos 不带观众数 → 用 TwitchTracker
        dur = round(sum(float(s.get("dur_min") or 0) for s in rs), 1)   # 近场总时长(分) —— 供 #4 判「同期内容事件」
        row = [today, d.get("platform") or "kick", pinfo.get("name") or slug, slug,
               d.get("uid") or "", d.get("followers") or "", "是" if d.get("live") else "否",
               avg if avg is not None else "", (rs[0].get("start") if rs else ""), len(rs), dur, now]
        rows.append([str(x) for x in row])
        jl.append({"date": today, "ts": now, "slug": slug, "name": pinfo.get("name"), **d})
        print("  ✓ %-16s 粉丝=%-8s 在播=%-2s 近场均看=%s"
              % (slug, d.get("followers"), "是" if d.get("live") else "否", avg), file=sys.stderr)
        time.sleep(0.4)

    with open(os.path.join(HERE, "partner_trend_raw.jsonl"), "a", encoding="utf-8") as f:
        for x in jl:
            f.write(json.dumps(x, ensure_ascii=False) + "\n")

    if rows and APPLY:
        ws.append_rows(rows, value_input_option="USER_ENTERED")
        print("[写入] %s +%d 行" % (TAB, len(rows)), file=sys.stderr)
    print("[结果] 写入 %d 行 / 跳过(今日已写) %d 行%s"
          % (len(rows), skipped, "" if APPLY else "（dry-run 未写表）"), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
