#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fill_contacts_cloud.py — 云上「联系方式」补全腿（**并进 kick-twitch.yml**，不额外占额度）。

同一条腿的本机版是 `~/scripts/fill-contacts.py`。云上这份解决三件事：

  · **设备关机也跑**（GitHub Actions）—— 本机那版是 nohup loop，不跨重启；
  · **不额外占 Actions 分钟额度**：GitHub 私有仓库免费 2000 分钟/月，`kick-twitch.yml`
    每小时跑已吃 1440 分钟 —— 所以**不新开定时 workflow**，只在同一个 job 里多跑一步；
  · 表里新写的空行下一轮（≤1h）就被补上。

## 做什么

扫 4 张发现表 → 挑「联系方式」为空的全部行（2026-10-20 用户：不看任何标记，
因为我们自己的网红全都要补；已有邮箱/TG/WA 的自动跳过）：

  · Twitch → `/helix/users` 的 `description`（批量 100 个 login/次）→ 正则提邮箱/TG/WA
  · 只补空、绝不覆盖；按行号批量写回

**排序很关键**：候选按入库时间【新→旧】排序，`--limit` 截断时优先保住最新的行 ——
否则会像首版那样一直卡在表头部的老行（那些基本是提不到的），新行永远轮不到。
老行由本机 `fill-contacts.py`（有 `fill_contacts_tried.json` 记账）兜底。

## 为什么限定这几个条件（2026-10-12 实测，见 ~/scripts/fill-contacts.py 文件头）

| 来源腿 | description 有邮箱 | 处理 |
|---|---|---|
| twitch-cloud | **40%** | ✅ 补 |
| streamscharts-free | 27% | ✅ 补 |
| 邮件腿 / 漏出 | **0%**（discover-runner 落表时就提取过了） | ❌ 跳过 |
| kick（任意腿） | **0%**（Kick API 的 user.bio 里就没有邮箱；实测 0/30） | ❌ 不处理 |

云端是无状态的（每次 Actions 都是新容器），没有本机那份 `fill_contacts_tried.json`，
所以用**时间戳窗口**（`--days`）+ **新→旧排序**把范围收住。

env（Secrets，与 kick-twitch.yml 相同）：
  GSPREAD_SA / DISCOVER_SHEET_ID / TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET
