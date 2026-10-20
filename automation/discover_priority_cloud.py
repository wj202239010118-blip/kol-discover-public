#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""discover_priority_cloud.py — 候选优先级判定的**云端版**（Kick + Twitch 都看）

为什么要有它（2026-10-20 用户）
  「discover_priority 这个肯定上云啊，否则我电脑都关了就操作不了了」
  「kick/twitch 都要看啊」

和本机版的差别
  本机版读 15MB `discover-candidates.json`（Secret 上限 48KB，放不进去）。
  云端版**直接从 4 张发现表读**——观众数/粉丝数/语种/分类/时间戳表里都有，
  用不上候选池也能打分（少一维 seen_count，稳定性改用「近 N 天出现」代替）。

判据（与 discover_priority.py 同一套权重）
  viewers 40 / followers 20 / lang 15 / section 15 / stability 10 / freshness 10
  硬否决：已被排除、已合作（合作库 Secret）、观众极少且长期不活跃

平台
  **Kick 与 Twitch 一视同仁** —— 4 张发现表里两个平台的行走同一条判定。

用法（云端由 workflow 调用；本机也能跑，用于验证）
  python3 automation/discover_priority_cloud.py --dry-run          # 只出报告
  python3 automation/discover_priority_cloud.py --apply            # 写回发现表「状态」列
  python3 automation/discover_priority_cloud.py --platform kick
  python3 automation/discover_priority_cloud.py --limit 500 --days 30

env（Secrets）
  GSPREAD_SA(GSPREAD_SA_JSON 落地文件) / DISCOVER_SHEET_ID / COOP_SNAPSHOT_JSON(可选)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

# 打分逻辑复用本机版（同一套权重，避免两处漂移）
try:
    from discover_priority import score_one, TARGET_LANGS, CASINO_SECTIONS
except Exception:                                    # 兜底：自己带一份最小实现
    TARGET_LANGS = {"es", "en", "pt", "西班牙语", "英语", "葡萄牙语"}
    CASINO_SECTIONS = {"slots", "casino", "casino-slots", "sports", "sports-betting"}

    def score_one(slug, c, coop, cfg):               # type: ignore
        v = float(str(c.get("viewers") or 0).replace("K", "000") or 0)
        f = float(str(c.get("followers") or 0).replace("K", "000") or 0)
        lang = str(c.get("lang") or "").lower()
        sec = str(c.get("section") or "").lower()
        s = 0
        s += 40 if v >= 500 else 30 if v >= 200 else 20 if v >= 100 else 10 if v >= 50 else 0
        s += 20 if f >= 20000 else 15 if f >= 5000 else 10 if f >= 1000 else 4 if f > 0 else 0
        s += 15 if lang in TARGET_LANGS else 0
        s += 15 if sec in CASINO_SECTIONS else 0
        reasons = [f"观众 {int(v)}", f"粉丝 {int(f)}", f"语种 {lang}", f"分类 {sec}"]
        if slug.lower() in coop:
            return {"score": 0, "verdict": "暂缓联系", "reasons": ["已合作 —— 永不主动联系"],
                    "dims": {}, "veto": True}
        verdict = "优先联系" if s >= cfg["priority_th"] else ("暂缓联系" if s <= cfg["defer_th"] else "")
        return {"score": s, "verdict": verdict, "reasons": reasons, "dims": {}, "veto": False}

TABLES = ["casinoKOL", "钓KOC", "非casinoKOL", "非casinoKOC"]
COLS = ["平台", "主播名", "用户名/Slug", "分类", "语种", "观众数", "粉丝数", "时间戳", "状态"]
DEFAULT = {"priority_th": 55, "defer_th": 15, "active_days": 7, "stale_days": 30}


def _num(v) -> float:
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    t = str(v).strip().replace(",", "").replace(" ", "")
    if not t:
        return 0.0
    mult = 1.0
    if t and t[-1] in "kK":
        mult, t = 1e3, t[:-1]
    elif t and t[-1] in "mM":
        mult, t = 1e6, t[:-1]
    try:
        return float(t) * mult
    except ValueError:
        return 0.0


def _days(ts: str) -> float | None:
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"):
        try:
            return (datetime.now() - datetime.strptime(str(ts)[:19], fmt)).total_seconds() / 86400
        except Exception:
            continue
    return None


