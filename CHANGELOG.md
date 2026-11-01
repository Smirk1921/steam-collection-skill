# 更新日志

## v1.1.2 (2026-11-02)

增量模式实战复盘：修复抓取与分类环节暴露的问题。

- **社区页 2026-11 改版适配**：`fetch_library.py` 新增 `_json_array_before()`，从 React Query
  缓存的 `"queryKey":["OwnedGames"` 解析完整所有权数组（支持多个数组块合并）；跳过
  `family_shared:true`；旧版 rgGames 与 2025 结构保留为降级分支
- **Cookie 有效期自检**：启动时解析 `steamLoginSecure` 内嵌 JWT 的 `exp`，<7 天或已过期时
  打印警告；`--login` 成功后报告到期时间，并提示勾选「记住我」（实测不勾可能只有约 1 天）
- **离线模式显式警示**：`--offline` 的报告与终端都会标注"仅安装状态、不含未安装游戏"，
  SKILL.md 要求在线失败时必须告知用户并引导 `--login`，不能静默降级
- **非可收藏内容过滤通用化**：去掉写死的用户专属 appid，改为名称规则
  （Friend's Pass/好友通行证/Demo/试玩）+ `local_config.json` 的 `non_game_appids`
- 文档：methods-library 增加 Cookie 有效期、离线边界、非可收藏内容、OwnedGames 游玩数据；
  pitfalls 增加社区页再改版/Cookie 过期/离线漏游戏/缓存非所有权/非游戏混入/视觉小说误判；
  incremental-guide 要求新增款逐款核对 C01/C03 并补充抓取阶段的坑

## v1.1.1 (2026-10-03)

文档更新：使用说明不再局限于 ZCode。

- README 新增「安装到你的 AI 助手」章节：OpenAI Codex / Claude Code（cc）/
  dsh（DeepSeek Harness）/ ZCode 四家的导入位置、触发方式与注意事项（ZCode 列第四）
- 快速开始改为通用流程（安装 -> 触发 -> 填配置），未安装时也可直接让 AI 阅读 SKILL.md
- 新手版说明同步改为多工具通用，不再只写 ZCode

## v1.1.0 (2026-09-29)

增量模式：已完成分类后，库里新增游戏只分类新增部分，不再重跑全流程。

- 新增 `scripts/incremental.py`：detect（新增/移除/安装变化/强制重分类 diff）→
  fetch-info（增量抓元数据，缓存续传 + 标签 ID→名称映射自动抓取）→
  prepare（子代理批次切分，协议与步骤3一致）→ merge（复用格式修复规则）→
  apply（备份后合并总表、同步 installed 列、自动清空已卸载款 A 状态、--prune 可选清理）
- 新增 `references/incremental-guide.md`：增量协议、A 状态处理（新装款必须问用户）、
  移除款默认保留策略、汇报模板
- SKILL.md 新增增量模式章节与触发描述；pitfalls 新增增量常见问题
- 以分类总表为唯一已分类基准，不引入独立 state 文件；重复运行幂等

## v1.0.0 (2026-09-26)

首个公开版本。

- 四步流程：抓取游戏列表 → 分类体系与预分类 → 联网逐款复核 → 生成收藏集并写入 Steam
- 抓取方法库：Web API / 社区页(匿名或 Cookie) / 浏览器控制登录 / 许可页审计 / 本地安装扫描
- ABCDE 类目方法论与按列表预生成；编号规范（兜底类最大号、主编号连续）
- 复核标签来源四级递进（steamcmd API → 官方 API → SteamDB/商店页 → 搜索引擎子代理）
- 子代理并发/批次参数化（按用户配额与预算调整），幂等续跑与结果格式自动修复
- 写入协议：Steam 运行守卫 + 双备份 + 原子写入 + 写前强制确认
- 附新手纯文本说明（使用说明-新手版.txt）与实战踩坑清单（references/pitfalls.md）