用法：python3 automation/fill_contacts_cloud.py [--apply] [--limit 800] [--days 3]
"""
import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import twitch_discover_cloud as T          # 复用 token()/hdr()/_json()/users_desc()/RE_*

TABLES = ["casinoKOL", "钓KOC", "非casinoKOL", "非casinoKOC"]
KOC_TABS = {"钓KOC", "非casinoKOC"}   # 2026-10-21(用户: KOC 慢慢来): 排序时 KOC 表后置，KOL 先消化
COL = "联系方式"
LEG_COL = "建联时间"      # 2026-10-20 改造：原「建联腿」→「建联时间」（记发邮件建联的日期）
TS_COL = "时间戳"
DEFAULT_LEGS = ""          # 2026-10-20：不再按腿筛，留空=不过滤


def extract_contact(bio):
    """简介文本 → 「联系方式」列的值。

    ⚠️ 格式必须与 `discover-runner.extract_contact_and_socials` / `~/scripts/fill-contacts.py`
    一致（表里既有样本就是 `邮箱: mailto:x@y`）—— 别改用裸邮箱，否则同列两种写法。
    """
    bio = bio or ""
    parts = []
    m = T.RE_EMAIL.search(bio)
    if m:
        parts.append("邮箱: mailto:%s" % m.group(0))
    m = T.RE_TG.search(bio)
    if m:
        parts.append("TG: https://t.me/%s" % m.group(1))
    m = T.RE_WA.search(bio)
    if m:
        parts.append("WA: https://wa.me/%s" % m.group(1))
    return ", ".join(parts)


def _try_imports():
    """按需加载三路增强模块（缺哪个就跳过哪路，不影响主线）。"""
    mods = {}
    for name in ("x_profile_fetch", "yt_desc_fetch", "kol_identity"):
        try:
            mods[name] = __import__(name)
        except Exception as e:
            print("  [warn] %s 不可用: %s" % (name, str(e)[:60]), file=sys.stderr)
    return mods


def _social_contact(bio):
    """2026-10-21(用户): 提不到邮箱/TG/WA 时，把 bio 里的社交主页当成「联系路径」写进联系方式。
    IG / X / TikTok / YouTube / Facebook 都是能找到人的入口 —— 运营点进去就能私信。"""
    try:
        import kol_identity as _ki
        links = _ki.parse_links(bio or "")
    except Exception:
        return ""
    out = []
    for k, pref in (("twitter", "X"), ("instagram", "IG"), ("tiktok", "TikTok"),
                    ("youtube", "YouTube"), ("facebook", "FB")):
        v = links.get(k) or []
        if v:
            out.append("%s: %s" % (pref, str(v[0]).lstrip("@")))
    return " / ".join(out[:3])


def _social_bio(kind, handle):
    """抓 IG / TikTok 的主页 bio（未登录可见的部分）。

    2026-10-21(用户): "IG / X / TikTok / YouTube / Facebook 都是能找到人的路径" ——
    原来只做了 X 和 YouTube，这里补 IG / TikTok（Facebook 有登录墙，跳过）。
    只用公开页面的 og:description / meta description，不登录、不绕墙。
    返回 (bio 文本, 是否确认是同一个人)。
    """
    import re as _re
    try:
        from curl_cffi import requests as _cr
    except Exception:
        return "", False
    h = str(handle or "").strip().lstrip("@")
    if not h:
        return "", False
    url = ("https://www.instagram.com/%s/" % h) if kind == "ig" else \
          ("https://www.tiktok.com/@%s" % h)
    try:
        r = _cr.get(url, impersonate="chrome124", timeout=12,
                    headers={"accept-language": "en-US,en;q=0.9"})
        if r.status_code != 200:
            return "", False
        html = r.text or ""
        if h.lower() not in html.lower():
            return "", False          # 页面里没有这个 handle → 不是同一个人
        out = []
        for pat in (r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']+)',
                    r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)'):
            m = _re.search(pat, html, _re.I)
            if m:
                out.append(m.group(1))
        return " ".join(dict.fromkeys(out))[:1200], True
    except Exception:
        return "", False


def deep_enrich(slug, bio, mods, budget_s=25.0):
    """第一路（Twitch 简介）没提到时，走二/三路再试一次。

    2026-10-20 合并自 contact_enrich.py（借鉴 nanoinfluencer 的 Email Match 三路）：
      · X bio          —— fxTwitter 公开接口，免登录，最快
      · YouTube 视频描述 —— 很多主播把商务邮箱放这里，比 About 页更全（要 yt-dlp，慢）
      · link-in-bio    —— Linktree 那类聚合页

    返回：与 extract_contact 同格式的联系方式串（拿不到返回空串）。
    budget_s：单行总预算，超时就放弃 —— 云端 Actions 按分钟计费，不能被单行拖死。
    """
    import time as _t
    t0 = _t.time()
    texts = []

    # 从 bio 里找各平台 handle（不联网）
    identity = mods.get("kol_identity")
    links = {}
    if identity:
        try:
            links = identity.parse_links(bio or "")
        except Exception:
            links = {}

    # ② X bio
    xf = mods.get("x_profile_fetch")
    x_handles = (links.get("twitter") or links.get("x") or [])
    # 2026-10-21: bio 里常常没写 X 链接，但主播的 X handle 多数与 Twitch slug 同名
    #   （kaicenat / trainwreckstv / xqc 都是）。没有链接时用 slug 猜一次，
    #   并要求返回的 screen_name 与 slug 完全一致 —— 否则就是抓到同名路人，直接丢弃。
    if xf and not x_handles and slug:
        x_handles = [slug]
    if xf and x_handles and (_t.time() - t0) < budget_s:
        try:
            _h = str(x_handles[0]).lstrip("@")
            j = xf.fetch(_h)
            _u = (j.get("user") or {})
            if str(_u.get("screen_name") or "").lower() == _h.lower():
                desc = (_u.get("description") or "")
                if desc:
                    texts.append(desc)
        except Exception:
            pass

    # ③ YouTube 视频描述（慢，只在还有预算时做）
    yd = mods.get("yt_desc_fetch")
    yt = links.get("youtube") or []
    if yd and yt and (_t.time() - t0) < budget_s * 0.6:
        try:
            url = yt[0] if str(yt[0]).startswith("http") else \
                "https://www.youtube.com/@%s/videos" % str(yt[0]).lstrip("@")
            desc = yd.fetch_desc(url, 2)
            if desc and not str(desc).startswith("__ERR__"):
                texts.append(desc)
        except Exception:
            pass

    # ④ link-in-bio 聚合页（可能含邮箱）
    ki = mods.get("kol_identity")
    if ki and (_t.time() - t0) < budget_s * 0.8:
        try:
            page = ki.expand(bio or "", do_fetch=True, gap=0.0)
            for k, v in (page or {}).items():
                if isinstance(v, list):
                    texts.append(" ".join(str(x) for x in v))
        except Exception:
            pass

    # ⑤ IG / TikTok（2026-10-21）：bio 里没链接时用 slug 猜 handle，抓到 og:description 再提邮箱。
    for _kind, _known in (("ig", (links.get("instagram") or [None])[0]),
                          ("tt", (links.get("tiktok") or [None])[0])):
        if (_t.time() - t0) >= budget_s * 0.9:
            break
        _h = _known or slug
        if not _h:
            continue
        _txt, _same = _social_bio(_kind, _h)
        if _txt and _same:
            texts.append(_txt)

    for t in texts:
        c = extract_contact(t)
        if c:
            return c
    return ""


def _ts_epoch(ts):
    """表内「时间戳」是本地 IST(UTC+5:30) 的 '%Y-%m-%d %H:%M'；容器是 UTC —— 差 5.5h，
    天级窗口下不构成问题。解析不了返回 None（调用方会当作超期丢弃）。"""
    try:
        return time.mktime(time.strptime(str(ts).strip(), "%Y-%m-%d %H:%M"))
    except Exception:
        return None


# ── 2026-10-21: D1 状态（只记"已补到联系方式"，供下游建联队列直接查）──
D1_API = (os.environ.get("DISCOVER_API") or "").rstrip("/")
D1_TOK = os.environ.get("DISCOVER_TOKEN") or ""


def _d1_contact(items):
    if not D1_API or not D1_TOK or not items:
        return
    try:
        req = urllib.request.Request(D1_API + "/discover/contact_done",
            data=json.dumps({"items": items}).encode(),
            headers={"Authorization": "Bearer " + D1_TOK,
                     "Content-Type": "application/json", "User-Agent": "fill-contacts/1.0"})
        urllib.request.urlopen(req, timeout=30).read()
    except Exception as e:
        print("  [warn] D1 contact_done 失败: %s" % str(e)[:70], file=sys.stderr)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真写表（默认干跑）")
    ap.add_argument("--limit", type=int, default=800, help="本轮最多补多少行")
    ap.add_argument("--days", type=int, default=3, help="只看最近 N 天入库的行（云端无 tried 状态）")
    ap.add_argument("--legs", default=DEFAULT_LEGS,
                    help="（已废弃）原来按建联腿筛行；现在默认不过滤，所有联系方式为空的行都补")
    ap.add_argument("--deep-limit", type=int, default=20,
                    help="--deep 时最多深度抓多少行（每行可能几秒，控 Actions 分钟数）")
    ap.add_argument("--deep", action="store_true",
                    help="第一路没提到时，追加 X bio / YouTube 描述 / link-in-bio（慢，默认关）")
    a = ap.parse_args()

    legs_filter = [] if a.legs.strip().lower() == "all" else \
        [s.strip() for s in a.legs.split(",") if s.strip()]

    sheet_id = os.environ.get("DISCOVER_SHEET_ID", "")
    sa_file = os.environ.get("GSPREAD_SA", ".secrets/sa.json")
    if not sheet_id:
        print("❌ 缺 DISCOVER_SHEET_ID", file=sys.stderr)
        return 1

    import gspread
    import _gspread429; _gspread429.patch()   # 429 配额自动重试
    sh = gspread.service_account(filename=sa_file).open_by_key(sheet_id)

    todo, skipped_leg, skipped_old = [], 0, 0
    for nm in TABLES:
        try:
            ws = sh.worksheet(nm)
        except Exception as e:
            print("⚠️ 打不开 %s: %s" % (nm, str(e)[:60]), file=sys.stderr)
            continue
        try:
            hdr = ws.row_values(1)
        except Exception as e:
            print("⚠️ [%s] 读表头失败: %s" % (nm, str(e)[:60]), file=sys.stderr)
            continue
        if not hdr:
            continue
        need = ["平台", "用户名/Slug", COL, LEG_COL]
        miss = [c for c in need if c not in hdr]
        if miss:
            print("⚠️ %s 缺列 %s → 跳过" % (nm, miss), file=sys.stderr)
            continue
        # ⚠️ 只读需要的 5 列，**不要 get_all_values()** —— 4 表 9 万行 × 28 列要传几十 MB、
        #    耗时 ~60s，会把 kick-twitch job 从 2 分钟顶到 3 分钟（Actions 按分钟计费，
        #    私有仓库免费额度 2000/月，直接超）。读 5 列 ≈ 1/5 数据量。
        want = ["平台", "用户名/Slug", COL, LEG_COL] + ([TS_COL] if TS_COL in hdr else [])
        cols = {}
        for cname in want:
            try:
                cols[cname] = ws.col_values(hdr.index(cname) + 1)
            except Exception as e:
                print("⚠️ [%s] 读列 %s 失败: %s" % (nm, cname, str(e)[:50]), file=sys.stderr)
                cols[cname] = []
        nrow = max([len(v) for v in cols.values()] or [0])

        def g(cname, i):
            v = cols.get(cname) or []
            return v[i] if i < len(v) else ""

        n_tw = n_empty = 0
        for i in range(1, nrow):                       # i=0 是表头；真实行号 = i+1
            if g("平台", i).strip().lower() != "twitch":   # 只做 Twitch（Kick 的 bio 里 0% 有邮箱）
                continue
            n_tw += 1
            if g(COL, i).strip():
                continue        # 已有邮箱/TG/WA 等 → 跳过（用户：已有的不重复找、不覆盖）
            n_empty += 1
            if legs_filter:                      # 默认空 → 不再按腿过滤
                leg = g(LEG_COL, i).strip() if LEG_COL in cols else ""
                if not any(s_ in leg for s_ in legs_filter):
                    skipped_leg += 1
                    continue
            slug = g("用户名/Slug", i).strip()
            if not slug:
                continue
            ts = _ts_epoch(g(TS_COL, i)) if TS_COL in cols else None
            if ts is None or (time.time() - ts) >= a.days * 86400:
                skipped_old += 1
                continue
            _plat = g("平台", i).strip() if "平台" in cols else ""
            _uid = g("平台UID", i).strip() if "平台UID" in cols else ""
            todo.append((ts, nm, i + 1, slug, _plat, _uid))
        print("[%s] Twitch %d 行 / 空联系方式 %d 行" % (nm, n_tw, n_empty), file=sys.stderr)

    # 2026-10-21: 先 KOL 两表、再 KOC（用户：KOC 慢慢来）；同组内按入库时间【新→旧】，
    #   limit 截断时优先保住最新的行（老行由本机 fill-contacts 兜底）。
    todo.sort(key=lambda x: (1 if x[1] in KOC_TABS else 0, -x[0]))
    print("▶ 待补 %d 行（跳过：来源腿不匹配 %d / 超 %d 天或时间戳不可解析 %d）"
          % (len(todo), skipped_leg, a.days, skipped_old), file=sys.stderr)
    todo = todo[:a.limit]
    if not todo:
        print("（没有待补的行）", file=sys.stderr)
        return 0

    # ── 批量取简介（100 个 login/次） ──
    logins = [t[3] for t in todo]   # todo 现在是 6 元组（多了 platform/uid）
    descs = T.users_desc(logins)
    hit = sum(1 for l in logins if descs.get(str(l).lower()))
    print("▶ /helix/users 命中简介 %d/%d" % (hit, len(logins)), file=sys.stderr)

    results = []
    for ts, nm, rn, slug, _plat, _uid in todo:
        _bio = descs.get(str(slug).lower(), "")
        c = extract_contact(_bio)
        if not c:
            c = _social_contact(_bio)          # 没邮箱 → 用社交主页当联系路径
        if c:
            results.append((nm, rn, slug, c, _plat, _uid))
    # ── 二/三路：对第一路没提到的行再试（--deep 才开）────────────────────
    if a.deep and len(results) < len(todo):
        mods = _try_imports()
        got = {r[2] for r in results}
        miss = [(t[0], t[1], t[2], t[3]) for t in todo if t[3] not in got]
        print("▶ 深度抓取：%d 行待补（X bio / YouTube 描述 / link-in-bio）" % len(miss), file=sys.stderr)
        deep_hits = 0
        for ts, nm, rn, sl in miss[:a.deep_limit]:
            bio = descs.get(str(sl).lower(), "")
            if not bio and not sl:
                continue
            c = deep_enrich(sl, bio, mods)
            if c:
                results.append((nm, rn, sl, c, it.get("_plat", ""), it.get("_uid", "")))
                deep_hits += 1
                print("   ✔ %-20s %s" % (sl, c[:60]), file=sys.stderr)
        print("▶ 深度抓取额外命中 %d/%d" % (deep_hits, min(len(miss), a.deep_limit)), file=sys.stderr)

    rate = 100.0 * len(results) / max(1, len(todo))
    print("▶ 提到联系方式 %d/%d（%.0f%%）" % (len(results), len(todo), rate), file=sys.stderr)
    for nm, rn, slug, c, _pl, _ud in results[:5]:
        print("   %-14s %-22s → %s" % (nm, slug, c[:64]), file=sys.stderr)
    if not a.apply:
        print("（干跑；加 --apply 才写表）", file=sys.stderr)
        return 0

    # ── 写回（按扫描时记下的行号，不做 slug 反查 → 天然免疫同名跨平台撞行） ──
    import gspread.utils
    by_tab = {}
    for nm, rn, slug, c, _pl, _ud in results:
        by_tab.setdefault(nm, []).append((rn, c))
    written, failed = 0, 0
    for nm, pairs in by_tab.items():
        ws = sh.worksheet(nm)
        hdr = ws.row_values(1)
        j = hdr.index(COL) + 1
        for i in range(0, len(pairs), 200):          # 分批，单次 batch 不过大
            batch = [{"range": gspread.utils.rowcol_to_a1(rn, j), "values": [[c]]}
                     for rn, c in pairs[i:i + 200]]
            try:
                ws.batch_update(batch, value_input_option="USER_ENTERED")
                written += len(batch)
            except Exception as e:
                failed += len(batch)
                print("  ⚠️ [%s] 写表失败（保留待下轮）: %s" % (nm, str(e)[:80]), file=sys.stderr)
    # 2026-10-21: 补到联系方式的，记到 D1（下游建联队列可直接查 contact_status='has_contact'）
    _ci = [{"platform": ("kick" if "kick" in str(_pl).lower() else "twitch"),
            "uid": str(_ud), "contact_status": "has_contact"}
           for (_nm, _rn, _sl, _c, _pl, _ud) in results if _ud]
    _d1_contact(_ci)
    print("▶ 已写 %d 处 / 失败 %d 处" % (written, failed), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
