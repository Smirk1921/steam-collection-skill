# -*- coding: utf-8 -*-
"""步骤1: 全自动抓取 Steam 游戏列表 -> step1/steam_library.csv。

数据来源分层 (详见 references/methods-library.md):
  [本地] 注册表定位 Steam -> appmanifest_*.acf 扫描 -> 已安装集合 (唯一权威)
  [L1] Web API GetOwnedGames   需 key 且账号"游戏详情"公开
  [L2] 社区 games 页            匿名(详情公开)或 steamLoginSecure Cookie; 兼容 2025 改版与
                                2026-11 的 React Query OwnedGames 结构
  [--login] 浏览器登录          Playwright 弹出系统浏览器, 用户手动登录后自动提取 Cookie;
                                自动解析 JWT 有效期, 快过期会提前警告
  [--licenses] 许可页审计       需 Cookie, 只出报告不进列表

用法:
  python fetch_library.py                # L1 失败自动降级 L2
  python fetch_library.py --login        # Cookie 缺失/过期时, 浏览器登录一次
  python fetch_library.py --licenses     # 额外许可页审计
  python fetch_library.py --offline      # 断网应急: 以上次结果为基准仅刷新安装状态
                                         # (不含未安装的已购游戏, 不是完整列表)
  python fetch_library.py --dry-run --no-proxy
依赖: python3 + requests ; --login 另需 pip install playwright (浏览器用系统 Edge/Chrome)
"""
import csv, json, os, re, shutil, sys, time
import requests

BASE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(os.path.dirname(BASE), "step1")
CFG = os.path.join(BASE, "local_config.json")
LIB = os.path.join(OUT_DIR, "steam_library.csv")
DIFF = os.path.join(OUT_DIR, "library_diff.txt")
BAK = os.path.join(OUT_DIR, "backup")
PROFILE = os.path.join(BASE, ".browser_profile")
NOISE = {"228980"}   # Steamworks Common Redistributables 等系统组件
# 非可收藏内容: 好友通行证/试玩版等 (旧版 rgGames 同样不返回)。规则只匹配明确形态:
# Friend's Pass/好友通行证任意位置; 英文 Demo 只认名称结尾(避免误伤 "Demo Builder" 这类正式游戏);
# 中文"试玩(版)"只认结尾。也可在 local_config.json 的 non_game_appids 里追加具体 appid。
NON_GAME_PAT = re.compile(
    r"(friend'?s?\s*pass|好友通行证|(?:^|[\s\-–—:：])demo(?:\s+version)?\s*$|试玩版?$)", re.I)

def load_cfg():
    c = {"steamid32": "", "steamid64": "", "vanity": "", "steam_root": "", "proxy": "",
         "steam_api_key": "", "steam_login_secure": "", "non_game_appids": []}
    if os.path.exists(CFG):
        try: c.update({k: v for k, v in json.load(open(CFG, encoding="utf-8")).items() if k in c})
        except Exception as e: print("WARNING: 配置解析失败(%s), 用环境变量/空默认" % e)
    for k, e in (("steam_api_key", "STEAM_API_KEY"), ("steam_login_secure", "STEAM_LOGIN_SECURE"),
                 ("proxy", "STEAM_PROXY"), ("steamid32", "STEAM_ID32"),
                 ("vanity", "STEAM_VANITY"), ("steam_root", "STEAM_ROOT")):
        if os.environ.get(e): c[k] = os.environ[e]
    return c

