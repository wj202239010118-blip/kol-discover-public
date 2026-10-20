#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""daily_report.py — 云腿日报：过去 24h「各平台发现多少人写进表」

2026-10-09：由 Cloudflare Worker 每天 18:00 IST（= 12:30 UTC）触发，发到 Lark 内部通知群。
口径：只统计 **云腿 + KOL 表**：
      ① 表：只读 casinoKOL / 非casinoKOL（KOC 表 钓KOC/非casinoKOC **不计入**）；
      ② 来源：**2026-10-20 起「建联腿」已改为「建联时间」**（记发邮件建联的日期），
         原「按建联腿含 cloud 过滤」的口径作废 —— 该列现在为空表示还没发过邮件建联；
      ③ 窗口：取「时间戳」列在最近 REPORT_WINDOW_H(默认24) 小时内的行，按「平台」分组计数。
"""
import os
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from urllib.request import urlretrieve  # noqa: F401  (占位，避免误用)

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import lark_notify  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
TABS = ["casinoKOL", "非casinoKOL"]   # 2026-10-09: 只统计 KOL 表（KOC 表不计入日报）
WINDOW_H = float(os.environ.get("REPORT_WINDOW_H", "24"))


def _parse_ist(s):
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(str(s).strip()[:19], fmt).replace(tzinfo=IST)
        except Exception:
            pass
    return None


def _sheet():
    import gspread
    import _gspread429; _gspread429.patch()   # 429 配额自动重试（读写全覆盖）
    sa = os.environ.get("GSPREAD_SA_JSON")
    if sa:
        p = os.path.join(os.environ.get("RUNNER_TEMP") or "/tmp", "sa.json")
        with open(p, "w", encoding="utf-8") as f:
            f.write(sa)
        path = p
    else:
        path = os.environ.get("GSPREAD_SA", "")
    gc = gspread.service_account(filename=path)
    return gc.open_by_key(os.environ.get("DISCOVER_SHEET_ID", ""))


def main():
    now = datetime.now(IST)
    cutoff = now - timedelta(hours=WINDOW_H)
    sh = _sheet()
    per_platform = Counter()
    per_table = Counter()
    for t in TABS:
        try:
            ws = sh.worksheet(t)
            header = ws.row_values(1)
            # 2026-10-20：「建联腿」已改为「建联时间」（空=还没发邮件建联），
            #   不再读这一列 —— 日报改为统计**全部新增**，不按腿过滤。
            ts_col = ws.col_values(1)[1:]
            plat_col = ws.col_values(2)[1:]
        except Exception as e:
            print("[report] 读表失败 %s: %s" % (t, e), file=sys.stderr)
            continue
        n = 0
        for i, ts in enumerate(ts_col):
            dt = _parse_ist(ts)
            if dt and dt >= cutoff:
                plat = (plat_col[i] if i < len(plat_col) else "").strip() or "未知"
                per_platform[plat] += 1
                n += 1
        per_table[t] = n

    total = sum(per_platform.values())
    lines = ["📊 KOL 日报（过去 %gh 新增入表）" % WINDOW_H,
             "统计时间：%s (IST)" % now.strftime("%Y-%m-%d %H:%M"),
             ""]
    if total == 0:
        lines.append("⚠️ 过去 %gh 没有任何新增 —— 建议看下 health-check 或云端 run。" % WINDOW_H)
    else:
        for plat, c in per_platform.most_common():
            lines.append("· %s：%d 人" % (plat, c))
        lines.append("")
        lines.append("合计 %d 人入表" % total)
        lines.append("（分表：%s）" % " / ".join("%s %d" % (k, v) for k, v in per_table.items()))
    text = "\n".join(lines)
    print(text)
    ok = lark_notify.send(text)
    print("[report] lark 发送:", ok)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
