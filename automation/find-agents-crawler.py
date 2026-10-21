#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# 2026-09-14: 直连优先，失败回退代理
try:
    import net_fallback; net_fallback.install()
except Exception:
    pass
"""
find-agents-crawler.py v2 — 无头 Telegram 频道图爬虫（找代理管线）
2026-08-20 升级（Claude Code 审查落地）:
  1. 抓取重试: 5xx/429 指数退避 2 次，其他异常也重试 2 次
  2. 页面有效性: t.me 验证码/空页(tgme_page 缺失) 视为抓取失效而非"无线索"
  3. 死 handle 健康检查: 全表最久未查的 handle 重查，连续 2 次失效标"失效(爬虫)"
  4. TG 搜索腿: 复用 smart-cs 生产账号会话（--search-channels）按关键词搜频道
  5. 相关性分类加权: casino 权重2 / sports 权重1 + 排除词（配置外置）
  6. 指标: 每轮末尾一行 JSON METRICS + 连续 N 轮 0 新增告警（alerts 日志）
  7. 原子状态写: 临时文件 + os.replace
  8. 配置外置: ~/scripts/agent_crawler_config.json（目录腿 URL/关键词/阈值）
"""
import argparse, csv, gzip, html as htmlmod, json, os, re, time, urllib.error, urllib.request
from collections import defaultdict
from datetime import datetime
from pathlib import Path

# 2026-10-09: 表内时间戳统一为本地时间（IST, UTC+5:30）—— 与本机写表脚本口径一致。
def _local_ts(fmt='%Y-%m-%d %H:%M'):
    return time.strftime(fmt, time.gmtime(time.time() + 5 * 3600 + 30 * 60))

# ─── 配置 ───────────────────────────────────────────────────────────────
SHEET_ID = os.environ.get('AGENT_CRAWLER_SHEET_ID') or ''   # 2026-10-08: env 可覆盖（云上用 Secret，不写死在公开代码里）
TARGET_GID = 963214243  # 「代理」tab
# 2026-10-08: 路径支持 env 覆盖 —— 为了能在 GitHub Actions（免费定时）里跑同一份脚本
#   （云上没有 ~/scripts、~/Desktop 这些本机路径）。**env 未设时行为与本机完全一致**。
SA = os.environ.get('AGENT_CRAWLER_SA') or str(Path.home() / 'influencer-scraper' / 'service_account.json')
LOCAL_CSV = os.environ.get('AGENT_CRAWLER_CSV') or str(Path.home() / 'Desktop' / '代理线索池_20260820.csv')
STATE_FILE = Path(os.environ.get('AGENT_CRAWLER_STATE') or (Path.home() / 'scripts' / 'agent_crawler_state.json'))
CONFIG_FILE = Path(os.environ.get('AGENT_CRAWLER_CONFIG') or (Path.home() / 'scripts' / 'agent_crawler_config.json'))
ALERTS_FILE = Path(os.environ.get('AGENT_CRAWLER_ALERTS') or (Path.home() / 'scripts' / 'agent_crawler_alerts.log'))
# 2026-10-09: 「来源降级」显式记录 —— 打破静默。
#   背景：云端(GitHub Actions)上 WA 的两处来源必然拿不到（见下），但原来被 try/except 吞掉，
#   workflow 仍显示 success → “跑成功但结果不完整”很难被发现。现在每个缺失来源都登记，
#   写进 METRICS + alerts.log（workflow 摘要步骤会 tail）→ 云端日志里直接可见。
DEGRADED = []
UA = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36'}
SLEEP = 1.2

SKIP_SLUGS = {'share', 'proxy', 'addstickers', 'setlanguage', 'bg', 'iv', 'socks',
              'joinchat', 'telegram', 'configure', 'channel', 'privacy', 'tos', 'faq'}

RE_INVITE = re.compile(r'(?:t\.me|telegram\.me)/(?:joinchat/)?\+([A-Za-z0-9_\-]{8,})')
RE_HANDLE = re.compile(r'(?:t\.me|telegram\.me)/([A-Za-z0-9_]{3,32})')
RE_EMAIL = re.compile(r'[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}')
RE_WA = re.compile(r'wa\.me/(\d{7,15})')
# 2026-08-24: WA 群邀请链接（chat.whatsapp.com/xxx）—— 进群钥匙，只收集不主动用
RE_WA_GROUP = re.compile(r'chat\.whatsapp\.com/([A-Za-z0-9]{15,25})')
RE_BIO = re.compile(r'<div class="tgme_channel_info_description"[^>]*>(.*?)</div>', re.S)

# 2026-08-24 同事反馈：代理线索必须纯西语/英语 —— 中文/西里尔/日韩等一律视为噪音跳过
# （原有西里尔过滤扩展为中文+西里尔+其他非目标文字，保证代理内容对口）
RE_NOISE_LANG = re.compile(r'[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af\u0400-\u04FF\u0600-\u06FF\u0e00-\u0e7f]')


