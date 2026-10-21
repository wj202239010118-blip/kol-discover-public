#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kick_discover_cloud.py — 云上 Kick 发现（GitHub Actions，自包含）

口径（2026-10-08 与用户逐条确认）：
- **垂类二分**：播 casino 的 → casino 表；**有垂类用户可能性但不做垂类的** → 非casino 表。
  Kick 类目：casino=28；非casino=**15 Just Chatting / 75 Sports / 509 Sports Betting / 242 Games+Demos**。
  其余 300+ 类目（Minecraft/GTA/音乐台/News…）**不收**。
- **KOL/KOC 分层**（照本机 sheet_common 阈值，不自己编）：
  Kick KOL ≥2000（高价值国家/专属语言打对折 → ≥1000），**未达标的全部进 KOC**（用户：不丢）。
- **语种**：主 **GPT**(goshdev gpt-5.6-sol) → 回落 **DeepSeek** → 再回落文字系统规则 → 判不出写"未知"（不编造）。
  依据是 **Kick 聊天采样**（Kick API 不给语言字段）。
- **去重（唯一方案）**：主键 `平台:平台UID`（Kick channel id，随改名/换链接不变）——
  每轮从 4 张表拉 UID 列建 set，O(1) 判重；历史空 UID 行用 slug 兜底；
  已 在 KOC 表但现够 KOL → 写 KOL 表并删 KOC 那行（**移动**，不重复）。

env（Secrets）：GSPREAD_SA_JSON / DISCOVER_SHEET_ID / KICK_CLIENT_ID / KICK_CLIENT_SECRET
             GPT_THINK_KEY（主）/ DEEPSEEK_API_KEY（兜底）
