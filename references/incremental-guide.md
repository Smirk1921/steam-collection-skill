# 增量模式：新增游戏只分类新增部分

适用场景：之前已完成一轮完整分类（`step2/preclassification.csv` 存在且基本完整），
之后买了/领了新游戏、退款移除了游戏、或在别的机器装/卸了游戏。
不重跑全量流程，只处理与总表的差异。**总表本身就是"已分类基准"，不另设状态文件**——
上次分类过什么，总表说了算。

## 流程（六步）

```
① fetch_library.py 刷新列表    diff 报告里已能看到新增/移除
② incremental.py detect       -> step5/diff_report.txt (新增/移除/安装变化/强制重分类)
③ 新装游戏的 A 状态            询问用户逐款指定, 或按 categories.md 的默认档规则填写
④ incremental.py fetch-info   -> 增量抓元数据 (data/info.json 缓存, 只抓缺的 appid)
⑤ 分类新增款                   元数据+名称规则直接分; 拿不准的按 methods-review.md 的
                               子代理协议批量搜索 (prepare 切批 -> 子代理 -> merge)
⑥ 汇报 -> 用户确认 -> apply    合并总表 -> build_collections -> 写前确认 -> write_steam
```

增量量通常只有几款：⑤ 不必派子代理，主会话逐款搜索更快；超过一批（25 款）再走
`prepare`/子代理流水线，协议与格式红线与步骤 3 完全一致。

## 各步要点

### 抓取（fetch_library）
- 启动时会自动解析 `steamLoginSecure` 的 JWT 有效期，快过期/已过期会警告：先 `--login` 再跑，
  否则社区页会 302 到登录页、白跑一轮。
- 在线失败时**不要静默用 `--offline` 凑合**：离线只能对账安装状态，不含未安装的已购游戏。
  实战中离线对账只报 2 款新增，实际漏了 45 款已购买未安装的——必须告诉用户并引导登录补齐。
- 页面 2026-11 又改版过（React Query `OwnedGames`），脚本已适配；若解析条数明显偏少，
  先查 `references/methods-library.md` 的改版说明，不要自己写一次性正则硬凑。

### detect
- `python scripts/incremental.py detect [appid ...]`——位置参数可强制重分类指定款
  （上次待确认的、用户点名的），其行会带上现有分类值进入批次，由子代理/搜索修正。
- 产出 `step5/new_rows.csv`（总表同 12 列格式）：新增款 A/B/C/D/E 为空；
  移除款默认保留在总表，apply --prune 才删除（留在收藏集里无害，Steam 自会忽略）。

### A 状态（增量最容易漏的一步）
- 新装游戏（installed=true）：A 状态是用户个人状态，**必须问用户**（逐款或给默认档），
  填进 `new_rows.csv` 的 A_status 列再 merge/apply；留空会让"正在玩"等状态收藏集缺人。
  用户不想逐款指定：按 categories.md 的 A 规则给默认档（如 A04 暂时搁置）并在汇报中列明。
- 卸载的游戏：apply 自动清空其 A 状态（A 仅限已安装），无需人工处理。
- 工具软件（E01）：不进 A，apply/merge 会自动清。

### fetch-info
- `api.steamcmd.net/v1/info/<appid>`，6 并发、4 次退避重试、缓存 `data/info.json` 原子写、
  只抓缓存里没有的 appid——重复跑安全。
- 标签 ID→名称映射（`data/steam_tags_map.json`，约 430 条）首次自动从
  `store.steampowered.com/tagdata/populartags/english` 抓取；拿不到时标签列保留数字 ID
  （不影响流程，子代理可按名称自行搜索）。
- `_missing`/`_http` 的款会打印警告：按名称 WebSearch 分类，仍定不了 C 的进待确认表。

### 分类与批次（prepare）
- `prepare [--size 25]` 的批次行格式与步骤 3 完全一致，子代理提示词协议直接复用
  `methods-review.md` 的 M3（含格式红线、幂等续跑、防中断）。区别只有一点：新增款没有
  "当前分类"，提示词里说明"按搜索到的事实从零打码"，输出格式不变（7 列 CSV）。
- **新增款必须逐款核对 C 类型**，尤其 C01 视觉小说 / C03 Galgame 这类容易被名称或宽泛标签
  触发的分类。实战案例：动作游戏"影之刃零"被规则标成 C01，用户直接打回。宁可多搜一次，
  也不要凭名字猜；修好后用 `incremental.py detect <appid>` 强制重分类。
- A 状态提案可以用抓取时 `OwnedGames` 里的 `playtime_forever` / `rtime_last_played` 作参考，
  但仍要以用户确认为准。

### merge / apply
- `merge`：修复规则与步骤 3 相同（竖线分隔/漏空列/值前缀/依据逗号），产出
  `step5/preclassification_new.csv` + `pending_confirmation_new.csv` + `change_report.txt`。
- `apply [--dry-run] [--prune]`：
  - 备份总表到 `step2/backup/` 后合并：已有行覆盖（强制重分类，A 留空时保留旧 A），其余追加
  - 同步全部行的 installed 列；变为未安装的行**自动清空 A 状态**
  - 待确认表同步：已确定的移除，新增的追加
  - `--prune` 同时移除已不在库中的行（默认保留仅报告）
  - 已安装但 A 为空的行会逐条列出"需指定 A"——apply 前补齐比写完再返工省事
- apply 后必须重跑 `build_collections.py`，并走步骤 4 的写前确认（Steam 退出 + 备份 + 确认）。

## 汇报模板

向用户汇报后再 apply：

- 新增 X 款（其中已安装 Y）、移除 Z 款、安装状态变化 W 款、强制重分类 V 款
- 新增款分类结果（每款一行：名称 → B/C/D/E，标注依据是元数据还是搜索）
- A 状态指定情况（用户已指定的 / 建议默认档的 / 需要用户补的）
- 待确认清单；移除款的处理方式（保留/删除）
- 确认后：apply -> build_collections -> 写前确认 -> write_steam
