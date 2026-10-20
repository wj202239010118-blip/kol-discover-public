#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""discover_priority.py — 发现候选的「优先联系 / 暂缓联系」自动判定

为什么要有它（2026-10-20 用户）
  「这个不是我来写，我需要一个判断脚本，基于获取的各个数据维度处理」
  —— 联系人优先级不该靠人一条条点，要按数据自动分档。

判断维度（全部来自已采集字段，不额外抓取）
  viewers      观众数        —— 核心：有没有人在看
  followers    粉丝数        —— 来自发现表（可选增强；候选池没这列）
  lang         语种          —— 目标市场（es/en/pt 优先）
  section      分类          —— casino 相关优先（业务是博彩）
  seen_count   出现次数      —— 多次被扫到 = 稳定活跃，不是一次性路过
  last_seen    最近出现      —— 越新越值得联系
  first_seen   首次发现      —— 算「新鲜度」
  excluded     是否被排除    —— 已排除的直接暂缓
  已合作       查 coop_members —— 已合作的永不联系（铁律）

分档
  ≥ PRIORITY_TH   → 优先联系
  ≤ DEFER_TH      → 暂缓联系
  中间            → 留空（普通，人工判断）

用法
  python3 discover_priority.py                      # 出报告：前 20 优先 / 前 20 暂缓
  python3 discover_priority.py --top 50
  python3 discover_priority.py --platform kick --section slots
  python3 discover_priority.py --explain kurtismatt # 单个 slug 的逐维度打分
  python3 discover_priority.py --json out.json
  python3 discover_priority.py --apply --dry-run    # 看会写哪些行（不写表）
  python3 discover_priority.py --apply              # 写回「联系筛选」表

注意
  · 默认只读（不算 --apply 绝不写表）
  · 阈值都能用参数覆盖，不用改代码
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
CAND_FILE = os.environ.get("DISCOVER_CAND_FILE") or os.path.join(HERE, "discover-candidates.json")
# 2026-10-20 修正数据源：
#   coop_members.json  → 全是 TG 数字 uid（3179 条），与 platform slug 对不上，**用不了**。
#   .state/coop_master_brief_snapshot.json → 503 条，字段 puid/slug/stage/uid，**有 slug**。
#   实证：koasvo、heroutv 在后者里，不在前者里 —— 这就是"已合作却当成新候选"的根因。
COOP_SNAPSHOT = (os.environ.get("COOP_SNAPSHOT_FILE")
                 or os.path.join(HERE, ".state", "coop_master_brief_snapshot.json"))
COOP_FILE = os.path.join(HERE, "coop_members.json")

# ── 可调阈值 ────────────────────────────────────────────────────────────────
TARGET_LANGS = {"es", "en", "pt", "西班牙语", "英语", "葡萄牙语"}
CASINO_SECTIONS = {"slots", "casino", "casino-slots", "sports", "sports-betting"}

W = {           # 各维度权重
    "viewers": 40,
    "followers": 20,
    "lang": 15,
    "section": 15,
    "stability": 10,
    "freshness": 10,
}

DEFAULTS = {
    "priority_th": 55,
    "defer_th": 15,
    "active_days": 7,        # last_seen 在 N 天内 = 活跃
    "stale_days": 30,        # 超过 N 天没出现 = 过气
}


def _num(v) -> float:
    """把 "18K" / "1.2M" / "1,234" / 数字 统一成 float（候选池里粉丝数是字符串）。"""
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    t = str(v).strip().replace(",", "").replace(" ", "")
    if not t:
        return 0.0
    mult = 1.0
    if t[-1] in "kK":
        mult, t = 1e3, t[:-1]
    elif t[-1] in "mM":
        mult, t = 1e6, t[:-1]
    elif t[-1] in "bB":
        mult, t = 1e9, t[:-1]
    try:
        return float(t) * mult
    except ValueError:
        return 0.0


def _days_since(ts: str) -> float | None:
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"):
        try:
            return (datetime.now() - datetime.strptime(str(ts)[:19], fmt)).total_seconds() / 86400
        except Exception:
            continue
    return None