def load_cfg():
    default = {
        'run_hour': 6,
        'directory_urls': [],
        'search_enabled': False,  # 2026-09: TG 搜索腿停用(占位)，防 spawn 抢 session
        'search_keywords': ['tragamonedas', 'casino', 'slots', 'ruleta', 'apuestas casino'],
        'search_limit': 10,
        'alert_zero_runs': 3,
        'casino_strong': ['tragamonedas', 'tragaperras', 'slots', 'ruleta', 'casino', 'blackjack',
                          'bingo', 'jackpot', 'maquinas', 'máquinas', 'ruleta en vivo', 'casino online'],
        'sports_keys': ['apuestas deportivas', 'futbol', 'fútbol', 'pronostic', 'picks', 'cuota',
                        'quiniela', 'tipster', 'stake', 'bet365', 'retos', 'combinadas', 'analistas',
                        'backtesting', 'trading', 'mlb', 'nba', 'nfl', 'nhl', 'tenis', 'baloncesto',
                        'boxeo', 'mma', 'esports', 'apuestas', 'deporte', 'football', 'basket', 'ncaa'],
        'exclude_words': ['jugar gratis', 'sin dinero real', 'tutorial', 'demo'],
    }
    try:
        if CONFIG_FILE.exists():
            default.update(json.loads(CONFIG_FILE.read_text()))
    except Exception:
        pass
    return default


def classify(text, cfg):
    t = (text or '').lower()
    if any(w in t for w in cfg.get('exclude_words', [])):
        return '排除'
    c = sum(2 for k in cfg.get('casino_strong', []) if k in t)
    s = sum(1 for k in cfg.get('sports_keys', []) if k in t)
    if c >= 2:
        return 'casino相关'
    if c == 1 and s == 0:
        return 'casino相关(弱)'
    if c >= 1 and s >= 1:
        return '混合(casino+体育)'
    if s >= 1:
        return '体育博彩'
    return '待确认'


def fetch_page(url, retries=2, timeout=8):
    """通用抓取（gzip 解码），5xx/429/超时指数退避重试；仍失败返回 None。timeout 默认 8s 防网络悬挂。"""
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=UA)
            resp = urllib.request.urlopen(req, timeout=timeout)
            raw = resp.read()
            if resp.headers.get('Content-Encoding') == 'gzip' or raw[:2] == b'\x1f\x8b':
                raw = gzip.decompress(raw)
            return raw.decode('utf-8', 'replace')
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and i < retries:
                time.sleep(2 * (i + 1))
                continue
            return None
        except Exception:
            if i < retries:
                time.sleep(2 * (i + 1))
                continue
            return None
    return None


def fetch_tme(handle):
    return fetch_page(f'https://t.me/s/{handle}')


def extract(html, own_handle):
    """从预览页提取: (邀请链, 其他handle, emails, wa, bio, wa群邀请链)"""
    invites = sorted(set(RE_INVITE.findall(html)))
    handles = sorted(set(h for h in RE_HANDLE.findall(html)
                         if h not in SKIP_SLUGS and h.lower() != own_handle.lower()))
    emails = sorted(set(e for e in RE_EMAIL.findall(html)
                        if not e.endswith(('.png', '.jpg', '.gif', '.svg', '.webp'))))
    wa = sorted(set(RE_WA.findall(html)))
    wa_groups = sorted(set(RE_WA_GROUP.findall(html)))  # 2026-08-24: WA 群邀请链接
    bio = ''
    m = RE_BIO.search(html)
    if m:
        bio = re.sub(r'<[^>]+>', '', m.group(1))
        bio = htmlmod.unescape(bio).strip()
    return invites, handles, emails, wa, bio, wa_groups


def load_sheet():
    """读「代理」tab → (rows, handles, invites, handle→row行号, ws, sh)"""
    import gspread
    gc = gspread.service_account(filename=SA)
    sh = gc.open_by_key(SHEET_ID)
    ws = sh.get_worksheet_by_id(TARGET_GID)
    rows = ws.get_all_values()
    handles, invites, hrow = set(), set(), {}
    for i, r in enumerate(rows[1:], start=2):
        if len(r) >= 6:
            invites.update(RE_INVITE.findall(r[2] or ''))
            invites.update(RE_INVITE.findall(r[5] or ''))
            for h in RE_HANDLE.findall(r[2] or '') + RE_HANDLE.findall(r[5] or '') + RE_HANDLE.findall(r[4] or ''):
                if h not in SKIP_SLUGS:
                    handles.add(h)
                    hrow.setdefault(h, i)
    return rows, handles, invites, hrow, ws, sh


def row_key(r):
    """行的去重键 = 【身份标识】归一化：TG handle/邀请链 > wa.me > chat.whatsapp > 否则名字(col1)。
    2026-10-03：注意 col2 对论坛腿是【恒定源URL】(gpwa)，绝不能当键 → 先抽 t.me/wa 标识，抽不到才回落名字。"""
    cols = [str(r[i] if len(r) > i else '').strip() for i in range(len(r))]
    joined = ' '.join(cols[2:6])
    m = re.search(r'(?:t\.me|telegram\.me)/(?:joinchat/)?\+?([A-Za-z0-9_\-]{4,})', joined)
    if m:
        return 'tg:' + m.group(1).lower()
    m = re.search(r'wa\.me/(\d{7,15})', joined)
    if m:
        return 'wa:' + m.group(1)
    m = re.search(r'chat\.whatsapp\.com/([A-Za-z0-9]{10,})', joined)
    if m:
        return 'wag:' + m.group(1)
    c1 = cols[1] if len(cols) > 1 else ''
    if c1:
        return 'nm:' + c1.lower()
    c2 = cols[2] if len(cols) > 2 else ''
    return ('u:' + c2.lower().rstrip('/')) if c2 else ''


def csv_seeds():
    out = []
    if not os.path.exists(LOCAL_CSV):
        DEGRADED.append('local_csv 缺失(%s)' % LOCAL_CSV)
        return []
    with open(LOCAL_CSV, encoding='utf-8') as f:
        for r in csv.reader(f):
            if len(r) >= 6:
                out += RE_HANDLE.findall(r[2] or '')
                out += RE_HANDLE.findall(r[5] or '')
    return sorted(set(h for h in out if h not in SKIP_SLUGS))


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {}


