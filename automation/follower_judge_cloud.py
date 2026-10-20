#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""follower_judge_cloud.py — 合作主播「粉丝突增判定」云版（#4）。

口径（**照搬本机 follower_history.py，勿改**）：
  · 7 天窗口内 **涨幅 ≥ SPIKE_PCT(15%)** 且 **同期内容事件少**
    （VOD 场次 < NO_EVENT_VODS(2) **或** 直播时长 < NO_EVENT_HOURS(3h)）
    → 命中「粉丝突增但无对应内容事件」。
  · 样本 < 2 天 → **不判**（spike=None，延续本项目「不猜」纪律）。
  · 缺数据留空，**绝不 0 填充**。

数据源（云上自洽，不再打平台 —— 复用 #3 的产出）：
  ① 种子：`automation/follower_history_seed.jsonl`（本机 12 天历史，一次性起点）
  ② 云表 tab「合作快照」（#3 每日产出）：vods=「近场数」、hours=「近场总时长(分)」/60、
     viewers=「近场平均观众」（与本机 ingest_snapshot 的映射一致）
落点：tab「粉丝突增」写**命中行**（未命中只在日志里，保持表干净）。

env：GSPREAD_SA_JSON（或 GSPREAD_SA）/ DISCOVER_SHEET_ID
用法：python3 follower_judge_cloud.py --apply
"""
import json
import os
import sys
import time
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
SEED = os.path.join(HERE, "follower_history_seed.jsonl")
SHEET_ID = os.environ.get("DISCOVER_SHEET_ID", "")
TAB_SNAP = os.environ.get("PARTNER_SNAP_TAB", "合作快照")     # #3 的产出
TAB = os.environ.get("FOLLOWER_JUDGE_TAB", "粉丝突增")
APPLY = "--apply" in sys.argv
DAYS = int(os.environ.get("SPIKE_DAYS", "7"))

SPIKE_PCT = 0.15          # 7 天涨幅阈值（本机同名常量）
NO_EVENT_VODS = 2         # 同期 VOD 场次 < 2 → 「无对应内容事件」
NO_EVENT_HOURS = 3.0      # 同期直播时长 < 3h → 同上

HEADERS = ["判定日", "平台", "主播名", "用户名/Slug", "粉丝首", "粉丝末", "涨幅%",
           "同期场次", "同期时长(h)", "窗口起", "窗口止", "判定", "原因"]


def _ist(fmt="%Y-%m-%d"):
    return time.strftime(fmt, time.gmtime(time.time() + 5 * 3600 + 30 * 60))


def _load_seed():
    """种子历史：{slug: [{date, followers, vods, hours}]}。"""
    out = {}
    try:
        for line in open(SEED, encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            s = (r.get("slug") or "").strip()
            if s and r.get("date"):
                out.setdefault(s, []).append(r)
    except Exception as e:
        print("  [warn] 读种子失败: %s" % str(e)[:60], file=sys.stderr)
    return out


def _load_sheet(ws):
    """从「合作快照」表读历史 → 与种子同构（vods=近场数、hours=近场总时长(分)/60）。"""
    out = {}
    try:
        vals = ws.get_all_values()
        hdr = vals[0] if vals else []

        def col(name):
            return hdr.index(name) if name in hdr else -1
        c_date, c_plat, c_slug = col("日期"), col("平台"), col("用户名/Slug")
        c_fol, c_vods, c_dur = col("粉丝数"), col("近场数"), col("近场总时长(分)")
        if min(c_date, c_slug, c_fol) < 0:
            print("  [warn] 快照表缺少必需列，跳过表数据", file=sys.stderr)
            return out
        for r in vals[1:]:
            if len(r) <= max(c_date, c_slug, c_fol) or not r[c_slug].strip() or not r[c_date].strip():
                continue

            def g(i):
                return r[i].strip() if (0 <= i < len(r)) else ""
            d = g(c_date)
            slug = g(c_slug)
            fol = None
            try:
                fol = float(g(c_fol)) if g(c_fol) else None
            except Exception:
                fol = None
            vods, dur = None, None
            try:
                vods = int(float(g(c_vods))) if g(c_vods) else None
            except Exception:
                vods = None
            try:
                dur = float(g(c_dur)) if g(c_dur) else None
            except Exception:
                dur = None
            out.setdefault(slug, []).append({"date": d, "platform": g(c_plat), "followers": fol,
                                             "vods": vods, "hours": round(dur / 60.0, 1) if dur else None})
    except Exception as e:
        print("  [warn] 读快照表失败: %s" % str(e)[:70], file=sys.stderr)
    return out


def _merge(*maps):
    """合并多来源历史：[{...}] 按 (date) 去重（表覆盖种子 —— 表是最新采集）。"""
    out = {}
    for m in maps:
        for slug, rows in (m or {}).items():
            by_date = {r["date"]: r for r in out.get(slug, [])}
            for r in rows:
                by_date[r["date"]] = r
            out[slug] = list(by_date.values())
    for slug in out:
        out[slug].sort(key=lambda r: r.get("date") or "")
    return out


def growth(rows, days=DAYS):
    """窗口内增长 → {first,last,delta,pct,vods,hours,points,from,to}；样本 <2 或首值 ≤0 → None。"""
    cut = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    win = [r for r in rows if (r.get("date") or "") >= cut]
    if len(win) < 2:
        return None
    f0 = win[0].get("followers") or 0
    f1 = win[-1].get("followers") or 0
    if f0 <= 0:
        return None
    return {"first": f0, "last": f1, "delta": f1 - f0, "pct": round((f1 - f0) / f0, 4),
            "vods": sum(int(r.get("vods") or 0) for r in win),
            "hours": round(sum(float(r.get("hours") or 0) for r in win), 1),
            "points": len(win), "from": win[0].get("date"), "to": win[-1].get("date")}


def check_spike(rows, days=DAYS):
    """照搬本机 check_spike：样本不足 spike=None（不猜）。"""
    g = growth(rows, days)
    if not g:
        return {"spike": None, "reason": "粉丝历史样本不足（窗口内 <2 天数据点）", "growth": None}
    bad = (g["pct"] >= SPIKE_PCT) and (g["vods"] < NO_EVENT_VODS or g["hours"] < NO_EVENT_HOURS)
    return {"spike": bool(bad), "growth": g,
            "reason": ("粉丝 %+d（%.1f%%）同期仅 %d 场 / %.1fh 直播 → 突增无对应内容事件"
                       % (g["delta"], g["pct"] * 100, g["vods"], g["hours"])) if bad
                      else ("粉丝 %+d（%.1f%%），同期 %d 场 / %.1fh"
                            % (g["delta"], g["pct"] * 100, g["vods"], g["hours"]))}


def main():
    if not SHEET_ID:
        print("缺 DISCOVER_SHEET_ID", file=sys.stderr)
        return 2
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
        snap_ws = sh.worksheet(TAB_SNAP)
    except Exception as e:
        print("读不到快照表「%s」: %s" % (TAB_SNAP, str(e)[:80]), file=sys.stderr)
        return 1

    hist = _merge(_load_seed(), _load_sheet(snap_ws))
    today = _ist()
    rows, hits = [], 0
    for slug in sorted(hist):
        r = check_spike(hist[slug])
        g = r.get("growth")
        st = {None: "样本不足", True: "突增无内容事件", False: "正常"}[r.get("spike")]
        print("  %-18s %-10s %s" % (slug, st, r.get("reason")), file=sys.stderr)
        if r.get("spike"):
            hits += 1
            rows.append([today, (hist[slug][-1].get("platform") or ""), "", slug,
                         g["first"], g["last"], round(g["pct"] * 100, 1), g["vods"], g["hours"],
                         g["from"], g["to"], "突增无内容事件", r.get("reason")])
    print("[判定] 主播 %d 个 / 命中 %d 个" % (len(hist), hits), file=sys.stderr)

    if rows and APPLY:
        try:
            ws = sh.worksheet(TAB)
        except Exception:
            ws = sh.add_worksheet(title=TAB, rows=2000, cols=len(HEADERS))
            ws.append_row(HEADERS)
            print("[建表] 新建 tab「%s」+ 表头" % TAB, file=sys.stderr)
        ws.append_rows([[str(x) for x in r] for r in rows], value_input_option="USER_ENTERED")
        print("[写入] %s +%d 行" % (TAB, len(rows)), file=sys.stderr)
    print("[结果] 命中 %d / 共 %d%s" % (hits, len(hist), "" if APPLY else "（dry-run 未写）"), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
