# 步骤1：抓取游戏列表的方法库

目标产出：`steam_library.csv`（`appid,name,store_url,installed`，UTF-8 BOM）。
所有权（库里有什么）与安装状态（装了什么）来自不同数据源，必须分开获取再合并。

## 数据源总表

| 源 | 地址 | 凭据 | 给什么 | 注意 |
|---|---|---|---|---|
| Web API GetOwnedGames | `api.steampowered.com/IPlayerService/GetOwnedGames/v1/` | API key（[申请](https://steamcommunity.com/dev/apikey)） | 所有权 appid+名称，一次全量 | **要求账号"游戏详情"隐私为公开**，否则返回 0 条（key 归属无关） |
| 社区 games 页 | `steamcommunity.com/id/<vanity>/games/?tab=all` | 匿名 或 steamLoginSecure Cookie | 所有权 | 详情非公开时重定向登录页；2025 改版后页面是 React 内嵌 JSON（见下） |
| 许可和产品序列号激活页 | `store.steampowered.com/account/licenses/` | Cookie | 全部许可（含 CD-Key/订阅）+ 激活日期 | 无 appid，按名称比对做审计；页面 AJAX 翻页 |
| 本地 appmanifest | `<steam_root>/steamapps/libraryfolders.vdf` 列出所有库 → 各库 `appmanifest_*.acf` | 无 | **安装状态（唯一权威）** | Steam 根目录读注册表 `HKCU\Software\Valve\Steam\SteamPath` |
| SteamDB / 商店页 | steambd.net / `store.steampowered.com/app/<id>` | 无 | 单款信息补充 | 用于核对，不宜全库抓 |

**steamLoginSecure Cookie 获取方式**（三选一）：
1. 浏览器控制自动提取（见下节，推荐）
2. 用户手动：浏览器登录 steamcommunity.com → F12 → 应用程序 → Cookie → 复制 `steamLoginSecure` 值
3. 用户从已登录浏览器导出后粘贴给 agent

## Cookie 有效期与 --login

`steamLoginSecure` 的值形如 `<steamid64>||<JWT>`，JWT payload 里带 `exp`。
`scripts/fetch_library.py` 启动时会自动解析，剩余不足 7 天或已过期会打印警告——
看到警告就先跑 `--login`，不要等抓取失败再排查。

实测教训：登录时**不勾「记住我」拿到的 Cookie 可能只有约 1 天有效期**。`--login` 的提示语
已提醒用户勾选；脚本在提取成功后也会打印具体到期时间，有效期过短会再警告一次。
Cookie 过期后的典型现象是：社区 games 页 302 到登录页，甚至新登录后仍被判定为非公开，
所以过期预警比失败后的重试更省事。

## 离线模式（--offline）的边界

`--offline` 以上一次的 `steam_library.csv` 为所有权基准，只用本地 manifest 刷新安装状态：

- **不含未安装的已购游戏**——这是它最大的坑。一次实战中，离线对账只发现 2 款新增，
  实际还有 45 款早已购买但未安装的游戏；用户要求"补齐购买未安装的游戏"后重新登录才拿全。
- 因此只能在明确告知用户"列表不完整"的前提下使用；汇报时必须带出来源和这一限制。
- `librarycache`（`userdata/<id>/config/librarycache`）和 `localconfig.vdf` 都不是所有权
  来源：前者实测 2479 个 appid 而真实库存约 1000，后者可能被 gzip 压缩，都不宜用来判断拥有关系。

## 非可收藏内容的过滤

好友通行证、Demo、试玩版会出现在所有权数组里，但不是可收藏游戏（旧版 rgGames 也不返回）。
脚本按名称自动识别（`Friend's Pass` / `好友通行证` 任意位置；英文 `Demo`、中文 `试玩版`
只认名称结尾，避免误伤 "Demo Builder" 这类正式游戏），并支持在 `local_config.json` 的
`non_game_appids` 里追加 appid。跳过的条目会在报告里列出，不要让它们进入分类表。

## 合并规则

```
所有权列表 (L1/L2) 为"库存里有什么"的唯一基准
manifest 为安装状态: appid 在所有权 ∩ manifest → installed=true
manifest 有而所有权无 → 疑似家庭共享/免费周末残留: 只在报告里列出, 不写入
系统组件过滤: 排除 Steamworks Common Redistributables (appid 228980) 等非游戏条目
名称冲突: 所有权名称为空时用 manifest 的 name 兜底
```

## 浏览器控制登录（推荐的凭据获取方式）

用 Playwright 以**持久化上下文**启动系统浏览器，让用户手动登录，自动提取 Cookie：

```python
from playwright.sync_api import sync_playwright
# 依赖: pip install playwright ; 浏览器用系统自带的 Edge/Chrome(channel), 无需下载内核
with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(
        profile_dir,          # 持久化目录, 保留会话下次免登录
        channel="msedge",     # 失败再试 "chrome"
        headless=False,       # 必须可见, 用户要手动登录
        proxy={"server": proxy} if proxy else None)
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto("https://steamcommunity.com/login/home/", timeout=90000,
              wait_until="domcontentloaded")   # 关键: 社区页慢资源会卡死默认 load 事件
    # 轮询 cookie 直到出现 steamLoginSecure (给用户 5-10 分钟)
    for c in ctx.cookies("https://steamcommunity.com"):
        if c.get("name") == "steamLoginSecure" and c.get("value"):
            save_to_local_config(c["value"])
    ctx.close()
```

要点：
- 用户在弹出的窗口里正常登录（账号密码 + Steam Guard 令牌/邮件验证都能过）
- 检测到 Cookie 后写回本地配置（同目录 `local_config.json`），之后所有请求走 requests + Cookie
- 轮询期间浏览器窗口被用户关闭要捕获异常并优雅退出
- 同一会话可以顺带打开 games 页抓取列表，省一次请求

## 新版社区 games 页解析（2025 改版后）

旧版页面有 `rgGames = [...]` JS 数组；改版后没有了，改为**内嵌 React Query JSON 数据块**。
两种结构都要兼容：

```python
def parse_games(html):
    out = {}
    m = re.search(r'rgGames\s*=\s*\[(.*?)\]\s*;', html, re.S)   # 旧版
    if m:
        for blk in re.finditer(r'\{[^{}]*\}', m.group(1)):
            a = re.search(r'"appid"\s*:\s*(\d+)', blk.group(0))
            n = re.search(r'"name"\s*:\s*"((?:[^"\\]|\\.)*)"', blk.group(0))
            if a: out[a.group(1)] = json.loads('"%s"' % n.group(1)) if n else ""
    if out: return out
    # 新版: name 与 store_url_path 紧邻配对; name 字符集排除引号和反斜杠, 防止跨嵌套 JSON 误抓
    for name, appid in re.findall(
            r'\\{2,}"name\\{2,}":\\{2,}"([^"\\]*)\\{2,}",\\{2,}"store_url_path\\{2,}":\\{2,}"app/(\d+)',
            html, re.S):
        if appid != "0" and appid not in out:
            try: name = json.loads('"%s"' % name)
            except Exception: pass
            if name.strip(): out[appid] = name.strip()
    return out
```

注意：新版页面里个人资料装饰品（Steam 点数商品）也有同名结构，`store_url_path` 以 `app/` 开头
这一条件可以过滤掉绝大部分；appid 0 的条目丢弃。

## 2026-11 再改版：React Query `OwnedGames`

2026-11 起页面换成了单引号转义的 React Query 缓存，`store_url_path` 与 name 不再相邻
（`store_url_path` 只剩在 StoreItem 查询里），上面的正则只能抓到零散条目。可靠做法是取
`"queryKey":["OwnedGames"` 所在 state 的 `"data":[...]` 数组：

- 数组元素形如 `{"appid":10,"name":"Counter-Strike","playtime_forever":83,...,"family_shared":false}`；
- 必须跳过 `family_shared:true`（家庭共享不是本账号购买，旧版 rgGames 同样不返回）；
- 解析前先 `html.replace('\"', '"')` 反转义，再用括号配对截取数组（不要regex整段）。
- 同一元素还带 `playtime_forever`（总分钟数）与 `rtime_last_played`（最近游玩时间戳），
  适合给步骤2 的 A 状态提案做依据（"正在玩"= 最近两周有记录等），但它们只代表游玩历史，
  不代表用户想怎么分类，仍要向用户确认。

`scripts/fetch_library.py` 的 `_json_array_before()` + `parse_games()` 已实现该结构，
旧版与 2025 结构保留为降级分支。同一份页面还会返回 `mapPerfectUnownedGames`（已不拥有的游戏），
不能当作所有权列表使用。

## 许可页审计（可选）

- 首页 `GET /account/licenses/`，行结构 `class="license_row"` 内含 `license_game_name` 与 `license_date_col`
- 翻页参数不确定，逐个尝试 `?p=N`、`?ajax=1&p=N`，能翻出新行即有效，连续 2 页无新行停止
- 页面无 appid，只能按**名称**（casefold 归一）与列表比对，差异项人工判断（免费领取未入库/改名/订阅）

## 产出与汇报

- 写 CSV 前备份旧文件；输出 diff 报告（新增/移除/安装变化/疑似残留）
- 向用户汇报：用了哪个方法、总数、已安装数、任何可疑差异 → **等确认后进步骤2**