def save_state(state):
    """原子写: 临时文件 + os.replace，防进程被杀留下坏 JSON"""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1))
    os.replace(str(tmp), str(STATE_FILE))


def fetch_dir_handles(cfg):
    """目录种子自增腿: Wayback 快照目录页提取 t.me handle"""
    out = set()
    for url in cfg.get('directory_urls', []):
        html = fetch_page(url, retries=1, timeout=10)  # 目录腿快照属 best-effort，快速失败
        if html:
            out.update(RE_HANDLE.findall(html))
        time.sleep(2)
    return {h for h in out if h not in SKIP_SLUGS and re.fullmatch(r'[A-Za-z0-9_]{3,32}', h or '')}


RE_FORUM_BY = re.compile(r'by\s+(?:<[^>]+>\s*)*([A-Za-z][A-Za-z0-9_]{2,29})')
# 2026-08-24: 论坛作者提取增强 —— 兼容 vBulletin(member.php?u=) / xenForo(members/) / phpBB(memberlist) 等
# 单捕获组（避免 findall 返回元组）：提取 member 链接里的用户名
RE_FORUM_MEMBER = re.compile(r'(?:member\.php\?u=\d+["\'][^>]*>|/members/|memberlist\.php[^>]*>|/user/)([A-Za-z0-9_.\-]{3,32})')
FORUM_SKIP = {'the', 'and', 'for', 'with', 'from', 'this', 'that', 'was', 'were', 'are', 'have', 'has',
              'not', 'but', 'you', 'your', 'all', 'can', 'new', 'now', 'per', 'out', 'one', 'two',
              'via', 'just', 'last', 'more', 'than', 'into', 'been', 'will', 'when', 'what', 'their',
              'there', 'they', 'them', 'these', 'those', 'first', 'also', 'after', 'before', 'about',
              'would', 'could', 'should', 'member', 'members', 'post', 'posts', 'pm', 'am', 'days',
              'threads', 'thread', 'forum', 'replies', 'views', 'ago', 'today', 'yesterday', 'by'}


def fetch_forum_users(cfg):
    """论坛腿: 直连/RSS 优先，Wayback 兜底（2026-08-24 改造，绕开 Wayback 不稳定）。
    返回 (usernames, handles) —— 论坛社群成员是 A 线线索（站内信可触达）"""
    users, handles = set(), set()
    # 1) RSS 源（结构化，含作者）：AGD xenForo RSS
    for rss_url in cfg.get('forum_rss_urls', []):
        html = fetch_page(rss_url, retries=1, timeout=15)
        if not html:
            continue
        for m in re.finditer(r'<dc:creator[^>]*>([^<]+)</dc:creator>', html):
            u = m.group(1).strip()
            if u and len(u) >= 3:
                users.add(u)
        for m in re.finditer(r'<link[^>]*>(https?://[^<]+)</link>', html):
            handles.update(RE_HANDLE.findall(m.group(1)))
        time.sleep(1.5)
        print(f'[forum] RSS 源 {rss_url.split("/")[2]} 提取 {len(users)} 个作者')
    # 2) 直连论坛页（GPWA / WebmasterSun 等，非 Wayback）
    for url in cfg.get('forum_direct_urls', []):
        html = fetch_page(url, retries=1, timeout=15)
        if not html:
            continue
        users.update(RE_FORUM_BY.findall(html))
        users.update(RE_FORUM_MEMBER.findall(html))
        handles.update(RE_HANDLE.findall(html))
        time.sleep(2)
    # 3) Wayback 兜底（老论坛/403 源）
    for url in cfg.get('forum_urls', []):
        html = fetch_page(url, retries=1, timeout=15)
        if not html:
            continue
        users.update(RE_FORUM_BY.findall(html))
        users.update(RE_FORUM_MEMBER.findall(html))
        handles.update(RE_HANDLE.findall(html))
        time.sleep(2)
    users = {u for u in users if u not in FORUM_SKIP and len(u) >= 3}
    handles = {h for h in handles if h not in SKIP_SLUGS and re.fullmatch(r'[A-Za-z0-9_]{3,32}', h or '')}
    return users, handles