用法：python3 kick_discover_cloud.py [--apply]（不加 = 干跑）
"""
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

# 2026-10-09: 表内时间戳统一为本地时间（IST, UTC+5:30）—— 与本机写表脚本口径一致。
# 用 time.gmtime 偏移（与 runner/机器时区无关），任何环境都产出 IST。
def _local_ts(fmt="%Y-%m-%d %H:%M"):
    return time.strftime(fmt, time.gmtime(time.time() + 5 * 3600 + 30 * 60))

API = "https://api.kick.com/public/v2"
KICK_V1 = "https://kick.com/api/v1"
WORKER = os.environ.get("KICK_WORKER_URL", "")   # 必须由 Secrets 注入；仓库内不写死内部地址
LIMIT = int(os.environ.get("KICK_LIMIT", "1000"))
MAX_PAGES = int(os.environ.get("KICK_MAX_PAGES", "6"))
SHEET_ID = os.environ.get("DISCOVER_SHEET_ID", "")
APPLY = ("--apply" in sys.argv) or (os.environ.get("KICK_APPLY") == "1")
# 限流：每轮最多处理多少个"新候选"。详情+聊天+LLM 每人都要几秒，
# 不限流会在 30 分钟周期内跑不完（实测 300+ 候选 >12 分钟）。
MAX_NEW = int(os.environ.get("KICK_MAX_NEW", "20"))
# 2026-10-21(消化效率): KOL/KOC 配额分开 —— KOL 先消化，KOC 保底但不挤占 KOL。
KOL_QUOTA = int(os.environ.get("KICK_KOL_QUOTA", "140"))
KOC_QUOTA = int(os.environ.get("KICK_KOC_QUOTA", "60"))
# 详情抓取失败（限流/超时）的 slug 记在这里，下一轮**优先重试**，而不是直接丢弃。
RETRY_P = os.environ.get("KICK_RETRY_STATE") or os.path.join(
    os.environ.get("RUNNER_TEMP") or "/tmp", "kick_retry.json")


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


CASINO_CATS = {"28"}
NONCASINO_CATS = {"15", "75", "509", "242", "8549"}   # Just Chatting/Sports/Sports Betting/Games+Demos/IRL
# 2026-10-10 用户拍板：Kick 侧 IRL(2) 也要收 —— 此前 Twitch 侧收、Kick 侧不收（口径不对称），
# 实测 Kick IRL 有 149 人在播却被整批丢掉。
CAT_NAME = {"28": "Slots & Casino", "15": "Just Chatting", "75": "Sports",
            "509": "Sports Betting", "242": "Games+Demos"}
TAB = {"casino": {"kol": "casinoKOL", "koc": "钓KOC"},
       "noncasino": {"kol": "非casinoKOL", "koc": "非casinoKOC"}}

# KOL 阈值（sheet_common._qualifies_for_kol）；高价值国家打对折
KOL_MIN_KICK = 2000
HIGH_VALUE_FACTOR = 0.5
HIGH_VALUE_COUNTRIES = {"挪威", "瑞典", "丹麦", "芬兰", "冰岛", "瑞士", "奥地利", "卢森堡",
                        "爱尔兰", "新加坡", "卡塔尔", "阿联酋", "科威特", "巴林", "文莱", "新西兰"}
# 语言→国家（单一国家专属语言，享折扣）
DISCOUNT_LANG_COUNTRY = {"俄语": None, "瑞典语": "瑞典", "丹麦语": "丹麦", "芬兰语": "芬兰",
                         "冰岛语": "冰岛", "挪威语": "挪威"}

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36", "Accept": "application/json"}

LANG_HINTS = [("es", ["hola", "buenas", "gente", "sorteo", "ruleta", "tragaperras", "amigos"]),
              ("pt", ["obrigado", "obrigada", "ganhar", "galera", "cassino", "sorte"]),
              ("en", ["welcome", "hello", "hey guys", "subscribe", "giveaway", "stream"]),
              ("de", ["willkommen", "leute", "servus", "glück", "abzocke", "spiele"]),
              ("fr", ["bonjour", "bienvenue", "salut", "merci", "gagner"]),
              ("it", ["ciao", "benvenuti", "salve", "vincere"]),
              ("tr", ["merhaba", "hoş geldin", "kazan"]),
              ("pl", ["witam", "cześć", "wygrana"])]
LANG_CN = {"es": "西班牙语", "pt": "葡萄牙语", "en": "英语", "de": "德语", "fr": "法语",
           "it": "意大利语", "tr": "土耳其语", "pl": "波兰语",
           # 2026-10-09 补：streamscharts 榜单「地区码 → 语种」兜底要用；
           # 原来缺键时会回退成 "TH" 这类英文代码，与表内既有中文值不一致。
           "ru": "俄语", "uk": "乌克兰语", "ja": "日语", "ko": "韩语", "zh": "中文",
           "th": "泰语", "vi": "越南语", "id": "印尼语", "nl": "荷兰语", "sv": "瑞典语",
           "da": "丹麦语", "fi": "芬兰语", "no": "挪威语", "cs": "捷克语", "hu": "匈牙利语",
           "ro": "罗马尼亚语", "el": "希腊语", "he": "希伯来语", "hi": "印地语", "bg": "保加利亚语",
           "sk": "斯洛伐克语", "hr": "克罗地亚语", "sr": "塞尔维亚语", "lt": "立陶宛语",
           "lv": "拉脱维亚语", "et": "爱沙尼亚语", "fa": "波斯语", "ms": "马来语"}
RE_CYR = re.compile(r"[\u0400-\u04ff]")
RE_CJK = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff]")
RE_HANGUL = re.compile(r"[\uac00-\ud7af]")
RE_ARAB = re.compile(r"[\u0600-\u06ff]")
RE_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
RE_TG = re.compile(r"(?:t\.me/|telegram[:\s]+@?)([A-Za-z0-9_]{4,32})", re.I)
RE_WA = re.compile(r"wa\.me/(\d{7,15})")
COUNTRY_HINTS = [("阿根廷", ["argentina", "buenos aires", "mate"]), ("智利", ["chile", "chileno"]),
                 ("巴西", ["brasil", "brazil", "são paulo"]), ("墨西哥", ["mexico", "méxico", "cdmx"]),
                 ("西班牙", ["españa", "madrid"]), ("秘鲁", ["perú", "lima"]),
                 ("哥伦比亚", ["colombia", "bogotá"]), ("委内瑞拉", ["venezuela"]),
                 ("厄瓜多尔", ["ecuador"]), ("美国", ["united states", "miami", "texas"])]
# 2026-10-09: 语种兜底表 —— LLM 移除后，规则判不出语种时用**国家线索**反推（纯规则、零成本）。
#   只覆盖 COUNTRY_HINTS 里能判出的国家；美国按英语（多数）；其余国家语种不定 → 不填（写「未知」）。
COUNTRY_LANG = {"阿根廷": "西班牙语", "智利": "西班牙语", "墨西哥": "西班牙语", "西班牙": "西班牙语",
                "秘鲁": "西班牙语", "哥伦比亚": "西班牙语", "委内瑞拉": "西班牙语", "厄瓜多尔": "西班牙语",
                "巴西": "葡萄牙语", "美国": "英语"}
_TOKEN = {"v": "", "exp": 0.0}


# 2026-10-21(429 治理): 实测一轮 691 个候选因 HTTP 429 丢弃（kick.com/api/v2 与 web.kick.com 都限）。
#   加**全局令牌桶**：所有 Kick 请求共享一个速率上限（默认 3 req/s），比加并发有效。
#   实测 6 workers 无节流 → 29% 失败；节流后应显著下降。
_TH_LOCK = threading.Lock()
_TH_NEXT = [0.0]
_KICK_RPS = float(os.environ.get("KICK_RPS", "3"))
# 2026-10-21(自适应): 固定降速会拖慢正常时段；改成按反馈控制 ——
#   连续 N 次 429 → 全局暂停（15s 起，指数翻倍，封顶 120s）；成功一次立即清零。
#   这样"被限时自动等、没被限时全速"，比把 RPS 拍死更治本。
_BACKOFF_UNTIL = [0.0]
_429_STREAK = [0]
_PROXY_HITS = [0]          # 本轮走代理的上游请求数（配额预算用）


def note_403_429():
    with _TH_LOCK:
        _429_STREAK[0] += 1
        wait = min(120.0, 15.0 * (2 ** min(4, _429_STREAK[0] - 1)))
        _BACKOFF_UNTIL[0] = max(_BACKOFF_UNTIL[0], time.time() + wait)
        print("     [backoff] 429 连续 %d 次 → 全局暂停 %.0fs" % (_429_STREAK[0], wait),
              file=sys.stderr)


def note_ok():
    if _429_STREAK[0]:
        with _TH_LOCK:
            _429_STREAK[0] = 0


def _throttle():
    with _TH_LOCK:
        now = time.time()
        if _BACKOFF_UNTIL[0] > now:
            time.sleep(min(30.0, _BACKOFF_UNTIL[0] - now))
            now = time.time()
        if _TH_NEXT[0] > now:
            time.sleep(_TH_NEXT[0] - now)
            now = time.time()
        _TH_NEXT[0] = max(now, _TH_NEXT[0]) + 1.0 / max(0.5, _KICK_RPS)


def _req(url, headers=None, timeout=20, data=None, cffi=True):
    # 2026-10-21(实测治本): GitHub Actions 的 IP 段被 Kick WAF **整段拉黑** ——
    #   同一批 URL：本机住宅 IP 全 200，Actions 全 429（v2 86 次 + v1 38 次）。
    #   降速/换版本都救不了（不是限速，是 IP 信誉）。唯一验证过的出口是
    #   我们的 Cloudflare Worker 反代（/proxy?url=），实测同样 URL 200。
    #   所以 kick.com/api/* 与 web.kick.com 一律**优先走代理**；没配 WORKER 时直连（本机开发不受影响）。
    _via_proxy = bool(WORKER and ("kick.com/api/" in url or "web.kick.com" in url))
    if _via_proxy:
        # 走我们自己的 /kickproxy（**不带客户端 IP 头**，出口就是 Cloudflare）。
        # 旧代理 holy-cell-3965 会透传来源 IP → Actions 的 IP 照样被 Kick 封，等于没代理。
        url = "%s/kickproxy?url=%s" % (WORKER.rstrip("/"), urllib.parse.quote(url, safe=""))
    if "kick.com" in url:
        _throttle()
    h = dict(UA)
    if headers:
        h.update(headers)
    if _via_proxy:
        with _TH_LOCK:
            _PROXY_HITS[0] += 1
        if D1_TOK:
            h["Authorization"] = "Bearer " + D1_TOK
    if data is None and cffi:
        try:
            from curl_cffi import requests as _c
            r = _c.get(url, headers=h, impersonate="chrome124", timeout=timeout)
            if r.status_code in (429, 403):
                note_403_429()
            elif r.status_code == 200:
                note_ok()
            return r.status_code, r.text
        except Exception:
            pass
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
        print("  [warn] %s → HTTP %s" % (url[:88], st), file=sys.stderr)
        return None
    try:
        return json.loads(body)
    except Exception:
        return None


def token():
    if _TOKEN["v"] and time.time() < _TOKEN["exp"] - 120:
        return _TOKEN["v"]
    cid, csec = os.environ.get("KICK_CLIENT_ID", ""), os.environ.get("KICK_CLIENT_SECRET", "")
    if not cid or not csec:
        print("  [warn] 缺 KICK_CLIENT_ID/SECRET", file=sys.stderr)
        return ""
    data = urllib.parse.urlencode({"grant_type": "client_credentials",
                                   "client_id": cid, "client_secret": csec}).encode()
    st, body = _req("https://id.kick.com/oauth/token",
                    {"Content-Type": "application/x-www-form-urlencoded"}, data=data, cffi=False)
    try:
        d = json.loads(body or "{}")
        _TOKEN["v"] = str(d.get("access_token") or "")
        _TOKEN["exp"] = time.time() + float(d.get("expires_in") or 3600)
    except Exception:
        _TOKEN["v"] = ""
    return _TOKEN["v"]


def live_now():
    """在播全量（官方 v2 livestreams；该端点不支持类目过滤）。"""
    tok = token()
    if not tok:
        return {}
    hdr = {"Authorization": "Bearer %s" % tok}
    out, cursor, t0 = {}, "", time.time()
    for _ in range(MAX_PAGES):
        if time.time() - t0 > 90:
            break
        url = "%s/livestreams?limit=%d" % (API, LIMIT)
        if cursor:
            url += "&cursor=" + urllib.parse.quote(cursor)
        d = _json(url, hdr)
        if not d:
            break
        items = d.get("data") or []
        for it in items:
            ch = it.get("channel") or {}
            slug = str(ch.get("slug") or "").strip().lower()
            if not slug:
                continue
            cat = it.get("category") or {}
            out[slug] = {"viewers": it.get("viewer_count") or 0,
                         "cat_id": str(cat.get("id") or cat.get("category_id") or ""),
                         "cat_name": str(cat.get("name") or "")}
        cursor = str(d.get("next_cursor") or (d.get("pagination") or {}).get("next_cursor") or "")
        if not cursor or len(items) < LIMIT:
            break
    print("[kick] 在播全量 %d 个" % len(out), file=sys.stderr)
    return out


def detail(slug):
    """频道详情。⚠️ 必须优先 v2：实测 v2 才给 display_name/followers/六个社交字段；
    v1 结构不同（display_name 取不到 → 会被"无名丢弃"误杀）。

    2026-10-09：**404/410 = 语义性不存在 → 不再级联 v1/Worker**（它们只会再 404/400，
    纯浪费请求，还把「不存在」伪装成「可重试」）。只有网络级失败才降级重试。
    返回 ``{"_not_found": True}`` 表示确定不存在，供调用方做负缓存。
    """
    st, body = _req("https://kick.com/api/v2/channels/%s" % urllib.parse.quote(slug))
    if st == 200 and body:
        try:
            return json.loads(body)
        except Exception:
            pass
    if st in (404, 410):
        return {"_not_found": True}
    if st == 429:
        # 2026-10-21(实测): 云端 IP 段被 Kick 限流时，v2 返回 429 后 v1 必然也是 429
        #   （日志实测：v2 429 × 86、v1 429 × 38 —— 级联只是把限流翻倍）。
        #   直接判失败进重试队列，不再降级浪费请求。
        return None
    # 2026-10-21: v1 兜底保留（走 _req 时同样经代理）；原来那条指向 Worker 的错误路径
    #   "%s/api/v1/channels/" 从来没可能成功（Worker 只认 /proxy?url=），已删。
    d = _json("%s/channels/%s" % (KICK_V1, urllib.parse.quote(slug)))
    return d or {}


def chat_sample(slug, pages=2):
    """Kick 聊天采样（web.kick.com history，每页 25 条）——语种判据。"""
    ci = _json("https://kick.com/api/v2/channels/%s" % urllib.parse.quote(slug))
    cid = str((ci or {}).get("id") or "")
    if not cid:
        return []
    hdr = {"Origin": "https://kick.com", "Referer": "https://kick.com/"}
    out, cursor = [], ""
    for _ in range(max(1, pages)):
        url = "https://web.kick.com/api/v1/chat/%s/history" % cid
        if cursor:
            url += "?cursor=" + urllib.parse.quote(str(cursor))
        d = _json(url, hdr)
        msgs = None
        if isinstance(d, dict):
            data = d.get("data")
            msgs = (data.get("messages") if isinstance(data, dict) else None) or d.get("messages")
        if not msgs:
            break
        for m in msgs:
            c = m.strip() if isinstance(m, str) else str(((m or {}).get("content") or (m or {}).get("text") or "")).strip()
            if c:
                out.append(c)
        last = msgs[-1] if msgs else None
        cursor = ""
        if isinstance(last, dict):
            lm = last.get("metadata")
            if isinstance(lm, dict):
                cursor = lm.get("message_ref") or ""
            elif isinstance(lm, str):
                cursor = lm
            if not cursor:
                cursor = last.get("message_ref") or ""
        if not cursor:
            break
    return out


def _llm(url, key, payload, headers_extra=None, timeout=30):
    """通用 LLM 调用 → 返回文本（兼容 choices[].message.content / output_text / output[].content[].text）。"""
    if not key:
        return ""
    h = {"Content-Type": "application/json", "Authorization": "Bearer %s" % key}
    if headers_extra:
        h.update(headers_extra)
    st, txt = _req(url, h, data=json.dumps(payload).encode(), cffi=False, timeout=timeout)
    try:
        d = json.loads(txt or "{}")
    except Exception:
        return ""
    c = (d.get("choices") or [{}])[0].get("message", {}) or {}
    out = str(c.get("content") or "").strip()
    if not out:
        out = str(d.get("output_text") or "").strip()
    if not out:
        for it in (d.get("output") or []):
            for cc in (it.get("content") or []):
                if isinstance(cc, dict) and cc.get("text"):
                    out = str(cc["text"])
                    break
            if out:
                break
    return re.sub(r"[^\u4e00-\u9fffA-Za-z]", "", out)[:12]


def ai_lang(texts):
    """语种：① GPT(goshdev) 主 ② DeepSeek 兜底 ③ 规则。"""
    sample = [t for t in (texts or []) if t][:30]
    if not sample:
        return ""
    q = ("下面是某直播频道观众的聊天样本。只回答该频道的主要语言，用中文名"
         "（如 西班牙语/英语/葡萄牙语/德语/波兰语/土耳其语/俄语）。只输出语言名，不要解释。\n\n"
         + "\n".join(sample)[:1800])
    gkey = os.environ.get("GPT_THINK_KEY", "").strip()
    if not gkey:
        try:
            with open(os.path.expanduser("~/.secrets/gpt_key")) as f:
                gkey = f.read().strip()
        except Exception:
            gkey = ""
    r = _llm("https://ai.goshdev.com/v1/chat/completions", gkey,
             {"model": os.environ.get("GPT_MODEL") or "gpt-5.6-sol", "input": q,
              "messages": [{"role": "user", "content": q}], "stream": False})
    if not r:
        r = _llm("https://ai.goshdev.com/chat/completions", gkey,
                 {"model": os.environ.get("GPT_MODEL") or "gpt-5.6-sol", "input": q, "stream": False})
    if r:
        return r
    dkey = os.environ.get("DEEPSEEK_API_KEY", "")
    if dkey:
        r = _llm("https://api.deepseek.com/chat/completions", dkey,
                 {"model": "deepseek-chat", "messages": [{"role": "user", "content": q}],
                  "temperature": 0, "max_tokens": 16})
        if r:
            return r
    return ""


def lang_from_text(t):
    t = t or ""
    if RE_CYR.search(t):
        return "俄语"
    if RE_CJK.search(t):
        return "中文"
    if RE_HANGUL.search(t):
        return "韩语"
    if RE_ARAB.search(t):
        return "阿拉伯语"
    low = t.lower()
    best, sc = "", 0
    for code, kws in LANG_HINTS:
        n = sum(1 for k in kws if k in low)
        if n > sc:
            best, sc = code, n
    return LANG_CN.get(best, "") if sc else ""


def guess_lang(slug, disp, bio, chats=None):
    # 2026-10-09: LLM 判语种**默认关闭**（设 KICK_AI_LANG=1 才启用）—— 用户口径「LLM 判断先不要了」。
    #   原因：LLM 是整条 Kick 腿的耗时瓶颈 —— 单次调用 12–30s **剧烈波动**，20 个候选无论怎么并发
    #   都要 65–80s；去掉后只剩 HTTP，Kick 腿预计 ~20s、整 job <60s
    #   （GitHub 按 job 向上取整计费：2 分钟 → 1 分钟，月计费 1440 → 720，且**无需降频/不用花钱**）。
    #   代价：语种改纯规则判 —— 聊天样本关键词投票（≥2 票）/ bio·名字线索 / 非拉丁脚本正则；
    #   覆盖 es/pt/en/de/fr/it/tr/pl 与俄中韩日阿等脚本，规则未覆盖的语言（泰/越/印尼…）写「未知」（不编造）。
    if os.environ.get("KICK_AI_LANG", "0") not in ("0", "", "false", "False"):
        r = ai_lang(chats)
        if r:
            return r
    texts = [x for x in (chats or []) if x]
    votes = {}
    for t in texts:
        rr = lang_from_text(t)
        if rr:
            votes[rr] = votes.get(rr, 0) + 1
    if votes:
        best = max(votes.items(), key=lambda kv: kv[1])
        if best[1] >= 2:
            return best[0]
    r = lang_from_text("%s %s %s" % (slug or "", disp or "", bio or ""))
    if r:
        return r
    # 2026-10-09 兜底：LLM 移除后，用**国家线索**反推语种（纯规则、零成本）。
    #   例：bio 含 "argentina" → 阿根廷 → 西班牙语。国家也判不出时仍写「未知」（不编造）。
    _cn, _ = guess_country(bio, disp, None)
    return COUNTRY_LANG.get(_cn, "未知")


def guess_country(bio, disp, api_country):
    c = str(api_country or "").strip()
    if c and c not in ("", "None"):
        return c, "Kick API country=%s" % c
    blob = ("%s %s" % (bio or "", disp or "")).lower()
    for cn, kws in COUNTRY_HINTS:
        for kw in kws:
            if kw in blob:
                return cn, "bio/名称含「%s」" % kw
    return "未知", "未判出（country 空、bio 无线索）"


def is_kol(followers, country, lang):
    f = int(followers or 0)
    factor = 1.0
    if country in HIGH_VALUE_COUNTRIES or DISCOUNT_LANG_COUNTRY.get(lang) in HIGH_VALUE_COUNTRIES:
        factor = HIGH_VALUE_FACTOR
    return f >= int(KOL_MIN_KICK * factor)


def metas(slug, disp, bio, d, cat_name, viewers, chat=True):
    """把「列名 → 值」一次算齐（写表时按各表真实表头取用）。

    ⚠️ 2026-10-08 实测：Kick 返回结构已变 —— 没有顶层 display_name；
    名字/bio/社交都在 `user{}` 里；粉丝是 `followers_count`（v2）/`followersCount`（v1）。

    ``chat=False``：跳过聊天采样 —— 调用方已经拿到语言线索（如榜单语言码）时用，
    可省掉一次 web.kick.com 请求 + 一次 LLM 调用。
    """
    u = d.get("user") or {}
    bio = bio or str(u.get("bio") or "")
    disp = disp or str(u.get("username") or d.get("slug") or "").strip()
    followers = d.get("followers_count") or d.get("followersCount") or 0
    uid = str(d.get("id") or d.get("user_id") or "")
    country, why = guess_country(bio, disp, d.get("country") or u.get("country"))
    # 语种：先靠 bio/名字（零成本）；聊天采样**按需** —— chat=False 时完全跳过。
    # 2026-10-09：调用方若已从榜单拿到语言码，就不该再花一次请求 + LLM 去猜。
    # 2026-10-21(省请求): 先只用 bio/名字判语种（零请求）；判不出才去采聊天样本。
    #   原来无条件 chat_sample → 每个候选多一次 web.kick.com 请求，429 的主要来源。
    lang = lang_from_text("%s %s %s" % (slug or "", disp or "", bio or ""))
    if not lang:
        lang = guess_lang(slug, disp, bio, chat_sample(slug, pages=1) if chat else [])
    mails, tg, wa = RE_EMAIL.findall(bio), RE_TG.findall(bio), RE_WA.findall(bio)
    others = (["TG:" + t for t in tg[:2]] + ["WA:" + w for w in wa[:2]])
    return {
        "时间戳": _local_ts(), "平台": "Kick", "主播名": disp,
        "用户名/Slug": slug, "用户名": slug, "平台UID": uid,
        "分类": cat_name or "", "语种": lang, "观众数": str(viewers or ""),
        "粉丝数": str(followers or ""), "主页链接": "https://kick.com/%s" % slug,
        "国家": country, "状态": "待确认",
        "联系方式": ("邮箱: %s" % mails[0]) if mails else "",
        "其他社交账号": " / ".join(others), "国家判断理由": why,
        "IG": str(u.get("instagram") or ""), "X": str(u.get("twitter") or ""),
        "TikTok": str(u.get("tiktok") or ""), "YouTube": str(u.get("youtube") or ""),
        "Facebook": str(u.get("facebook") or ""), "Discord": str(u.get("discord") or ""),
        "william备注": "", "建联腿": "kick-cloud", "来源": "kick-cloud",
        "_followers": int(followers or 0),
    }


def load_seen(sh):
    """去重索引：UID 主键 + slug 兜底。

    ⚠️ 2026-10-08 修：slug 兜底**必须按平台隔离**（`kick:<slug>`）——
    四表里同时存在 Twitch 与 Kick 的记录，裸 slug 会跨平台误撞（同名不同人）。
    UID 列 = 第 5 列，slug 列 = 第 4 列，平台列 = 第 2 列（四表同位置）。
    """
    seen, slugs = set(), set()
    names = [TAB["casino"]["kol"], TAB["casino"]["koc"], TAB["noncasino"]["kol"], TAB["noncasino"]["koc"]]
    for nm in names:
        try:
            ws = sh.worksheet(nm)
            # 2026-10-21(提速): 原来每表 3 次 col_values(2/4/5) = 12 次 API；
            #   改成 1 次 batchGet（B/D/E 三列）→ 4 次 API。去重口径完全不变。
            plat_c, slug_c, uid_c = ws.batch_get(["B:B", "D:D", "E:E"])
            for row in uid_c[1:]:
                v = row[0] if row else ""
                if str(v).strip():
                    seen.add("kick:" + str(v).strip())
            for i in range(1, min(len(plat_c), len(slug_c))):
                pv = plat_c[i][0] if plat_c[i] else ""
                sv = slug_c[i][0] if slug_c[i] else ""
                if "kick" in str(pv).lower() and str(sv).strip():
                    slugs.add(str(sv).strip().lower())
        except Exception as e:
            print("  [warn] 读 %s 索引失败: %s" % (nm, str(e)[:70]), file=sys.stderr)
    print("[去重] 已登记 Kick UID %d / Kick slug %d" % (len(seen), len(slugs)), file=sys.stderr)
    return seen, slugs, names


# ── 2026-10-21: D1 状态层（与 twitch 腿同构）。未配置时静默回落读表。──
D1_API = (os.environ.get("DISCOVER_API") or "").rstrip("/")
D1_TOK = os.environ.get("DISCOVER_TOKEN") or ""
# D1 写失败（免费层日额度用尽等）时的本地兜底：存「已写表但没记进 D1」的 slug，
# 下一轮去重并进来，避免重复写表。
RECONCILE_P = os.environ.get("KICK_RECONCILE") or os.path.join(
    os.environ.get("RUNNER_TEMP") or "/tmp", "kick_reconcile.txt")


def load_recon():
    try:
        with open(RECONCILE_P, encoding="utf-8") as f:
            return {ln.strip().lower() for ln in f if ln.strip()}
    except Exception:
        return set()


def save_recon(ids):
    try:
        with open(RECONCILE_P, "w", encoding="utf-8") as f:
            for x in sorted(ids):
                f.write(x + "\n")
    except Exception:
        pass



def _d1(path, payload=None, method="POST"):
    if not D1_API or not D1_TOK:
        return None
    try:
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(D1_API + path, data=data, method=method,
            headers={"Authorization": "Bearer " + D1_TOK,
                     "Content-Type": "application/json", "User-Agent": "kick-discover/1.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        print("  [warn] D1 %s 失败: %s" % (path, str(e)[:70]), file=sys.stderr)
        return None


def d1_known_slugs(slugs):
    """批量查 D1 已知 slug。D1 不可用 → None（回落读表）。"""
    if not D1_API or not D1_TOK:
        return None
    out = set()
    for i in range(0, len(slugs), 1000):
        r = _d1("/discover/check_slug", {"platform": "kick", "slugs": slugs[i:i + 1000]})
        if r is None:
            return None
        for it in (r.get("known") or []):
            if it.get("slug"):
                out.add(str(it["slug"]).lower())
    return out


def d1_known_uids(uids):
    if not D1_API or not D1_TOK:
        return None
    out = set()
    for i in range(0, len(uids), 1000):
        r = _d1("/discover/check", {"platform": "kick", "uids": uids[i:i + 1000]})
        if r is None:
            return None
        for it in (r.get("known") or []):
            out.add(str(it.get("uid")))
    return out


def main():
    if not SHEET_ID:
        print("缺 DISCOVER_SHEET_ID", file=sys.stderr)
        return 2
    live = live_now()
    if not live:
        print("在播为空 → 本轮不写", file=sys.stderr)
        return 0
    buckets = {"casino": [], "noncasino": [], "skip": 0}
    for slug, m in live.items():
        cid = m.get("cat_id") or ""
        if cid in CASINO_CATS:
            buckets["casino"].append((slug, m))
        elif cid in NONCASINO_CATS:
            buckets["noncasino"].append((slug, m))
        else:
            buckets["skip"] += 1
            # 2026-10-09: 记下被丢的类目名，便于定期核查有没有漏收的价值类目
            _nm = "%s(id=%s)" % (m.get("cat_name") or m.get("category_name") or m.get("category") or "?", cid)
            buckets.setdefault("skip_names", {})
            buckets["skip_names"][_nm] = buckets["skip_names"].get(_nm, 0) + 1
    # 2026-10-21(提速): 分类目分频 —— 每轮只跑 casino（主力），非 casino 每 2 小时跑一次。
    #   casino 类目在播只有几十个 → 秒级；非 casino 四百多个 → 几分钟。
    _scope = (os.environ.get("KICK_SCOPE") or "all").strip().lower()
    if _scope == "casino":
        buckets["noncasino"] = []
    elif _scope == "noncasino":
        buckets["casino"] = []
    print("[范围] KICK_SCOPE=%s" % _scope, file=sys.stderr)
    print("[分流] casino=%d 非casino=%d 其它类目不收=%d"
          % (len(buckets["casino"]), len(buckets["noncasino"]), buckets["skip"]), file=sys.stderr)
    if buckets.get("skip_names"):
        _top = sorted(buckets["skip_names"].items(), key=lambda x: -x[1])[:10]
        print("     [不收类目 top10] " + ", ".join("%s=%d" % (k, v) for k, v in _top), file=sys.stderr)
    if not (buckets["casino"] or buckets["noncasino"]):
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
    # 2026-10-21: slug 去重优先查 D1（不读 4 张表）；uid 去重在拉到详情后批量查。
    _all_slugs = [str(sl) for k in ("casino", "noncasino") for sl, _m in buckets[k]]
    _t_dedup = time.time()
    _d1sl = d1_known_slugs(_all_slugs) if _all_slugs else None
    if _d1sl is not None:
        slugs_seen = set(_d1sl)
        _recon = load_recon()
        slugs_seen |= _recon
        if _recon:
            print("[去重] 本地兜底 %d 条（D1 写失败遗留）" % len(_recon), file=sys.stderr)
        seen = set()
        print("[去重] D1 slug 命中 %d / 候选 %d（%.1fs，未读表）"
              % (len(slugs_seen), len(_all_slugs), time.time() - _t_dedup), file=sys.stderr)
    else:
        seen, slugs_seen, _ = load_seen(sh)

    # 目标表表头（动态：钓KOC 是 24 列且第3列叫「用户名」）
    hdrs = {}
    for kind, t in TAB.items():
        for tier, nm in t.items():
            hdrs[(kind, tier)] = (nm, sh.worksheet(nm).row_values(1))

    # ── 阶段 1：串行筛候选 + 拉详情（去重需 uid，故在此完成） ────────────────
    retry = load_retry()
    _failed = []
    st = {"slug": 0, "uid": 0, "nodetail": 0, "noname": 0, "pass": 0}
    _CAND_CAP = KOL_QUOTA + KOC_QUOTA + 60     # 缓冲：部分候选会落到另一 tier
    _done = 0                                  # 本轮已处理的候选数
    pending = []                               # [(kind, slug, meta, d, disp)]
    for kind in ("casino", "noncasino"):
        # 2026-10-21: 上一轮详情失败的 slug **优先**（失败次数多的更靠前），其余按观众数降序。
        items = sorted(buckets[kind],
                       key=lambda x: (0 if x[0] in retry else 1,
                                      -int((retry.get(x[0]) or {}).get("fails", 0)),
                                      -int(x[1].get("viewers") or 0)))
        if _done >= _CAND_CAP:
            break
        for slug, meta in items:
            if _done >= _CAND_CAP:
                break
            if slug in slugs_seen:
                st["slug"] += 1
                continue
            d = detail(slug)
            if not d:
                st["nodetail"] += 1
                _r = retry.get(slug) or {}
                retry[slug] = {"fails": int(_r.get("fails", 0)) + 1,
                               "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
                _failed.append({"platform": "kick", "slug": slug, "uid": "",
                                "status": "failed", "last_error": "detail_429_or_miss"})
                continue
            _u = d.get("user") or {}
            disp = str(_u.get("username") or d.get("slug") or "").strip()
            if not disp:
                st["noname"] += 1
                continue
            uid = str(d.get("id") or d.get("user_id") or "")
            if uid and ("kick:" + uid) in seen:
                st["uid"] += 1
                continue
            st["pass"] += 1
            _done += 1
            pending.append((kind, slug, meta, d, disp))

    # 2026-10-21: uid 去重（uid 只有拉到详情才知道）→ 批量查 D1，已入库的剔除。
    if pending and not seen:
        _p_uids = [str((t[3] or {}).get("id") or (t[3] or {}).get("user_id") or "") for t in pending]
        _d1u = d1_known_uids([u for u in _p_uids if u])
        if _d1u is not None:
            _before = len(pending)
            pending = [t for t in pending
                       if str((t[3] or {}).get("id") or (t[3] or {}).get("user_id") or "") not in _d1u]
            print("[去重] D1 uid 命中 %d，剔除后剩 %d" % (_before - len(pending), len(pending)),
                  file=sys.stderr)

    # ── 阶段 2：并发算 metas（每个候选 1 次聊天采样 + 1 次 LLM 判语种，~17s/个） ──
    # 2026-10-09 提速：原先**串行** → 20 个候选 ~372s（占整条 Kick 腿 6.4min 的绝大部分）。
    # 判定口径、去重、写表行内容**完全不变** —— 只把互相独立的候选并行处理
    # （网络层无共享可变状态：curl_cffi/urllib 每次新建连接；_TOKEN 只在串行的 live_now 用）。
    # 并发数可用 KICK_WORKERS 覆盖（默认 6 —— 实测甜点）。
    #   20 个候选：6 workers → 4 批 ≈ 49s（实测）；12 workers → 2 批**反而 79s**
    #   （阶段1 不变、只有并发段变慢 ⇒ 触发 LLM 网关/kick.com 排队限流，单候选 12s→39s）。
    #   ⇒ 提高并发无益；要再快只能**减少 LLM 调用次数**（批量判语种），而非加大并发。
    import concurrent.futures as _cf

    def _mk(t):
        kind, slug, meta, d, disp = t
        try:
            mm = metas(slug, disp, str(d.get("bio") or ""), d,
                       meta.get("cat_name") or CAT_NAME.get(meta.get("cat_id") or "", ""),
                       meta.get("viewers"))
            return t, mm
        except Exception as e:                 # 单候选失败不拖垮整批
            print("  [warn] metas(%s) 失败: %s" % (slug, str(e)[:80]), file=sys.stderr)
            return t, None

    out_rows = {}
    done_pairs = []
    if pending:
        _W = max(1, int(os.environ.get("KICK_WORKERS", "6")))
        print("[并发] %d 个候选 × %d workers" % (len(pending), _W), file=sys.stderr)
        with _cf.ThreadPoolExecutor(max_workers=_W) as _ex:
            done_pairs = list(_ex.map(_mk, pending))

    # ── 阶段 3：串行组行（按各表真实表头取用）+ KOL/KOC 配额 ───────────────
    _tier_done = {"kol": 0, "koc": 0}
    _recon_fail = []
    _over_quota = 0
    # 2026-10-21: 按粉丝数降序处理 —— 粉丝多的更可能是 KOL，先把 KOL 配额填满；
    #   剩下的额度（KOL 没填满的部分）动态让给 KOC，避免「KOL 名额空着、KOC 反而被卡」。
    done_pairs.sort(key=lambda p: -int(((p[0][3] or {}).get("followers_count")
                                        or (p[0][3] or {}).get("followersCount") or 0)))
    for (kind, slug, meta, d, disp), mm in done_pairs:
        if not mm:
            continue
        tier = "kol" if is_kol(mm["_followers"], mm["国家"], mm["语种"]) else "koc"
        _q = (KOL_QUOTA if tier == "kol"
              else KOC_QUOTA + max(0, KOL_QUOTA - _tier_done["kol"]))
        if _tier_done[tier] >= _q:
            _over_quota += 1
            continue                           # 超配额：不写、不标 seen → 下一轮还会遇到
        _tier_done[tier] += 1
        nm, hdr = hdrs[(kind, tier)]
        row = [str(mm.get(c, "")) for c in hdr]
        out_rows.setdefault(nm, {"hdr": hdr, "rows": [], "keys": []})
        out_rows[nm]["rows"].append(row)
        out_rows[nm]["keys"].append((slug, str(d.get("id") or d.get("user_id") or "")))
        out_rows[nm].setdefault("keys4", []).append(
            (slug, str(d.get("id") or d.get("user_id") or ""), tier, kind))

    print("[去重明细] slug跳过=%d uid跳过=%d 详情失败=%d 无名=%d 通过=%d"
          % (st["slug"], st["uid"], st["nodetail"], st["noname"], st["pass"]), file=sys.stderr)
    total = 0
    for nm, pack in out_rows.items():
        rows = pack["rows"]
        total += len(rows)
        print("[%s] 待写 %d 行（表头 %d 列）" % (nm, len(rows), len(pack["hdr"])), file=sys.stderr)
        for r in rows[:3]:
            print("     + " + " | ".join(str(x)[:14] for x in r[:8]), file=sys.stderr)
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

            for _slug, _uid in pack.get("keys", []):
                slugs_seen.add(_slug)
                retry.pop(_slug, None)
                if _uid:
                    seen.add("kick:" + _uid)
            # 2026-10-21: 写表成功 → D1 标 exported（含人群/垂类）
            _items = [{"platform": "kick", "uid": _uid, "slug": _slug, "status": "exported",
                       "grp": _tier, "vertical": _kind}
                      for (_slug, _uid, _tier, _kind) in pack.get("keys4", []) if _uid]
            if _items:
                if _d1("/discover/done", {"items": _items}) is None:
                    _recon_fail.extend([_slug for (_slug, _u, _t, _k) in pack.get("keys4", []) if _slug])
            print("     [写入] %s +%d" % (nm, len(rows)), file=sys.stderr)
    if _over_quota:
        print("     [配额] 超 KOL/KOC 配额未写 %d 个（下一轮继续）" % _over_quota, file=sys.stderr)
    if len(retry) > 500:                       # 防止状态文件无限膨胀
        _drop = sorted(retry.items(), key=lambda x: -int((x[1] or {}).get("fails", 0)))[500:]
        for _k, _ in _drop:
            retry.pop(_k, None)
    save_retry(retry)
    if _failed:
        _d1("/discover/done", {"items": _failed})
    if _recon_fail:
        save_recon(set(locals().get("_recon", set())) | set(_recon_fail))
        print("     [warn] D1 写失败 %d 条 → 本地兜底记账（下轮仍能去重）" % len(_recon_fail),
              file=sys.stderr)
    _d1("/discover/metrics", {"items": [
        {"name": "kick.candidates", "value": len(_all_slugs)},
        {"name": "kick.written", "value": total},
        {"name": "kick.detail_failed", "value": len(_failed)},
        {"name": "kick.dedup_ms", "value": int((time.time() - _t_dedup) * 1000)},
        {"name": "kick.passed", "value": st["pass"]},
        {"name": "kick.proxy_requests", "value": _PROXY_HITS[0]},
    ]})
    print("[结果] 合计 %d 行%s" % (total, "" if APPLY else "（dry-run 未写）"), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
