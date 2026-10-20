"""_gspread429.py — gspread 的 Sheets 配额守卫：客户端【读节流】+ 撞 429 自动【重试】。

用法（在各入口脚本 import gspread 之后）：
    import _gspread429; _gspread429.patch()

背景：云腿与本机脚本共用同一个 service account，Sheets「每分钟读/写请求数」配额会互抢；
旧代码撞 429 直接抛 → 整轮 exit 1（那一小时 0 产出）。这里：
  ① 客户端节流：GET 请求每 60s 不超过 reads_per_min 次（从源头少撞）；
  ② 429 重试：真撞上则指数退避重试，不整轮挂。
"""
import time
import random
import collections


def patch(tries=8, base=8.0, cap=120.0, reads_per_min=120):
    """包 gspread.http_client.HTTPClient.request（幂等；重复调用无副作用）。

    reads_per_min: 每分钟 GET（读）上限；0 = 不节流。默认 120（Google 默认 ~300/min，
                   但本机+云腿共用同一 SA，留足余量）。
    """
    try:
        from gspread.http_client import HTTPClient
        from gspread.exceptions import APIError
    except Exception:
        return
    if getattr(HTTPClient, "_guard429_patched", False):
        return
    _orig = HTTPClient.request
    _reads = collections.deque()          # 最近 60s 内 GET 的时间戳

    def request(self, *a, **k):
        method = str(a[0] if a else k.get("method", "")).upper()
        if reads_per_min and method == "GET":
            now = time.time()
            while _reads and now - _reads[0] > 60:
                _reads.popleft()
            if len(_reads) >= reads_per_min:
                _wait = 60 - (now - _reads[0]) + 0.5
                if _wait > 0:
                    print("     [throttle] Sheets 读达 %d/min 上限，等 %.0fs" % (reads_per_min, _wait), flush=True)
                    time.sleep(_wait)
                now = time.time()
                while _reads and now - _reads[0] > 60:
                    _reads.popleft()
            _reads.append(now)

        last = None
        for i in range(max(1, tries)):
            try:
                return _orig(self, *a, **k)
            except APIError as e:
                s = str(e)
                if "429" in s or "Quota exceeded" in s or "RATE_LIMIT" in s.upper():
                    last = e
                    w = min(cap, base * (2 ** i)) + random.uniform(0, 3)
                    print("     [warn] Sheets 429 配额，%.0fs 后重试(%d/%d)" % (w, i + 1, tries), flush=True)
                    time.sleep(w)
                    continue
                raise
        if last:
            raise last

    HTTPClient.request = request
    HTTPClient._guard429_patched = True