def load_coop() -> set:
    """已合作名单（slug 集合）。

    2026-10-20 实测：coop_members.json 里 all_members / group_members 全是 **TG 数字 uid**，
    而候选池用 **platform slug**（kurtismatt 这种），两者对不上 —— 需要 uid↔slug 映射才能用。
    所以这里只收「看起来像 slug」的（含字母），纯数字一律忽略并明确告知，
    避免把"没匹配上"误当成"没合作过"。
    """
    out: set[str] = set()
    numeric = 0
    # ① 首选：合作库快照（有 slug）
    try:
        snap = json.load(open(COOP_SNAPSHOT))
        for it in (snap.get("items") or []):
            for k in ("slug", "puid", "uid"):
                v = str(it.get(k) or "").strip().lower()
                if v and not v.isdigit():
                    out.add(v)
        if out:
            return out
    except Exception as e:
        print(f"[warn] 读不到合作库快照（{str(e)[:50]}），回落 coop_members.json", file=sys.stderr)
    # ② 兜底：coop_members.json（只有 TG uid，基本用不上）
    try:
        d = json.load(open(COOP_FILE))
        raw = []
        if isinstance(d, list):
            raw = d
        elif isinstance(d, dict):
            for key in ("members", "items", "all_members"):
                if isinstance(d.get(key), list):
                    raw += d[key]
            gm = d.get("group_members")
            if isinstance(gm, dict):
                for v in gm.values():
                    raw += (v if isinstance(v, list) else [])
        for x in raw:
            t = str(x).strip().lower()
            if not t:
                continue
            if t.isdigit():
                numeric += 1
                continue
            out.add(t)
    except Exception as e:
        print(f"[warn] 读不到已合作名单（{str(e)[:50]}）—— 这一维不参与判定", file=sys.stderr)
        return out
    if numeric and not out:
        print(f"[warn] 已合作名单 {numeric} 条全是 TG 数字 uid，与 slug 无法直接匹配 → "
              f"「已合作」这一维**不参与判定**（需要 uid↔slug 映射表才能启用）", file=sys.stderr)
    return out


def score_one(slug: str, c: dict, coop: set, cfg: dict) -> dict:
    """逐维度打分。返回 {score, verdict, reasons, dims}。"""
    dims, reasons = {}, []
    v = _num(c.get("viewers"))

    # ① 观众数（最重要）
    if v >= 500:   p = W["viewers"];      reasons.append(f"观众 {int(v)}（很强）")
    elif v >= 200: p = W["viewers"] * 0.75; reasons.append(f"观众 {int(v)}（不错）")
    elif v >= 100: p = W["viewers"] * 0.5;  reasons.append(f"观众 {int(v)}（及格）")
    elif v >= 50:  p = W["viewers"] * 0.25; reasons.append(f"观众 {int(v)}（偏少）")
    else:          p = 0;                   reasons.append(f"观众 {int(v)}（太少）")
    dims["viewers"] = round(p, 1)

    # ② 粉丝数（可选增强）
    f = _num(c.get("followers"))
    if f >= 20000:   p = W["followers"];        reasons.append(f"粉丝 {int(f)}（大号）")
    elif f >= 5000:  p = W["followers"] * 0.75; reasons.append(f"粉丝 {int(f)}（中号）")
    elif f >= 1000:  p = W["followers"] * 0.5;  reasons.append(f"粉丝 {int(f)}")
    elif f > 0:      p = W["followers"] * 0.2;  reasons.append(f"粉丝 {int(f)}（小号）")
    else:            p = 0
    dims["followers"] = round(p, 1)

    # ③ 语种
    lang = str(c.get("lang") or "").strip().lower()
    if lang in TARGET_LANGS:
        dims["lang"] = W["lang"]; reasons.append(f"语种 {lang}（目标市场）")
    else:
        dims["lang"] = 0
        if lang:
            reasons.append(f"语种 {lang}（非目标）")

    # ④ 分类
    sec = str(c.get("section") or "").strip().lower()
    if sec in CASINO_SECTIONS:
        dims["section"] = W["section"]; reasons.append(f"分类 {sec}（对口）")
    else:
        dims["section"] = 0
        if sec:
            reasons.append(f"分类 {sec}（不对口）")

    # ⑤ 稳定性：被扫到多次 = 不是路过
    sc = int(_num(c.get("seen_count")))
    if sc >= 10:   p = W["stability"];      reasons.append(f"累计出现 {sc} 次（稳定）")
    elif sc >= 3:  p = W["stability"] * 0.6; reasons.append(f"累计出现 {sc} 次")
    elif sc >= 1:  p = W["stability"] * 0.3
    else:          p = 0
    dims["stability"] = round(p, 1)

    # ⑥ 新鲜度
    d_last = _days_since(c.get("last_seen"))
    if d_last is not None:
        if d_last <= cfg["active_days"]:
            dims["freshness"] = W["freshness"]; reasons.append(f"最近 {d_last:.0f} 天有出现")
        elif d_last <= cfg["stale_days"]:
            dims["freshness"] = W["freshness"] * 0.4
        else:
            dims["freshness"] = 0; reasons.append(f"已 {d_last:.0f} 天没出现（过气）")

    total = sum(dims.values())

    # ── 硬否决（一票暂缓）────────────────────────────────────────────────
    veto = []
    if c.get("excluded"):
        veto.append(f"已被排除（{c.get('excluded_reason') or '无理由'}）")
    # key 可能是 platform:slug（11412/27364 条）—— 匹配已合作库要剥离前缀
    _bare = slug.split(":", 1)[1].lower() if ":" in slug else slug.lower()
    if _bare in coop or slug.lower() in coop:
        veto.append("已合作 —— 永不主动联系")
    if v < 50 and d_last is not None and d_last > cfg["stale_days"]:
        veto.append("观众极少且长期不活跃")

    if veto:
        return {"score": 0, "verdict": "暂缓联系", "reasons": veto + reasons[:3],
                "dims": dims, "veto": True}

    if total >= cfg["priority_th"]:
        verdict = "优先联系"
    elif total <= cfg["defer_th"]:
        verdict = "暂缓联系"
    else:
        verdict = ""
    return {"score": round(total, 1), "verdict": verdict, "reasons": reasons,
            "dims": dims, "veto": False}