def load_coop() -> set:
    """已合作 slug 集合 —— 优先 Secret 注入的 COOP_SNAPSHOT_JSON，其次本地文件。"""
    raw = os.environ.get("COOP_SNAPSHOT_JSON")
    data = None
    if raw:
        try:
            data = json.loads(raw)
        except Exception as e:
            print(f"[warn] COOP_SNAPSHOT_JSON 解析失败: {str(e)[:60]}", file=sys.stderr)
    if data is None:
        p = os.environ.get("COOP_SNAPSHOT_FILE") or os.path.join(HERE, ".state", "coop_master_brief_snapshot.json")
        try:
            data = json.load(open(p))
        except Exception:
            print("[warn] 拿不到合作库 → 「已合作」这一维不参与判定", file=sys.stderr)
            return set()
    out = set()
    for it in (data.get("items") or []):
        v = str(it.get("slug") or "").strip().lower()
        if v and not v.isdigit():
            out.add(v)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="候选优先级判定（云端，Kick+Twitch）")
    ap.add_argument("--apply", action="store_true", help="写回发现表的「状态」列")
    ap.add_argument("--dry-run", action="store_true", help="只出报告（默认）")
    ap.add_argument("--platform", help="kick / twitch（默认两个都看）")
    ap.add_argument("--limit", type=int, default=0, help="每表最多处理多少行（0=全部）")
    ap.add_argument("--days", type=int, default=0, help="只看最近 N 天的行（0=不限）")
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--priority-th", type=float, default=DEFAULT["priority_th"])
    ap.add_argument("--defer-th", type=float, default=DEFAULT["defer_th"])
    ap.add_argument("--json", metavar="FILE")
    a = ap.parse_args()

    cfg = {"priority_th": a.priority_th, "defer_th": a.defer_th,
           "active_days": DEFAULT["active_days"], "stale_days": DEFAULT["stale_days"]}

    sid = os.environ.get("DISCOVER_SHEET_ID", "")
    sa = os.environ.get("GSPREAD_SA") or ".secrets/sa.json"
    if not sid:
        print("❌ 缺 DISCOVER_SHEET_ID", file=sys.stderr)
        return 1
    import gspread
    try:
        import _gspread429; _gspread429.patch()
    except Exception:
        pass
    sh = gspread.service_account(filename=sa).open_by_key(sid)

    coop = load_coop()
    print(f"已合作库 {len(coop)} 条｜阈值 优先≥{a.priority_th} 暂缓≤{a.defer_th}"
          f"｜平台 {a.platform or 'Kick + Twitch'}")

    all_rows = []
    for nm in TABLES:
        try:
            ws = sh.worksheet(nm)
            hdr = ws.row_values(1)
        except Exception as e:
            print(f"⚠️ 打不开 {nm}: {str(e)[:60]}", file=sys.stderr)
            continue
        if not hdr:
            continue
        cols = {}
        for cname in COLS:
            if cname in hdr:
                try:
                    cols[cname] = ws.col_values(hdr.index(cname) + 1)
                except Exception:
                    cols[cname] = []
        if "用户名/Slug" not in cols:
            print(f"⚠️ {nm} 缺「用户名/Slug」列 → 跳过", file=sys.stderr)
            continue

        def g(cname, i):
            v = cols.get(cname) or []
            return v[i] if i < len(v) else ""

        n = max((len(v) for v in cols.values()), default=0)
        per_tab = 0
        for i in range(1, n):
            plat = g("平台", i).strip()
            if a.platform and plat.lower() != a.platform.lower():
                continue
            slug = g("用户名/Slug", i).strip()
            if not slug:
                continue
            d = _days(g("时间戳", i))
            if a.days and (d is None or d > a.days):
                continue
            cand = {"platform": plat, "viewers": _num(g("观众数", i)),
                    "followers": _num(g("粉丝数", i)), "lang": g("语种", i).strip().lower(),
                    "section": g("分类", i).strip().lower(),
                    "last_seen": g("时间戳", i), "seen_count": 0, "excluded": False}
            r = score_one(slug, cand, coop, cfg)
            all_rows.append({"slug": slug, "tab": nm, "row": i + 1, "platform": plat,
                             "name": g("主播名", i), "viewers": int(cand["viewers"]),
                             "followers": int(cand["followers"]), **r})
            per_tab += 1
            if a.limit and per_tab >= a.limit:
                break
        print(f"  [{nm}] 处理 {per_tab} 行")

    pri = [r for r in all_rows if r["verdict"] == "优先联系"]
    dfr = [r for r in all_rows if r["verdict"] == "暂缓联系"]
    print(f"\n共 {len(all_rows)} 行 → 优先 {len(pri)}｜暂缓 {len(dfr)}｜普通 {len(all_rows)-len(pri)-len(dfr)}")
    by_plat = {}
    for r in pri:
        by_plat[r["platform"]] = by_plat.get(r["platform"], 0) + 1
    print("优先分布:", by_plat)

    print(f"\n=== 优先联系 Top {min(a.top, len(pri))} ===")
    for r in sorted(pri, key=lambda x: -x["score"])[:a.top]:
        print(f"  {r['score']:>5}  {r['slug'][:22]:22} {r['platform']:7} {r['tab']:12} "
              f"观众 {r['viewers']:>6} 粉丝 {r['followers']:>7}")

    out = {"at": datetime.now().strftime("%Y-%m-%d %H:%M"), "cfg": cfg,
           "counts": {"total": len(all_rows), "priority": len(pri), "defer": len(dfr),
                      "by_platform": by_plat}, "rows": all_rows}
    path = a.json or os.path.join(HERE, "logs", f"priority_cloud_{datetime.now():%Y%m%d_%H%M}.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    json.dump(out, open(path, "w"), ensure_ascii=False)
    print(f"\n✅ 结果: {path}")

    if not a.apply:
        print("（干跑；加 --apply 才写回发现表）")
        return 0
    # 写回「状态」列：只写「优先联系 / 暂缓联系」两档，普通不动
    import gspread.utils
    written = 0
    for nm in TABLES:
        pairs = [(r["row"], r["verdict"]) for r in all_rows if r["tab"] == nm and r["verdict"]]
        if not pairs:
            continue
        ws = sh.worksheet(nm)
        hdr = ws.row_values(1)
        if "状态" not in hdr:
            print(f"⚠️ {nm} 没有「状态」列 → 跳过写回", file=sys.stderr)
            continue
        j = hdr.index("状态") + 1
        for k in range(0, len(pairs), 200):
            batch = [{"range": gspread.utils.rowcol_to_a1(rn, j), "values": [[v]]}
                     for rn, v in pairs[k:k + 200]]
            try:
                ws.batch_update(batch, value_input_option="USER_ENTERED")
                written += len(batch)
            except Exception as e:
                print(f"  ⚠️ [{nm}] 写失败: {str(e)[:70]}", file=sys.stderr)
    print(f"▶ 已写 {written} 处「状态」")
    return 0


if __name__ == "__main__":
    sys.exit(main())