def save_cfg(key, val):
    c = json.load(open(CFG, encoding="utf-8")) if os.path.exists(CFG) else {}
    c[key] = val
    json.dump(c, open(CFG, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

def http(cfg, no_proxy=False):
    s = requests.Session()
    s.headers.update({"User-Agent": "steam-library-fetcher/1.0"})
    if cfg["proxy"] and not no_proxy:
        s.proxies = {"http": cfg["proxy"], "https": cfg["proxy"]}
    s.trust_env = False
    return s

def sid64(cfg):
    if cfg.get("steamid64"): return str(cfg["steamid64"])
    assert str(cfg.get("steamid32", "")).isdigit(), "配置缺少 steamid32/steamid64 (或环境变量)"
    return str(76561197960265728 + int(cfg["steamid32"]))

def cookie_expiry(cookie):
    """解析 steamLoginSecure 内嵌 JWT 的 exp。返回 (exp_ts, days_left) 或 None。
    格式: <steamid64>||<JWT>; JWT payload 是 base64url(JSON), 含 exp。"""
    if not cookie: return None
    try:
        import base64
        from urllib.parse import unquote
        v = unquote(str(cookie))
        tok = v.split("||", 1)[1] if "||" in v else v
        payload = tok.split(".")[0]
        payload += "=" * (-len(payload) % 4)
        d = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
        exp = float(d.get("exp") or 0)
        return (exp, (exp - time.time()) / 86400.0) if exp else None
    except Exception:
        return None

def warn_cookie(cfg):
    info = cookie_expiry(cfg.get("steam_login_secure"))
    if not info: return
    exp, days = info
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(exp))
    if days < 0:
        print("WARNING: steamLoginSecure 已于 %s 过期, 在线抓取会失败; 请运行 --login 重新登录"
              " (否则只能 --offline 仅对账安装状态)" % when)
    elif days < 7:
        print("WARNING: steamLoginSecure 将于 %s 过期(还剩 %.1f 天), 建议现在 --login 刷新" % (when, days))

# ---------- 本地安装扫描 ----------

def steam_root(cfg):
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam")
        return winreg.QueryValueEx(k, "SteamPath")[0]
    except Exception:
        return cfg["steam_root"]

def scan_installed(root):
    if not root or not os.path.isdir(root):
        print("WARNING: 未找到 Steam 根目录, 跳过安装状态扫描"); return {}
    libs = [root]
    vdf = os.path.join(root, "steamapps", "libraryfolders.vdf")
    if os.path.exists(vdf):
        t = open(vdf, encoding="utf-8", errors="ignore").read()
        libs += [m.replace("\\\\", "\\") for m in re.findall(r'"path"\s+"([^"]+)"', t)]
    out = {}
    for d in libs:
        sa = os.path.join(d, "steamapps")
        if not os.path.isdir(sa): continue
        for fn in os.listdir(sa):
            if fn.startswith("appmanifest_") and fn.endswith(".acf"):
                try: t = open(os.path.join(sa, fn), encoding="utf-8", errors="ignore").read()
                except OSError: continue
                m = re.search(r'"appid"\s+"(\d+)"', t)
                n = re.search(r'"name"\s+"([^"]*)"', t)
                if m: out[m.group(1)] = n.group(1) if n else ""
    return out

# ---------- 网络: 所有权 ----------

def fetch_api(cfg, no_proxy):
    if not cfg["steam_api_key"]: return None, "未配置 steam_api_key"
    for i in range(3):
        try:
            r = http(cfg, no_proxy).get("https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/",
                params={"key": cfg["steam_api_key"], "steamid": sid64(cfg),
                        "include_appinfo": 1, "include_played_free_games": 1}, timeout=30)
            if r.status_code == 200:
                games = (r.json().get("response") or {}).get("games") or []
                return ({str(g["appid"]): g.get("name", "") for g in games}, None) if games \
                    else (None, "API 返回 0 条(游戏详情隐私非公开或 key 无效)")
            if r.status_code in (429, 500, 502, 503): time.sleep(2 * (i + 1)); continue
            return None, "API HTTP %d" % r.status_code
        except Exception: time.sleep(2 * (i + 1))
    return None, "API 请求失败(已重试)"

def _json_array_before(text, pos):
    """取 pos 之前最近 "data": 后第一个 JSON 数组(括号配对, 跳过字符串)并解析。"""
    j = text.rfind('"data":', 0, pos)
    if j < 0: return None
    k = text.find('[', j, pos)
    if k < 0: return None
    depth, instr, esc = 0, False, False
    for p in range(k, len(text)):
        c = text[p]
        if instr:
            if esc: esc = False
            elif c == '\\': esc = True
            elif c == '"': instr = False
        else:
            if c == '"': instr = True
            elif c == '[': depth += 1
            elif c == ']':
                depth -= 1
                if depth == 0:
                    try: return json.loads(text[k:p + 1])
                    except Exception: return None
    return None

def parse_games(html):
    """兼容旧版 rgGames 数组、2025 改版与 2026-11 改版(React Query OwnedGames) (见 methods-library.md)。"""
    out = {}
    # 2026-11: 数据在 React Query 缓存的 OwnedGames 数组里, 每条带 family_shared 标记;
    # 家庭共享条目不是本账号购买, 与旧版 rgGames 行为保持一致地排除。
    u = html.replace('\\"', '"')
    for m in re.finditer(r'"queryKey":\["OwnedGames"', u):
        for it in _json_array_before(u, m.start()) or []:
            if not isinstance(it, dict): continue
            a = str(it.get("appid") or ""); n = (it.get("name") or "").strip()
            if not a or a == "0" or not n or it.get("family_shared"): continue
            out[a] = n
    if out: return out
    m = re.search(r'rgGames\s*=\s*\[(.*?)\]\s*;', html, re.S)
    if m:
        for blk in re.finditer(r'\{[^{}]*\}', m.group(1)):
            a = re.search(r'"appid"\s*:\s*(\d+)', blk.group(0))
            n = re.search(r'"name"\s*:\s*"((?:[^"\\]|\\.)*)"', blk.group(0))
            if a:
                try: out[a.group(1)] = json.loads('"%s"' % n.group(1)) if n else ""
                except Exception: out[a.group(1)] = n.group(1) if n else ""
        if out: return out
    for name, appid in re.findall(
            r'\\{2,}"name\\{2,}":\\{2,}"([^"\\]*)\\{2,}",\\{2,}"store_url_path\\{2,}":\\{2,}"app/(\d+)', html, re.S):
        if appid == "0" or appid in out: continue
        try: name = json.loads('"%s"' % name)
        except Exception: pass
        if name.strip(): out[appid] = name.strip()
    return out

def fetch_profile(cfg, no_proxy):
    urls = ([("https://steamcommunity.com/id/%s/games/?tab=all" % cfg["vanity"])] if cfg.get("vanity") else []) \
         + ["https://steamcommunity.com/profiles/%s/games/?tab=all" % sid64(cfg)]
    errs = []
    for u in urls:
        for ck in ([False, True] if cfg["steam_login_secure"] else [False]):
            s = http(cfg, no_proxy)
            if ck: s.cookies.set("steamLoginSecure", cfg["steam_login_secure"], domain="steamcommunity.com")
            last = "请求失败(已重试)"
            for i in range(3):
                try:
                    r = s.get(u, timeout=30)
                    if r.status_code == 200:
                        if "/login/" in r.url or ">Sign In<" in r.text:
                            last = "重定向到登录页(游戏详情非公开%s)" % ("; Cookie 无效" if ck else ""); break
                        d = parse_games(r.text)
                        if d: return d, None
                        last = "页面无游戏数据(结构变化?)"; break
                    if r.status_code in (429, 500, 502, 503): last = "HTTP %d" % r.status_code; time.sleep(2 * (i + 1)); continue
                    last = "HTTP %d" % r.status_code; break
                except Exception: time.sleep(2 * (i + 1))
            errs.append("%s%s: %s" % (u, "[Cookie]" if ck else "", last))
    return None, "; ".join(errs)

# ---------- 浏览器登录 ----------

def login_browser(cfg):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("ERROR: 缺少 playwright: pip install playwright"); return None, None
    os.makedirs(PROFILE, exist_ok=True)
    cookie = games = None
    with sync_playwright() as p:
        ctx, last = None, ""
        for ch in ("msedge", "chrome"):
            try:
                ctx = p.chromium.launch_persistent_context(
                    PROFILE, channel=ch, headless=False, viewport={"width": 1100, "height": 850},
                    proxy={"server": cfg["proxy"]} if cfg["proxy"] else None)
                break
            except Exception as e: last = str(e)
        if ctx is None:
            print("ERROR: 无法启动浏览器(%s)。可运行 playwright install chromium 后重试。" % last[:150])
            return None, None
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try: page.goto("https://steamcommunity.com/login/home/", timeout=90000, wait_until="domcontentloaded")
        except Exception as e: print("WARNING: 打开登录页异常(代理开着吗?): %s" % e)
        print("请在弹出的浏览器窗口登录 Steam (支持 Steam Guard)。"
              "建议勾选「记住我」以延长 Cookie 有效期。成功后自动继续, 请勿关闭窗口...")
        deadline = time.time() + 540
        while time.time() < deadline and not cookie:
            try:
                for c in ctx.cookies("https://steamcommunity.com"):
                    if c.get("name") == "steamLoginSecure" and c.get("value"): cookie = c["value"]; break
            except Exception:
                print("浏览器已关闭, 登录中止。"); return None, None
            if not cookie: time.sleep(2)
        if cookie:
            save_cfg("steam_login_secure", cookie)
            print("已提取 steamLoginSecure 并保存到 local_config.json")
            info = cookie_expiry(cookie)
            if info and info[1] > 0:
                print("Cookie 有效期至 %s (剩 %.1f 天)" % (
                    time.strftime("%Y-%m-%d %H:%M", time.localtime(info[0])), info[1]))
                if info[1] < 7:
                    print("WARNING: 有效期很短。登录时请勾选「记住我」, 否则下次运行前可能又要登录")
            elif info:
                print("WARNING: 新 Cookie 显示已过期, 请确认登录是否成功")
            try:
                u = ("https://steamcommunity.com/id/%s/games/?tab=all" % cfg["vanity"]) if cfg.get("vanity") \
                    else "https://steamcommunity.com/profiles/%s/games/?tab=all" % sid64(cfg)
                page.goto(u, timeout=90000, wait_until="domcontentloaded")
                page.wait_for_timeout(2000)
                games = parse_games(page.content())
                print("登录会话内解析: %d 条" % len(games))
            except Exception as e: print("WARNING: 会话内抓取失败(%s), 改走 requests" % e)
        try: ctx.close()
        except Exception: pass
    return cookie, games

# ---------- 许可页审计 ----------

def fetch_licenses(cfg, no_proxy):
    if not cfg["steam_login_secure"]: return None, "未配置 steam_login_secure"
    s = http(cfg, no_proxy)
    s.cookies.set("steamLoginSecure", cfg["steam_login_secure"], domain="store.steampowered.com")
    base = "https://store.steampowered.com/account/licenses/"
    def rows_of(html):
        cur = []
        for blk in html.split('class="license_row')[1:]:
            nm = re.search(r'license_game_name[^>]*>\s*([^<]+?)\s*<', blk)
            if nm: cur.append(nm.group(1))
        return cur
    try: r = s.get(base, timeout=30)
    except Exception as e: return None, "请求失败: %s" % e
    if r.status_code != 200: return None, "HTTP %d (Cookie 可能过期)" % r.status_code
    first = rows_of(r.text); seen, rows = set(first), list(first)
    for q in ("?p=%d", "?ajax=1&p=%d"):
        p, fail = 2, 0
        while p <= 60 and fail < 2:
            try:
                rr = s.get(base + q % p, timeout=30); cur = rows_of(rr.text) if rr.status_code == 200 else []
            except Exception: cur = []
            new = [x for x in cur if x not in seen]
            if not new: fail += 1
            else:
                fail = 0
                for x in new: seen.add(x); rows.append(x)
            p += 1
        if len(rows) > len(first): break
    return rows, (None if len(rows) > len(first) else "翻页失效, 仅第 1 页 %d 行" % len(rows))

# ---------- 主流程 ----------

def main():
    cfg = load_cfg()
    dry = "--dry-run" in sys.argv; no_proxy = "--no-proxy" in sys.argv
    offline = "--offline" in sys.argv and "--login" not in sys.argv
    if not offline and "--login" not in sys.argv:
        warn_cookie(cfg)
    root = steam_root(cfg)
    inst = scan_installed(root)
    print("Steam 根目录: %s | 本地 manifest: %d" % (root or "(未找到)", len(inst)))
    old = list(csv.DictReader(open(LIB, encoding="utf-8-sig"))) if os.path.exists(LIB) else []
    if offline:
        own = {r["appid"]: r["name"] for r in old}
        if not own: print("ERROR: offline 需已有上次结果"); sys.exit(2)
        src = "offline(上次结果; 仅安装状态, 不含未安装游戏)"
        print("WARNING: 离线模式只能对账安装状态; 未安装的已购游戏不会出现在列表里。"
              "要完整列表请运行 --login 在线抓取。")
    else:
        if "--login" in sys.argv:
            cookie, own = login_browser(cfg)
            if not cookie: print("ERROR: 未完成登录"); sys.exit(4)
            cfg["steam_login_secure"] = cookie; src = "浏览器登录会话"
            if not own:
                own, e = fetch_profile(cfg, no_proxy)
                if not own: print("ERROR: 登录后抓取失败: %s" % e); sys.exit(3)
        else:
            own, e1 = fetch_api(cfg, no_proxy)
            if own: src = "Web API"; print("L1 Web API: %d 条" % len(own))
            else:
                print("L1 跳过(%s), 降级 L2 社区页" % e1)
                own, e2 = fetch_profile(cfg, no_proxy)
                if not own:
                    print("ERROR: L2 失败: %s" % e2)
                    print("处理: 1) python fetch_library.py --login 浏览器登录(推荐)")
                    print("      2) 手动填 steam_login_secure 到 local_config.json")
                    print("      3) 应急 --offline 仅刷新安装状态")
                    sys.exit(3)
                src = "社区页(登录会话)"
            print("所有权: %d 条 (来源: %s)" % (len(own), src))
    extra = {a: n for a, n in inst.items() if a not in own and a not in NOISE}
    extra_ids = set(cfg.get("non_game_appids") or [])
    def is_non_game(a, n):
        return a in extra_ids or bool(NON_GAME_PAT.search(n or ""))
    skipped = sorted([a for a in own if is_non_game(a, own[a])], key=int)
    rows = [{"appid": a, "name": own[a] or inst.get(a, ""),
             "store_url": "https://store.steampowered.com/app/%s" % a,
             "installed": "true" if a in inst else "false"}
            for a in sorted(own, key=int) if not is_non_game(a, own.get(a, ""))]
    if skipped:
        print("跳过非可收藏内容(好友通行证/试玩): %s" % ", ".join(
            "%s %s" % (a, own[a]) for a in skipped))
    oldmap = {r["appid"]: r for r in old}
    L = ["游戏列表刷新报告", "时间: %s" % time.strftime("%Y-%m-%d %H:%M:%S"), "来源: %s" % src,
         "旧 %d -> 新 %d (已安装 %d)" % (len(old), len(rows), sum(1 for r in rows if r["installed"] == "true")), "",
         "新增(%d):" % sum(1 for r in rows if r["appid"] not in oldmap),
         "移除(%d):" % sum(1 for a in oldmap if a not in own),
         "仅 manifest 无所有权(疑似家庭共享/免费周末残留, 未写入 %d):" % len(extra)]
    L += ["  ~ %s %s" % (a, extra[a]) for a in sorted(extra, key=int)]
    print("\n".join(L))
    if dry: print("DRY-RUN: 未写入。"); return
    os.makedirs(OUT_DIR, exist_ok=True)
    if old:
        os.makedirs(BAK, exist_ok=True)
        b = os.path.join(BAK, "steam_library_%s.csv" % time.strftime("%Y%m%d_%H%M%S"))
        shutil.copy2(LIB, b); print("旧表备份:", b)
    t = LIB + ".tmp"
    with open(t, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["appid", "name", "store_url", "installed"])
        w.writeheader(); w.writerows(rows)
    os.replace(t, LIB)
    open(DIFF, "w", encoding="utf-8").write("\n".join(L) + "\n")
    print("已写入: %s (%d 条)" % (LIB, len(rows)))
    print("下一步: 向用户确认列表无误后进入步骤2 (分类体系设计)")

if __name__ == "__main__":
    main()
