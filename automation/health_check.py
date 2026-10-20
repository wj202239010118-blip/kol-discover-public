#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""health_check.py — 云端发现腿健康自检（不依赖本机、不发外部通知）

2026-10-09：把「腿坏没坏」的判断从本机（cs_health_watch / discover-gap-watchdog 读 127.0.0.1:8765
+ osascript 通知）搬到云上 —— 本机那套的盲点是「腿在云上跑，眼睛在本机」。

检查两项：
  ① 4 张发现表最新时间戳的新鲜度（默认 >12h 视为 ⚠️）
  ② GitHub Actions 各腿最近一轮 run 是否成功、是否太久没跑
输出：控制台 + 写 $GITHUB_STEP_SUMMARY（Actions 页面直接可见）。
默认**不 fail、不发通知**（只展示）；如要它 fail（触发 GitHub 原生提醒）设 HEALTH_FAIL_ON_BAD=1。
"""
import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import lark_notify  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
TABS = ["casinoKOL", "钓KOC", "非casinoKOL", "非casinoKOC"]
REPO = os.environ.get("GITHUB_REPOSITORY", "")
TOKEN = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
STALE_H = float(os.environ.get("HEALTH_STALE_H", "12"))
# 腿 → 最近一轮 run 的最大可接受间隔（小时）。kick-twitch 由 Cloudflare Worker 每小时触发。
LEGS = {"kick-twitch-discover": 3.0, "youtube-discover": 12.0, "agent-discover": 18.0}


def _sheet_last_ts():
    import gspread
    import _gspread429; _gspread429.patch()   # 429 配额自动重试（读写全覆盖）
    res = {}
    try:
        sa = os.environ.get("GSPREAD_SA_JSON")
        if sa:
            p = os.path.join(os.environ.get("RUNNER_TEMP") or "/tmp", "sa.json")
            with open(p, "w", encoding="utf-8") as f:
                f.write(sa)
            safile = p
        else:
            safile = os.environ.get("GSPREAD_SA", "")
        gc = gspread.service_account(filename=safile)
        sh = gc.open_by_key(os.environ.get("DISCOVER_SHEET_ID", ""))
    except Exception as e:
        # 连不上/配错 → 全表标记 ERR（而不是让脚本崩，那样自检本身失效）
        return {t: "ERR:%s" % str(e)[:60] for t in TABS}
    for t in TABS:
        try:
            col = [x for x in sh.worksheet(t).col_values(1)[1:] if x.strip()]
            res[t] = col[-1] if col else ""
        except Exception as e:
            res[t] = "ERR:%s" % str(e)[:60]
    return res


def _parse(ts):
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(ts.strip()[:19], fmt).replace(tzinfo=IST)
        except Exception:
            pass
    return None


def _last_runs():
    if not TOKEN or not REPO:
        return {"__err__": "无 GITHUB_TOKEN / GITHUB_REPOSITORY"}
    url = "https://api.github.com/repos/%s/actions/runs?per_page=40" % REPO
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + TOKEN,
        "Accept": "application/vnd.github+json",
        "User-Agent": "cf-discover-health"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode())
    except Exception as e:
        return {"__err__": str(e)[:80]}
    seen = {}
    for run in data.get("workflow_runs") or []:
        seen.setdefault(run.get("name") or "", run)
    return seen


def main():
    now = datetime.now(UTC)
    bad = []
    L = ["## 云端发现腿自检",
         "now: %s UTC / %s IST" % (now.strftime("%Y-%m-%d %H:%M"), now.astimezone(IST).strftime("%Y-%m-%d %H:%M"))]

    L.append("\n### 4 张发现表最新写入（阈值 %gh）" % STALE_H)
    for t, ts in _sheet_last_ts().items():
        dt = _parse(ts) if ts and not ts.startswith("ERR") else None
        if dt is None:
            L.append("- **%s**: ⚠️ 取不到（%s）" % (t, ts or "空"))
            bad.append("table:" + t)
            continue
        age = (now - dt).total_seconds() / 3600.0
        ok = age <= STALE_H
        if not ok:
            bad.append("stale:" + t)
        L.append("- %s: `%s`（%.1f h 前）%s" % (t, ts, age, "✅" if ok else "⚠️"))

    runs = _last_runs()
    L.append("\n### 各腿最近一轮 run")
    if "__err__" in runs:
        L.append("- ⚠️ 查 run 失败：%s" % runs["__err__"])
        bad.append("runs_api")
    else:
        for leg, maxh in LEGS.items():
            r = runs.get(leg)
            if not r:
                L.append("- **%s**: ⚠️ 无记录" % leg)
                bad.append("norun:" + leg)
                continue
            ct = datetime.strptime(r["created_at"][:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC)
            age = (now - ct).total_seconds() / 3600.0
            concl = r.get("conclusion") or r.get("status")
            ok = (concl in ("success", "in_progress")) and age <= maxh
            if not ok:
                bad.append("run:" + leg)
            L.append("- %s: `%s`（%.1f h 前, event=%s）%s" % (leg, concl, age, r.get("event"), "✅" if ok else "⚠️"))

    verdict = "✅ 全部正常" if not bad else "⚠️ 异常：%s" % ", ".join(bad)
    L.append("\n**%s**" % verdict)
    text = "\n".join(L)
    print(text)
    print("\nVERDICT: " + verdict)

    sp = os.environ.get("GITHUB_STEP_SUMMARY")
    if sp:
        with open(sp, "a", encoding="utf-8") as f:
            f.write(text + "\n")

    # ── Lark 风控告警（内部通知群，中文）──
    # 粒度 = 【按异常项逐条】去重：同一条只提醒一次；只有「新增异常」才发；
    # 「恢复」报一次；集合没变化则完全不打扰。状态存 .state/health_state.json
    # （由 workflow 的 actions/cache 持久化）。
    state_file = os.environ.get("HEALTH_STATE") or os.path.join(HERE, ".state", "health_state.json")
    cur_bad = set(bad)
    prev_bad = set()
    try:
        if os.path.exists(state_file):
            prev_bad = set((json.load(open(state_file, encoding="utf-8")) or {}).get("bad", []) or [])
    except Exception:
        prev_bad = set()
    new_bad = sorted(cur_bad - prev_bad)
    fixed = sorted(prev_bad - cur_bad)
    if new_bad:
        lark_notify.send("🚨 云腿风控：新增异常\n· " + "\n· ".join(new_bad)
                         + "\n\n" + text + "\n\n（每类异常只提醒一次，请人工介入）")
    if fixed:
        lark_notify.send("✅ 云腿风控：已恢复\n· " + "\n· ".join(fixed))
    if not new_bad and not fixed:
        print("[lark] 异常集合未变化，不重复发送")
    try:
        os.makedirs(os.path.dirname(state_file), exist_ok=True)
        with open(state_file, "w", encoding="utf-8") as f:
            json.dump({"bad": sorted(cur_bad), "at": now.strftime("%Y-%m-%d %H:%M:%S")}, f, ensure_ascii=False)
    except Exception as e:
        print("[state] 写入失败: %s" % e)

    if bad and os.environ.get("HEALTH_FAIL_ON_BAD") == "1":
        sys.exit(1)


if __name__ == "__main__":
    main()