def main() -> int:
    ap = argparse.ArgumentParser(description="发现候选的优先/暂缓自动判定")
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--platform")
    ap.add_argument("--section")
    ap.add_argument("--explain", help="单个 slug 逐维度打分")
    ap.add_argument("--priority-th", type=float, default=DEFAULTS["priority_th"])
    ap.add_argument("--defer-th", type=float, default=DEFAULTS["defer_th"])
    ap.add_argument("--active-days", type=int, default=DEFAULTS["active_days"])
    ap.add_argument("--stale-days", type=int, default=DEFAULTS["stale_days"])
    ap.add_argument("--json", metavar="FILE")
    ap.add_argument("--apply", action="store_true", help="写回「联系筛选」表（默认不写）")
    ap.add_argument("--dry-run", action="store_true", help="配合 --apply：只打印不写")
    a = ap.parse_args()

    cfg = {"priority_th": a.priority_th, "defer_th": a.defer_th,
           "active_days": a.active_days, "stale_days": a.stale_days}

    if not os.path.exists(CAND_FILE):
        print(f"没有候选池: {CAND_FILE}", file=sys.stderr)
        return 2
    cands = json.load(open(CAND_FILE))
    coop = load_coop()
    print(f"候选 {len(cands)} 条｜已合作名单 {len(coop)} 条｜阈值 优先≥{a.priority_th} 暂缓≤{a.defer_th}")

    rows = []
    for slug, c in cands.items():
        if a.platform and str(c.get("platform")).lower() != a.platform.lower():
            continue
        if a.section and str(c.get("section")).lower() != a.section.lower():
            continue
        r = score_one(slug, c, coop, cfg)
        rows.append({"slug": slug, **r, "platform": c.get("platform"),
                     "section": c.get("section"), "viewers": c.get("viewers"),
                     "lang": c.get("lang")})

    if a.explain:
        hit = [r for r in rows if r["slug"].lower() == a.explain.lower()]
        if not hit:
            print(f"没找到 {a.explain}"); return 1
        r = hit[0]
        print(f"\n{a.explain}  得分 {r['score']} → {r['verdict'] or '（普通）'}")
        for k, v in r["dims"].items():
            print(f"  {k:10} {v:>5}")
        print("  理由:", "；".join(r["reasons"]))
        return 0

    pri = sorted([r for r in rows if r["verdict"] == "优先联系"], key=lambda x: -x["score"])
    def_ = sorted([r for r in rows if r["verdict"] == "暂缓联系"], key=lambda x: x["score"])
    print(f"优先联系 {len(pri)}｜暂缓联系 {len(def_)}｜普通 {len(rows)-len(pri)-len(def_)}")

    print(f"\n=== 优先联系 Top {min(a.top, len(pri))} ===")
    for r in pri[:a.top]:
        print(f"  {r['score']:>5}  {r['slug'][:24]:24} {str(r['platform']):7} {str(r['section'])[:12]:12} "
              f"观众 {r['viewers']} | {'；'.join(r['reasons'][:2])}")

    print(f"\n=== 暂缓联系 Bottom {min(a.top, len(def_))} ===")
    for r in def_[:a.top]:
        print(f"  {r['slug'][:24]:24} {str(r['platform']):7} | {'；'.join(r['reasons'][:2])}")

    out = {"generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
           "cfg": cfg, "counts": {"priority": len(pri), "defer": len(def_), "total": len(rows)},
           "rows": rows}
    path = a.json or os.path.join(HERE, "logs", f"discover_priority_{datetime.now():%Y%m%d_%H%M}.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    json.dump(out, open(path, "w"), ensure_ascii=False)
    print(f"\n✅ 完整结果: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