def tg_search_keywords(keywords, limit=10, mode='all'):
    """TG 搜索腿 [占位]: 原实现 spawn smart-cs --search-channels 做全局 SearchRequest，
    会抢 userbot_read.session，已停用（2026-09 决策）。
    后续「只搜已加入群/私聊」逻辑想好后在此实现。返回 {'usernames':[], 'in_dialog':[]}。"""
    print('[search] TG 搜索腿已停用(占位)：不 spawn --search-channels，避免抢 session')
    return {'usernames': [], 'in_dialog': []}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true', help='只打印不写表、不跑搜索腿')
    ap.add_argument('--max-new', type=int, default=30)
    ap.add_argument('--depth', type=int, default=1)
    args = ap.parse_args()

    cfg = load_cfg()
    state = load_state()
    fetched = set(state.get('seen_handles', []))
    invite_owners = defaultdict(set, {k: set(v) for k, v in state.get('invite_owners', {}).items()})
    cn_handles = set(state.get('cn_handles', []))  # 2026-08-24: 历史中文 handle（持久化，聚类网络行也排除）

    # 1. seed
    try:
        rows, sheet_handles, sheet_invites, hrow, ws, sh = load_sheet()
        existing_keys = {row_key(r) for r in rows if row_key(r)}   # 2026-10-03: 表内已有链接（含 WA 群/线索）→ 写前过滤
        print(f'[seed] 「代理」tab 已见 handle {len(sheet_handles)} 个 / 邀请链 {len(sheet_invites)} 个 / 链接 {len(existing_keys)} 个')
    except Exception as e:
        print(f'[seed] 读表失败({e})，退回本地 CSV')
        rows, sheet_handles, sheet_invites, hrow, ws, sh = [], set(csv_seeds()), set(), {}, None, None
        existing_keys = set()
    pending = set(state.get('frontier', []))
    seeds = sorted(h for h in (sheet_handles | pending)
                   if h not in SKIP_SLUGS and h not in fetched
                   and re.fullmatch(r'[A-Za-z0-9_]{3,32}', h or ''))
    print(f'[seed] 本轮待抓 {len(seeds)} 个（表内 {len(sheet_handles)} + 前沿 {len(pending)}）: {seeds[:12]}')

    today = datetime.now().strftime('%Y-%m-%d')
    now_ts = _local_ts()
    in_dialog_rows = []   # 外部源直接入表行（论坛/目录/搜索腿已加入群）

    # 1b. 目录腿（frontier 枯竭时触发，每天最多尝试 1 次——快照内容是静态的）
    if len(seeds) < 10 and state.get('last_dir_date', '') != today:
        state['last_dir_date'] = today
        dir_handles = fetch_dir_handles(cfg)
        fresh = sorted(h for h in dir_handles if h not in fetched and h not in SKIP_SLUGS)
        if fresh:
            print(f'[dir] 目录页补充种子 {len(fresh)} 个: {fresh[:12]}')
            pending |= set(fresh)
            seeds = sorted(set(seeds) | set(fresh))
        else:
            print('[dir] 目录页无新 handle（快照 503 或已全部抓过）')

    # 1b2. 论坛腿（GPWA 等，每天 1 次：Wayback 最新快照 diff 出新发帖者/新 tipster）
    forum_all = set()
    if state.get('last_forum_date', '') != today:
        forum_users, forum_handles = fetch_forum_users(cfg)
        forum_all = forum_users | forum_handles
        seen_forum = set(state.get('seen_forum_users', []))
        fresh_users = sorted(u for u in forum_users if u not in seen_forum)
        fresh_handles = sorted(h for h in forum_handles if h not in fetched and h not in SKIP_SLUGS)
        if fresh_handles:
            print(f'[forum] 论坛页新 handle {len(fresh_handles)} 个: {fresh_handles[:12]}')
            pending |= set(fresh_handles)
            seeds = sorted(set(seeds) | set(fresh_handles))
        sheet_titles = {r[1] for r in rows if len(r) > 1}
        added = 0
        for u in fresh_users:
            if u in sheet_titles or u in sheet_handles:
                continue
            in_dialog_rows.append(['GPWA论坛', f'@{u}', 'https://www.gpwa.org/forum/', '论坛',
                                   u, '论坛站内信（注册后）', '西语区', '',
                                   '来源: GPWA 论坛活跃发帖者（Wayback 最新快照 diff）', 'A', '待验证', '待注册',
                                   '待确认', now_ts])
            added += 1
        if fresh_users:
            print(f'[forum] 新论坛成员 {len(fresh_users)} 个，入表 {added} 个: {fresh_users[:12]}')

        # pronosticadores WordPress REST API（稳定，比快照强；按文章 ID 去重）
        wp_url = cfg.get('wp_json_url', '')
        if wp_url:
            try:
                html = fetch_page(wp_url, retries=1, timeout=15)
                if html:
                    posts = json.loads(html)
                    seen_wp = set(state.get('seen_wp_posts', []))
                    new_handles, new_invites = [], []
                    for p in posts:
                        pid = p.get('id')
                        if pid in seen_wp:
                            continue
                        seen_wp.add(pid)
                        content = p.get('content', {})
                        content = content.get('rendered', '') if isinstance(content, dict) else str(content)
                        title = p.get('title', {})
                        title = title.get('rendered', '') if isinstance(title, dict) else str(title)
                        for h in RE_HANDLE.findall(content):
                            if h not in SKIP_SLUGS and h not in fetched and h not in pending:
                                new_handles.append(h)
                        for inv in RE_INVITE.findall(content):
                            if inv not in sheet_invites:
                                in_dialog_rows.append(['pronosticadores', f'文章: {title[:40]}', f'https://t.me/+{inv}',
                                                       'Telegram群', '文章内入群直链', f't.me/+{inv}', '待确认', '',
                                                       f'来源: WP API 新文章 {p.get("link", "")}', 'B', '待验证', '待入群', 'casino相关', now_ts])
                                new_invites.append(inv)
                    state['seen_wp_posts'] = sorted(seen_wp)
                    if new_handles:
                        pending |= set(new_handles)
                        seeds = sorted(set(seeds) | set(new_handles))
                    print(f'[forum] WP API: 新文章 {sum(1 for p in posts)} 篇，新handle {len(new_handles)} 个，新入群链 {len(new_invites)} 个')
            except Exception as e:
                print(f'[forum] WP API 失败: {e}')

    # 1c. TG 搜索腿（每轮都搜，关键词池轮转 6/24；只读保护账号）
    if cfg.get('search_enabled', True) and not args.dry_run and len(seeds) < 10:
        pool = cfg.get('search_keywords_pool') or cfg.get('search_keywords', [])
        per_run = max(1, int(cfg.get('search_keywords_per_run', 6)))
        off = state.get('kw_rotation_offset', 0)
        kws = (pool * 2)[off:off + per_run] if pool else cfg.get('search_keywords', [])
        if pool:
            state['kw_rotation_offset'] = (off + per_run) % len(pool)
        print(f'[search] 关键词轮转[{off}:{off + per_run}/{len(pool)}]: {kws}')
        found = tg_search_keywords(kws, cfg.get('search_limit', 10),
                                   cfg.get('search_mode', 'all'))
        fresh = sorted(h for h in found['usernames'] if h not in fetched and h not in SKIP_SLUGS
                       and re.fullmatch(r'[A-Za-z0-9_]{3,32}', h or ''))
        if fresh:
            print(f'[search] TG 搜索腿 {len(fresh)} 个新 handle: {fresh[:12]}')
            pending |= set(fresh)
            seeds = sorted(set(seeds) | set(fresh))
        else:
            print(f'[search] TG 搜索腿 0 个新 handle（命中 {len(found["usernames"])}，均已抓过）')
        sheet_titles = {r[1] for r in rows if len(r) > 1}
        for g in found.get('in_dialog', []):
            t = g.get('title') or ''
            if t and t not in sheet_titles and not re.search(RE_NOISE_LANG, t):  # 英西管线：中文/西里尔等标题跳过
                in_dialog_rows.append(['TG搜索腿(已加入)', t, '', 'Telegram群', 'bot已加入，可查群主',
                                       f"chat_id={g.get('chat_id')}", '待确认', '',
                                       '来源: 账号已加入的匹配群，群主可直接查', 'B', '已验证(账号在群)', '查群主',
                                       classify(t, cfg), now_ts])
        if in_dialog_rows:
            print(f'[search] 账号已加入的匹配群 {len(in_dialog_rows)} 个（可直接查群主）')
        state['last_search'] = today

    # 2. BFS
    new_leads, discovered, errors, invalid_count = [], set(), 0, 0
    frontier = seeds
    for level in range(args.depth + 1):
        next_frontier = []
        for handle in frontier:
            if handle in fetched or len(fetched) - len(state.get('seen_handles', [])) > args.max_new:
                continue
            time.sleep(SLEEP)
            html = fetch_tme(handle)
            if html is None:
                errors += 1
                fetched.add(handle)
                continue
            if 'tgme_page' not in html:  # 验证码/空页 = 抓取失效，不算"无线索"
                invalid_count += 1
                fetched.add(handle)
                continue
            fetched.add(handle)
            invites, chandles, emails, wa, bio, wa_groups = extract(html, handle)
            is_channel = 'tgme_channel_info' in html
            for inv in invites:
                invite_owners[inv].add(handle)
            if is_channel and (invites or emails or wa or bio):
                new_leads.append({'handle': handle, 'bio': bio, 'invites': invites,
                                  'admins': chandles, 'emails': emails, 'wa': wa, 'wa_groups': wa_groups})
            print(f'  [lvl{level}] @{handle} bio="{bio[:50]}" invites={invites[:2]} handles={chandles[:3]}')
            if re.search(RE_NOISE_LANG, bio or ''):
                if re.search(r'[\u4e00-\u9fff]', bio or ''):
                    cn_handles.add(handle)  # 中文 bio：聚类时整体排除该网络
                continue  # 西里尔 bio 不展开下一层（英西管线剪枝），已记录的邀请链仍参与聚类
            discovered.update(chandles)
            if level < args.depth:
                next_frontier += [h for h in chandles if h not in fetched]
        if not next_frontier:
            break
        frontier = next_frontier
        print(f'  → 下一层候选 {len(frontier)} 个')

    # 2b. seed 重扫（bio 变化检测，最多 5 个最久未查）
    last_check = state.get('last_check', {})
    now = datetime.now().isoformat(timespec='seconds')
    rescan = sorted(sheet_handles, key=lambda h: (last_check.get(h, '0000'), h))[:5]
    for handle in rescan:
        time.sleep(SLEEP)
        html = fetch_tme(handle)
        if html is None or 'tgme_page' not in html:
            last_check[handle] = now
            continue
        invites, chandles, emails, wa, bio, wa_groups = extract(html, handle)
        last_check[handle] = now
        new_inv = [i for i in invites if i not in sheet_invites]
        new_hd = [h for h in chandles if h not in fetched and h not in SKIP_SLUGS]
        if new_inv or new_hd:
            print(f'  [rescan] @{handle} 新邀请链={new_inv} 新handle={new_hd[:6]}')
            for inv in new_inv:
                invite_owners[inv].add(handle)
            discovered.update(new_hd)
    state['last_check'] = last_check

    # 2c. 死 handle 健康检查（全表最久未查且本轮未抓的，最多 6 个；连续 2 次失效标"失效"）
    dead = state.get('dead_count', {})
    stale = sorted((h for h in sheet_handles if h not in fetched),
                   key=lambda h: (last_check.get(h, '0000'), h))[:6]
    for handle in stale:
        time.sleep(SLEEP)
        html = fetch_tme(handle)
        ok = bool(html and 'tgme_page' in html)
        last_check[handle] = now
        if not ok:
            dead[handle] = dead.get(handle, 0) + 1
            if dead[handle] >= 2:
                if not args.dry_run and ws is not None and handle in hrow:
                    ws.update(f'K{hrow[handle]}', [['失效(爬虫)']], value_input_option='RAW')
                    print(f'  [health] @{handle} 标记失效 (row {hrow[handle]})')
        else:
            dead[handle] = 0
            invites2, chandles2, _, _, _, _ = extract(html, handle)
            for inv in invites2:
                invite_owners[inv].add(handle)
            discovered.update(chandles2)
    state['dead_count'] = dead

    # 3. 聚类 + 生成行
    networks = {inv: sorted(v) for inv, v in invite_owners.items() if len(v) >= 2}
    now_ts = _local_ts()
    rows = list(in_dialog_rows)
    noise_skipped = 0
    cn_skipped = 0   # 2026-08-24: 中文内容过滤计数（区分于西里尔等其他噪音）
    for lead in new_leads:
        if lead['handle'] in sheet_handles:
            continue
        if re.search(RE_NOISE_LANG, lead['bio'] or ''):  # 英西管线：中文/西里尔等 bio 视为噪音跳过
            noise_skipped += 1
            if re.search(r'[\u4e00-\u9fff]', lead['bio'] or ''):
                cn_skipped += 1
            continue
        net = [inv for inv in lead['invites'] if inv in networks]
        net_note = f" 同网络({net[0][:8]}…)" if net else ''
        contacts = ', '.join([f't.me/+{i}' for i in lead['invites']] + lead['emails'] + [f'wa.me/{w}' for w in lead['wa']])
        rows.append(['TG爬虫', f"@{lead['handle']}", f"https://t.me/{lead['handle']}", 'Telegram',
                     ', '.join(lead['admins'][:5]) or '未公开', contacts or '未公开', '待确认', '',
                     f"{lead['bio'][:70]}{net_note}", 'B', '待验证(爬虫)', '待私信',
                     classify(f"{lead['bio']} {lead['handle']}", cfg), now_ts])
        # 2026-08-24: TG 频道里公开的 wa.me 链接 → 单独成行（WA 线索，直接可加好友）
        for w in lead['wa']:
            rows.append(['WA线索', f"wa.me/{w}", f"https://wa.me/{w}", 'WhatsApp',
                         f"来源TG: @{lead['handle']}", f"wa.me/{w}", '待确认', '',
                         f"TG频道 {lead['handle']} 公开的WA联系方式", 'B', '待验证(WA)', '待加好友',
                         'casino相关' if classify(f"{lead['bio']}", cfg) in ('casino相关', 'casino相关(弱)') else classify(f"{lead['bio']}", cfg), now_ts])
        # 2026-08-24: TG 频道里公开的 WA 群邀请链接 → 单独成行（WA群邀，进群钥匙，只收集不主动进）
        for wg in lead.get('wa_groups') or []:
            rows.append(['WA群邀', f"chat.whatsapp.com/{wg}", f"https://chat.whatsapp.com/{wg}", 'WhatsApp群',
                         f"来源TG: @{lead['handle']}", f"chat.whatsapp.com/{wg}", '待确认', '',
                         f"TG频道 {lead['handle']} 公开的WA群邀请链接", 'B', '待验证(WA)', '待进群',
                         'casino相关' if classify(f"{lead['bio']}", cfg) in ('casino相关', 'casino相关(弱)') else classify(f"{lead['bio']}", cfg), now_ts])
    for inv, owners in networks.items():
        if inv in sheet_invites:
            continue
        if cn_handles & set(owners):  # 2026-08-24: 网络中任一频道 bio 含中文 → 整个网络不入表
            continue
        rows.append(['TG爬虫', f"同网络群({len(owners)}频道)", f"https://t.me/+{inv}", 'Telegram群',
                     f"疑似同运营: {', '.join('@' + h for h in owners[:6])}", f'入群直链 t.me/+{inv}',
                     '待确认', '', '邀请链聚类：多频道共享同一入群链，进群见群主', 'B', '待验证(爬虫)', '待入群私信',
                     '群(进群确认)', now_ts])

    # 3b. 2026-08-24 新增「WA 社群检索腿」—— 只取已加入的 WA 群（WA shim 消息里 @g.us）
    # 安全原则：不主动搜陌生群（WA 无公开搜索 API），仅从 wa_messages.json 已收到的群消息提取，
    # 零新增风控面；产出独立来源行（WA群），联系人为群 jid，供后续建联
    wa_rows = 0
    wa_groups = {}
    try:
        wa_msg_file = os.path.expanduser('~/scripts/wa_messages.json')
        if not os.path.exists(wa_msg_file):
            DEGRADED.append('wa_messages.json 缺失(%s)' % wa_msg_file)
        if os.path.exists(wa_msg_file):
            with open(wa_msg_file, encoding='utf-8') as _f:
                wa_msgs = json.load(_f)
            for m in wa_msgs or []:
                jid = str(m.get('jid') or '')
                if not jid.endswith('@g.us'):
                    continue
                gid = jid.split('@')[0]
                name = (m.get('name') or '').strip() or f'WA群_{gid}'
                text = str(m.get('text') or '')
                if gid not in wa_groups:
                    wa_groups[gid] = {'name': name, 'last_text': text[:80], 'last_ts': m.get('ts') or ''}
                else:
                    if str(m.get('ts') or '') > wa_groups[gid]['last_ts']:
                        wa_groups[gid]['last_text'] = text[:80]
                        wa_groups[gid]['last_ts'] = m.get('ts') or ''
        for gid, g in wa_groups.items():
            if re.search(RE_NOISE_LANG, (g.get('name') or '') + ' ' + (g.get('last_text') or '')):
                cn_skipped += 1  # 2026-08-24: WA 群名/消息含中文 → 不入表
                continue
            rows.append(['WA社群', g['name'], f"https://wa.me/{gid}", 'WhatsApp群',
                         f"群ID {gid}", f"wa.me/{gid}", '待确认', '',
                         f"WA群消息: {g['last_text']}", 'B', '待验证(WA)', '待私信',
                         '群(进群确认)', now_ts])
            wa_rows += 1
        # 2026-08-24: 补充 WA shim /groups（枚举已加入群 + 成员，不依赖群消息）
        try:
            import urllib.request as _ur
            # 2026-10-21: 云端（GitHub Actions）永远连不到本机 wa_bridge —— 直接跳过，
            #   省掉每次 8 秒超时；并明确记为「本机专属功能缺失」，而不是静默少收。
            if os.environ.get("GITHUB_ACTIONS"):
                raise RuntimeError("cloud: wa_bridge 是本机服务（127.0.0.1:8770），云端不可用 → WA 社群段跳过")
            _req = _ur.Request('http://127.0.0.1:8770/groups', headers={'User-Agent': 'Mozilla/5.0'})
            with _ur.urlopen(_req, timeout=8) as _rp:
                _gd = json.loads(_rp.read().decode())
            for g in _gd.get('groups') or []:
                gid = str(g.get('gid') or '')
                if not gid or gid in wa_groups:
                    continue
                gname = (g.get('name') or '').strip() or f'WA群_{gid}'
                if re.search(RE_NOISE_LANG, gname):
                    cn_skipped += 1
                    continue
                members = g.get('participants') or []
                # 2026-09-08 标准修正: 群成员=自己合作群成员=已合作(铁律)，非社群主，不再逐人入表
                # （曾每轮灌入 500+ 行 WA成员 噪音，已从代理表清理。WA社群 本身仍保留=社群主池）
                for p in members:
                    mid = str(p.get('id') or '')
                    if not mid or mid == '447756949523':  # 自己的 WA 号码跳过
                        continue
                    pass  # 成员不再入表
                # 群本身也入表（管理员/可联系）
                rows.append(['WA社群', gname, f"https://wa.me/{gid}", 'WhatsApp群',
                             f"群ID {gid} · {len(members)}成员", f"wa.me/{gid}", '待确认', '',
                             f"已加入WA群 {gname}（{len(members)}成员）", 'B', '待验证(WA)', '待私信',
                             '群(进群确认)', now_ts])
                wa_rows += 1
            if _gd.get('count'):
                print(f'[WA社群] /groups 枚举 {_gd.get("count")} 个群 + 成员线索')
        except Exception as _e:
            DEGRADED.append('wa_bridge 不可用(127.0.0.1:8770): %s' % str(_e)[:60])
            print(f'[WA社群] /groups 跳过（{_e}）')
        if wa_rows:
            print(f'[WA社群] 从已加入群提取 {wa_rows} 个 WA 线索（安全：仅已加入，不主动搜）')
    except Exception as e:
        print(f'[WA社群] 跳过（{e}）')

    # 4. 指标 + 告警
    # 4a. 2026-08-24 同事反馈：前 ~100 条最对口，后面相关性弱 —— 按相关度打分排序 + 置顶高相关
    # 相关度分 = casino_strong 命中×2 + sports×1 + 明确分类加分；写入第 12 列（原「来源」列旁补相关性）
    def relevance_score(r):
        label = str(r[12] if len(r) > 12 else '')  # 第13列 = classify 标签
        s = {'casino相关': 90, 'casino相关(弱)': 70, '混合(casino+体育)': 60, '体育博彩': 40, '群(进群确认)': 80, '待确认': 30}.get(label, 30)
        # 来源权重：爬虫直抓 > 搜索腿；带 WA 联系方式的加分
        src = str(r[0] if len(r) > 0 else '')
        if 'TG爬虫' in src: s += 5
        contacts = str(r[5] if len(r) > 5 else '')
        if 'wa.me' in contacts: s += 5
        return min(s, 100)
    rows.sort(key=relevance_score, reverse=True)  # 高相关在前
    new_rows = []
    for r in rows:
        # 重组：..., classify 标签, 相关度分, 时间戳（第13=相关性分, 14=时间戳）
        if len(r) >= 14:
            new_rows.append(r[:12] + [r[12], relevance_score(r), r[13]])
        else:
            new_rows.append(r + [relevance_score(r), ''])
    rows = new_rows
    # 4b. 2026-08-24 同事反馈：前 ~100 条最对口，后面相关性弱 —— 截断 + 低相关标记
    # 只保留相关度 >= 40 的行（或最多 MAX_ROWS 行），低相关(<40)标记「低相关」便于运营过滤
    MAX_ROWS = 100
    marked = []
    for r in rows:
        score = int(r[13]) if len(r) > 13 and str(r[13]).isdigit() else 0
        r2 = list(r)
        if score < 40:
            r2[11] = '低相关'   # 第12列标记（原「待私信」位）
        marked.append(r2)
    rows = [r for r in marked if int(r[13]) >= 40 or '低相关' not in str(r[11])][:MAX_ROWS]
    # 若全部低相关导致空表，保底保留前 MAX_ROWS（避免整轮无输出）
    if not rows and marked:
        rows = marked[:MAX_ROWS]
    metrics = {'date': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
               'new_handles': len(fetched) - len(state.get('seen_handles', [])),
               'new_leads': len(new_leads), 'networks': len(networks),
               'rows_written': len(rows), 'errors': errors, 'invalid': invalid_count, 'noise_skipped': noise_skipped,
               'cn_skipped': cn_skipped, 'wa_rows': wa_rows,
               'degraded': DEGRADED}   # 2026-08-24: 中文过滤数 + WA群数进指标；2026-10-09: +来源降级
    # 4c. 2026-08-24 降频护号：新线索少 → 拉长间隔（防 TG 高频抓取触发风控）
    #     <5 条 → 2h；5-20 → 45min；>20 → 30min；写 /tmp 供 daemon 读取
    n_lead = len(new_leads)
    next_iv = 1800 if n_lead >= 20 else (2700 if n_lead >= 5 else 7200)
    try:
        with open('/tmp/agent_crawler_next_interval', 'w') as _f:
            _f.write(str(next_iv))
        metrics['next_interval_s'] = next_iv
    except Exception:
        pass
    print('METRICS ' + json.dumps(metrics, ensure_ascii=False))
    if DEGRADED:
        _dline = 'DEGRADED ' + json.dumps(DEGRADED, ensure_ascii=False)
        print(_dline)
        try:
            with open(ALERTS_FILE, 'a', encoding='utf-8') as _af:
                _af.write('%s %s\n' % (metrics['date'], _dline))
        except Exception:
            pass
    history = state.get('history', [])
    history.append({'date': metrics['date'], 'new_leads': metrics['new_leads'], 'rows': metrics['rows_written']})
    history = history[-48:]  # 30min/轮 → 48 轮 = 24h 窗口
    state['history'] = history
    alerted = False
    if len(history) >= 48 and all(h['new_leads'] == 0 and h['rows'] == 0 for h in history) \
            and state.get('alerted_date') != today:
        alerted = True
        state['alerted_date'] = today
        msg = f"[ALERT] {metrics['date']} 连续 24h（48 轮×30min）0 新增，请检查账号风控/规则失效/图收敛"
        print(msg)
        try:
            with open(ALERTS_FILE, 'a') as f:
                f.write(msg + '\n')
        except Exception:
            pass

    # 5. 输出 / 写表
    if args.dry_run:
        for r in rows[:15]:
            print('  NEW:', r[2], '|', r[4], '|', r[5][:50], '|', r[12], '| 相关度', r[13] if len(r) > 13 else '?')
        for inv, owners in networks.items():
            print('  NET:', f't.me/+{inv}', '<-', ', '.join('@' + h for h in owners[:6]))
        print('[dry-run] 未写入 Google Sheet')
        state['cn_handles'] = sorted(cn_handles)
        state['last_metrics'] = metrics
        save_state(state)
        return

    # 写前去重（2026-10-03 修「代理表没查重」）：按链接归一，剔除【表内已有】+【本轮重复】的行。
    # 根因：WA社群/WA线索/网络群 等腿此前不走 sheet_handles/sheet_invites 闸门 → 每轮把同一批 WA 群重复 append。
    _seen_keys = set(existing_keys)
    _deduped_rows = []
    for _r in rows:
        _k = row_key(_r)
        if _k and _k in _seen_keys:
            continue
        if _k:
            _seen_keys.add(_k)
        _deduped_rows.append(_r)
    if len(_deduped_rows) != len(rows):
        print(f'[sheet] 写前去重：移除表内已有/本轮重复 {len(rows) - len(_deduped_rows)} 行')
    rows = _deduped_rows
    metrics['rows_written'] = len(rows)   # 修正：日志「入表行数」应按去重后计数
    if ws is not None:
        ws.update(range_name='M1', values=[['相关性']], value_input_option='RAW')  # 13 列表头（幂等）
        ws.update(range_name='N1', values=[['相关度分']], value_input_option='RAW')  # 14 列：2026-08-24 相关度打分
        ws.update(range_name='O1', values=[['时间戳']], value_input_option='RAW')  # 15 列表头（幂等）
        if rows:
            ws.append_rows(rows, value_input_option='RAW')
        print(f'[sheet] 已 append {len(rows)} 行到「代理」tab')

    # 5b. 运行日志 tab（与发现功能同枢纽管理）
    if sh is not None:
        try:
            log_ws = sh.worksheet('代理爬虫日志')
        except Exception:
            log_ws = sh.add_worksheet(title='代理爬虫日志', rows=200, cols=10)
            log_ws.append_row(['时间', '新handle', '新线索', '聚类网络', '入表行数', '错误', '无效', '中文过滤', 'WA群', '告警'], value_input_option='RAW')
        log_ws.append_row([metrics['date'], metrics['new_handles'], metrics['new_leads'],
                           metrics['networks'], metrics['rows_written'], metrics['errors'],
                           metrics['invalid'], metrics.get('cn_skipped', 0), metrics.get('wa_rows', 0),
                           'ALERT' if alerted else ''], value_input_option='RAW')
        print('[sheet] 运行日志已写入「代理爬虫日志」tab')

    state['seen_handles'] = sorted(fetched)
    state['seen_invites'] = sorted(set(state.get('seen_invites', [])) | set(invite_owners.keys()))
    state['invite_owners'] = {k: sorted(v) for k, v in invite_owners.items()}
    state['cn_handles'] = sorted(cn_handles)  # 2026-08-24: 持久化中文 handle，聚类/后续轮次持续排除
    state['frontier'] = sorted((discovered | pending) - fetched)
    state['seen_forum_users'] = sorted(set(state.get('seen_forum_users', [])) | forum_all)
    state['last_forum_date'] = today
    state['last_metrics'] = metrics
    state['last_run'] = datetime.now().isoformat(timespec='seconds')
    save_state(state)
    print('[state] 状态已保存', STATE_FILE)

if __name__ == '__main__':
    main()
