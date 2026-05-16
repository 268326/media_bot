# Media Bot AI 协作速览（AGENTS）

> 目标：让 AI / 代理在**不重新通读全部脚本**的前提下，快速理解本项目的模块边界、调用关系、修改入口、生产约束与高风险点。
>
> 适用场景：vibe coding、功能优化、重构、修 bug、加命令、调 STRM/ASS/签到/解锁流程、补部署文档、改 CI/workflow。
>
> 如果你是 AI，建议先读本文件，再按“**任务路由表**”进入对应文件，而不是从 `handlers.py` 开始全文乱扫。

---

## 1. 项目一句话

这是一个基于 **aiogram + HDHive Open API** 的 Telegram Bot，当前核心能力分为 6 组：

1. **HDHive 资源检索 / 提取 / 解锁**
2. **TMDB 关键词搜索与候选选择**
3. **每日签到与自动签到**
4. **B 站弹幕 XML 下载**
5. **ASS 菜单：子集化字体内封 / MKV 字幕内封**
6. **STRM 监控重命名 / 归档 / 通知 / 空目录清理**

它已经具备较强模块化，但**Telegram 编排层依然主要集中在 `handlers.py`**。做改动时要先判断问题属于：

- **UI / 消息流 / 回调编排问题**
- **业务逻辑 / 状态机问题**
- **底层执行 / 队列 / 并发 / 外部接口问题**

---

## 2. 启动入口与生命周期

### 2.1 程序入口
- `main.py`

### 2.2 `.env` 加载规则
当前 `.env` 路径不是写死的，而是按以下优先级解析：

1. `MEDIA_BOT_DOTENV_PATH`
2. `/app/.env`
3. 当前目录 `./.env`
4. 若前两者都不存在，则最终仍回退 `/app/.env`

> 结论：本地调试、Docker 运行、目标机自定义路径三种模式现在都兼容，不要再把文档写成“只支持 `/app/.env`”。

### 2.3 启动时做的事
1. 读取 `.env`
2. 初始化日志（stdout/stderr；可选文件日志）
3. 创建 `Bot` 和 `Dispatcher`
4. 启动以下后台组件：
   - `strm_notifier`
   - `hdhive_openapi_unlock_service`
   - `checkin_scheduler`
   - `strm_service`
5. 启动心跳健康检查写入
6. 注册 `handlers.router`
7. `start_polling()` 启动 Bot

### 2.4 健康检查
项目现在支持：

```bash
python main.py --healthcheck
```

行为：
- 读取内存态健康快照
- 尝试读取心跳文件（默认 `/tmp/media_bot_health.json`）
- 若心跳超过 120 秒未更新，则返回非 0

Docker Compose 已接入：

```yaml
healthcheck:
  test: ["CMD", "python", "main.py", "--healthcheck"]
```

> 结论：改启动逻辑、后台服务、长任务或事件循环时，别忘了检查 `--healthcheck` 是否仍成立。

### 2.5 Telegram 配置约定
- 统一使用 `bot_user_id` 控制可使用机器人的用户 ID（逗号分隔）
- 统一使用 `bot_chat_id` 作为接收通知的用户/群组/频道 Chat ID（逗号分隔）
- 已停止继续维护旧命名为主入口：
  - `ALLOWED_USER_ID`
  - `TGBOT_NOTIFY_CHAT_ID`
  - `TARGET_GROUP_ID`

---

## 3. 目录与模块总览

```text
main.py
├── config.py                     # 全局配置与校验
├── handlers.py                   # Telegram 命令 / 回调 / 业务编排总入口
│   ├── formatter.py              # Telegram 文案和按钮构建
│   ├── utils.py                  # 链接解析 / 网盘识别
│   ├── tmdb_api.py               # TMDB 搜索与详情
│   ├── hdhive_openapi_client.py          # HDHive official OpenAPI facade
│   │   └── hdhive_openapi_api.py         # Bot 业务适配：资源归一化 / 积分 / 提链
│   │       ├── hdhive_openapi_adapter.py # 官方 Python SDK 适配层：认证、重试、错误对象、扩展端点
│   │       ├── hdhive_openapi.py         # vendored 官方最小 Python SDK
│   │       └── hdhive_openapi_unlock_service.py # 官方 unlock 串行队列服务
│   ├── checkin_service.py        # 手动签到
│   ├── ass_service.py            # /ass 菜单服务门面
│   ├── danmu_service.py          # /danmu
│   ├── strm_service.py           # STRM watcher 服务门面
│   └── strm_prune_service.py     # /rm_strm 服务门面
├── checkin_scheduler.py          # 自动签到 cron
├── ass_*.py                      # ASS 子集化/内封流水线
└── strm_*.py                     # STRM 监控 / 状态 / 命名 / 通知 / 清理
```

其中 ASS 相关现在明确分成两条线：

```text
ass_config.py + ass_pipeline.py + ass_font_pool.py
└── /ass -> 子集化字体

ass_mux_config.py + ass_mux_planner.py + ass_mux_pipeline.py + ass_formatter.py + ass_service.py
└── /ass -> 内封字幕
```

---

## 4. 先记住这些“非直觉事实”

### 4.1 HDHive 关键词搜索并不在 HDHive 内实现
- `hdhive_openapi_api.search_resources()` 当前仍直接返回空列表。
- 原因：**HDHive OpenAPI 官方文档没有关键词搜索接口**。
- 底层 HDHive 访问已经切换为官方 Python SDK（`hdhive_openapi.py`）+ `hdhive_openapi_adapter.py` 生产适配层；不要再新增自写裸 HTTP 客户端。
- 所以 `/hdt`、`/hdm` 真实流程是：
  1. `tmdb_api.search_tmdb()` 搜 TMDB
  2. 用户选候选项
  3. 再用 `get_resources_by_tmdb_id()` 去 HDHive 拉资源列表

> 结论：优化搜索体验先看 `tmdb_api.py + handlers.py`，不是补 `hdhive_openapi_api.search_resources()`。

### 4.2 “免费 / 已解锁资源”最终仍走官方 unlock 流程统一提链
- `fetch_download_link()` 只负责用 `GET /api/open/shares/:slug` 做前置检查。
- 只要进入实际提链阶段，最终仍统一调用：
  - `unlock_resource()`
  - `unlock_and_fetch()`
  - 即 `POST /api/open/resources/unlock`
- 不再引入任何本地固定限速策略；官方限制完全以 `429` / `Retry-After` 为准。

> 结论：优化提链速度、排队展示、失败处理，优先看 `hdhive_openapi_unlock_service.py` 和 `handlers.py`。

### 4.3 `session_manager.py` 已删除
- 这个历史文件曾用于旧版会话层，但当前 HDHive 已完全切换为官方 OpenAPI 流程，不再需要它。
- 不要再恢复任何浏览器会话、session_id 或 keep_session 逻辑。

> 结论：HDHive 相关改动不要再依赖或恢复该文件。

### 4.4 `config.py` 有导入副作用
- 导入时会：
  - 读取 `.env`
  - 执行 `validate_config()`
  - 缺关键配置时 `sys.exit(1)`

> 结论：任何脚本只要 import `config.py`，都不是“纯类型导入”，会触发配置校验。

### 4.5 配置分为“启动时静态读取”和“运行时热读取”

#### 启动时静态读取
- `config.py`
- `main.py` 的日志配置
- `strm_service.py` 使用的 `STRM_SETTINGS`

#### 运行时热读取
- `ass_config.py`：每次执行 `/ass -> 子集化字体`
- `ass_mux_config.py`：每次执行 `/ass -> 内封字幕`
- `strm_prune.py`：每次执行 `/rm_strm`

> 结论：
> - 改 STRM watcher 主配置，通常要重启服务 / 容器
> - 改 `/ass` 或 `/rm_strm` 配置，不一定要重启，重新触发命令即可读到

### 4.6 `handlers.py` 仍是最大耦合点
它负责：
- 命令入口
- 回调入口
- 权限检查
- 状态缓存
- 自动添加 / 取消任务
- 解锁确认 / 排队提示
- `/ass` 菜单与字幕内封会话交互
- `/rm_strm` 确认按钮
- 直接链接解析

> 结论：改功能时不要默认“继续往 handlers 堆逻辑”；能下沉到 service / formatter / utils / pipeline 的尽量下沉。

### 4.7 STRM 不是全局一个大队列，而是一级目录批次模型
当 `STRM_ONLY_FIRST_LEVEL_DIR=1` 时：
- `WATCH_DIR/剧名A/...` -> 一个批次
- `WATCH_DIR/剧名B/...` -> 另一个批次
- 根目录直接出现的 `.strm` 文件 -> 单文件模式，直接归档，不参与目录批次 finalize

### 4.8 `/rm_strm` 默认只预览，不会直接删
- `/rm_strm` 先执行 `apply_changes=False`
- Telegram 按钮确认后才真的删
- 有发起人限制 + TTL
- 可选接 Emby 局部刷新

### 4.9 `/llog` 现在是“尾读日志”，不是整文件读入
- 当前实现只从文件尾部读取最后一块内容
- 再截取最近 30 行
- 目的是避免大日志整文件读入造成内存风险

> 结论：不要把 `/llog` 再改回整文件读取。

### 4.10 `/ass` 已经是菜单化系统，不是单一路径命令
- `/ass` 先弹 Telegram 菜单
- `🔤 子集化字体` -> 旧 `ass_pipeline.py`
- `🎞️ 内封字幕` -> `ass_mux_*`

> 结论：改 `/ass` 时先判断是在改“字体子集化线”还是“字幕内封线”。

### 4.11 `/ass -> 内封字幕` 不是扫全目录直接开跑，而是“计划 -> 交互修正 -> 确认执行”
- `ass_mux_planner.py` 扫描 `.mkv` + 同目录 `.ass/.sup` 生成计划
- 用户在 Telegram 中可修改：
  - 默认字幕组
  - 默认语言
  - **并发数（会话级）**
  - 单集字幕文件
  - 单条字幕轨道的字幕组 / 语言
- 最终才由 `ass_mux_pipeline.py` 并发调 `mkvmerge`

> 结论：
> - 匹配不准 / 语言识别 / 轨道名问题：先看 `ass_mux_planner.py`
> - 会话交互 / 按钮 / 输入提示：先看 `ass_service.py + ass_formatter.py + handlers.py`
> - 并发 / 临时文件 / 超时 / 替换策略：先看 `ass_mux_pipeline.py`

### 4.12 `/ass -> 内封字幕` 已有实时进度与会话级并发编辑
当前内封线已经支持：
- 执行中 Telegram 主消息实时显示：`已内封视频 / 总视频`
- 每个 MKV 完成后回刷同一条消息
- DRY-RUN 兼容
- 面板中可点 `⚙️ 并发数`，直接发送正整数修改本次会话并发

> 结论：如果用户说“并发要在 TG 面板里可改”或“执行中要看到进度”，现在已经是现成能力，先别重复造轮子。

---

## 5. 主要命令与功能入口

### 5.1 Telegram 命令
当前已接入：
- `/start`
- `/help`
- `/points`
- `/checkin`
- `/danmu`
- `/ass`
- `/strm_status`
- `/strm_scan`
- `/strm_restart`
- `/rm_strm`
- `/emby_tasks`
- `/hdc`
- `/llog`
- `/hdt`
- `/hdm`

### 5.2 `/ass` 的主要回调类型
当前主要包含：
- `ass_menu:subset`
- `ass_menu:mux_start`
- `ass_mux:toggle_delete`
- `ass_mux:toggle_dry`
- `ass_mux:prompt_group`
- `ass_mux:prompt_lang`
- `ass_mux:prompt_jobs`
- `ass_mux:refresh`
- `ass_mux:preview:summary`
- `ass_mux:preview:list`
- `ass_mux:preview_page:<n>`
- `ass_mux:page:<n>`
- `ass_mux:edit_item:<idx>`
- `ass_mux:prompt_subfile:<item>:<sub>`
- `ass_mux:prompt_subgroup:<item>:<sub>`
- `ass_mux:prompt_sublang:<item>:<sub>`
- `ass_mux:cancel_prompt`
- `ass_mux:back_plan`
- `ass_mux:run_confirm`
- `ass_mux:run_now`
- `ass_mux:cancel`

### 5.3 `/emby_tasks` 的主要回调类型
当前主要包含：
- `emby_task:page:<n>`
- `emby_task:refresh:<page>`
- `emby_task:filter:<mode>`
- `emby_task:detail:<task_id>`
- `emby_task:start:<task_id>`
- `emby_task:stop:<task_id>`
- `emby_task:quick_start:<task_id>`
- `emby_task:toggle_notify:<page>`
- `emby_task:summary:<mode>`

要点：
- 当前默认打开 `💎PRO` 视图，并可展示 PRO 常用任务快捷启动区
- 详情页返回列表时应回到“任务当前所在页”，不要依赖旧 `state.page`
- 轮询通知关闭后再开启时，应先重建快照，避免把关闭期间历史完成任务补发出来

---

## 6. 模块职责总表

### 6.1 通用 / 入口层

| 文件 | 作用 | 改动时机 |
|---|---|---|
| `main.py` | 进程入口、日志初始化、心跳健康检查、后台服务启动/停止 | 改启动顺序、加新后台服务、调全局日志、改健康检查 |
| `config.py` | 全局环境变量读取、静态配置、配置校验 | 新增全局配置、改校验规则、改默认值 |
| `utils.py` | HDHive 链接解析、网盘识别、少量通用工具 | 改链接识别规则、网盘识别、提取码逻辑 |
| `formatter.py` | 普通资源列表 / 错误 / 按钮文案 | 改 `/hdt` `/hdm` 等普通 UI |

### 6.2 Telegram 编排层

| 文件 | 作用 | 改动时机 |
|---|---|---|
| `handlers.py` | 所有命令 / 回调 / 权限 / 状态缓存 / 编排主入口 | 改命令、改交互流程、修回调逻辑、接新服务 |

### 6.3 ASS 流水线

| 文件 | 作用 | 改动时机 |
|---|---|---|
| `ass_config.py` | `/ass -> 子集化字体` 运行时配置读取 | 加新配置、改默认目录/命令 |
| `ass_utils.py` | 扫描、目录准备、命令执行、异常封装 | 改扫描规则、命令执行包装、错误处理 |
| `ass_font_pool.py` | 字体池准备：复制 TTF/TTC、OTF->TTF、name table 复制 | 改字体池策略、支持更多字体格式 |
| `ass_pipeline.py` | 子集化字体核心流水线 | 改处理步骤、跳过规则、失败汇总 |
| `ass_mux_config.py` | `/ass -> 内封字幕` 运行时配置读取 | 加新目录、并发、通知、临时目录配置 |
| `ass_mux_planner.py` | 扫描 MKV/ASS/SUP、自动匹配、生成/序列化计划 | 改匹配逻辑、语言识别、轨道名策略、分页预览 |
| `ass_mux_pipeline.py` | 并发执行 `mkvmerge`、失败自动停、原文件替换、可删外挂字幕、实时进度事件 | 改执行策略、临时文件、磁盘检查、超时、错误处理 |
| `ass_formatter.py` | `/ass` 菜单、面板、确认页、执行中、汇总等全部 TG 文案与按钮 | 改手机端文案、按钮布局、执行态展示 |
| `ass_service.py` | `/ass` 服务门面、互斥锁、会话状态、输入应用、通知 | 改会话交互、会话级并发数、汇总通知 |

### 6.4 Emby 任务相关

| 文件 | 作用 | 改动时机 |
|---|---|---|
| `emby_task_service.py` | Emby / Jellyfin ScheduledTasks 拉取、启动/停止、后台轮询通知、状态文件持久化 | 改服务端 API、重试、轮询通知、状态持久化 |
| `emby_task_formatter.py` | `/emby_tasks` 面板、详情页、分类统计、筛选与快捷按钮 | 改任务展示文案、分页、筛选、手机端按钮布局 |

### 6.5 STRM 相关

| 文件 | 作用 | 改动时机 |
|---|---|---|
| `strm_service.py` | watcher 服务门面 | 改 start/stop/restart/scan |
| `strm_watcher.py` | 实际监控、批次处理与归档 | 改监控事件处理、归档时机 |
| `strm_batch_state.py` | manifest/计划状态机 | 改 pending/processing/done/failed/missing 逻辑 |
| `strm_notifier.py` | Telegram 通知聚合与明细 | 改通知格式、明细裁剪、群推送 |
| `strm_prune.py` | 空目录扫描/删除核心 | 改删除策略、根目录保护 |
| `strm_prune_service.py` | `/rm_strm` 服务门面 | 改 Bot 接入层 |
| `strm_prune_emby.py` | Emby 局部刷新 | 改刷新 API 调用 |
| `strm_naming.py` | `.strm` 命名逻辑 | 改来源标签 / 发布组 / 杜比视界等命名规则 |

---

## 7. 关键业务流

### 7.1 `/ass -> 子集化字体`
入口：`cmd_ass()` -> `callback_ass_menu()` -> `ass_service.run_subset()`

流程：
1. `ass_config.load_ass_settings_from_env()` 读取配置
2. `ass_pipeline.run_ass_pipeline()` 真正执行
3. 扫 ASS / 字体目录 / 压缩包
4. 解压 `7z/zip`
5. `FontPoolBuilder.build()` 构建纯 TTF 字体池
6. `assfonts -b` 建字体数据库
7. 对每个 ASS：
   - `assfonts -s` 做 subset
   - `assfonts -e` 做 embed
8. 汇总成功/失败并发 Telegram 消息

### 7.2 `/ass -> 内封字幕`
入口：`cmd_ass()` -> `callback_ass_menu()` -> `ass_service.start_mux_session()`

流程：
1. `ass_mux_config.py` 读取运行时配置
2. 进入 Telegram 控制面板
3. `ass_mux_planner.build_mux_plan()` 扫描目录并生成计划
4. 用户在 Telegram 中可修改：
   - 默认字幕组
   - 默认语言
   - 并发数
   - 某集使用的字幕文件
   - 某条字幕轨的字幕组 / 语言
5. 用户切换 `DRY-RUN` / `删除外挂字幕`
6. 用户点击确认执行
7. `ass_mux_pipeline.run_mux_plan()` 并发调用 `mkvmerge`
8. 执行中通过 `progress_callback -> handlers.pump_ass_mux_progress()` 回刷实时进度消息
9. 失败时触发 `stop_event`，终止其他并发任务
10. 汇总成功/失败并发 Telegram 消息

### 7.3 `/ass -> 内封字幕` 的高风险点
- `ASS_MUX_TMP_DIR` 最好和视频在同一文件系统；否则当 `ASS_MUX_ALLOW_CROSS_FS=0` 会直接拒绝执行
- `mkvmerge` 使用“写临时文件 -> `os.replace()` 覆盖原 MKV”模式
- 删除外挂字幕现在是**全批次成功后统一删除**，不再是单集成功后立删
- 当前只支持 `.ass` / `.sup`，未接 `.srt`
- 匹配逻辑要求 **MKV 与字幕在同目录**；递归模式只是扫描多层目录，匹配仍按各自目录进行
- 已接入标准模式超时保护：
  - 空闲超时：1800s
  - 软告警：7200s
  - 极限保险：43200s

### 7.4 `/rm_strm`
入口：`cmd_rm_strm()` -> `strm_prune_service.run(apply_changes=False)` -> 按钮确认 -> `apply_changes=True`

关键点：
- 默认预览，不直接删除
- 有发起人校验
- 有确认 TTL
- 可选 Emby 刷新

---

## 8. 任务路由表

| 需求 | 先看这些文件 | 通常不用先看 |
|---|---|---|
| 改 `/ass` 菜单交互 | `handlers.py`, `ass_service.py`, `ass_formatter.py` | `strm_*` |
| 改字体子集化流程 | `ass_service.py`, `ass_pipeline.py`, `ass_font_pool.py`, `ass_utils.py` | `ass_mux_*` |
| 改字幕自动匹配 | `ass_mux_planner.py`, `ass_service.py` | `ass_pipeline.py` |
| 改内封执行/并发/替换策略 | `ass_mux_pipeline.py`, `ass_mux_config.py` | `ass_font_pool.py` |
| 改内封执行中文案/进度 | `ass_formatter.py`, `handlers.py`, `ass_mux_pipeline.py` | `ass_pipeline.py` |
| 改资源搜索交互 | `handlers.py`, `tmdb_api.py`, `formatter.py` | `strm_*`, `ass_*` |
| 改资源提取/解锁 | `handlers.py`, `hdhive_openapi_api.py`, `hdhive_openapi_adapter.py`, `hdhive_openapi_unlock_service.py` | `checkin_*` |
| 改 STRM 通知/状态机 | `strm_notifier.py`, `strm_watcher.py`, `strm_batch_state.py` | `ass_*` |
| 改 `/rm_strm` | `handlers.py`, `strm_prune_service.py`, `strm_prune.py`, `strm_prune_emby.py` | `ass_*` |
| 改健康检查/启动 | `main.py`, `docker-compose.yml` | `ass_*`, `strm_naming.py` |
| 改 CI / Docker 构建触发规则 | `.github/workflows/docker-image.yml` | `handlers.py` |

---

## 9. 典型误区

- 以为 `/ass` 还是“直接开跑”的单命令：不是，现在是菜单式。
- 以为字幕内封会跨目录自动找字幕：不是，当前仍按“同目录匹配”。
- 以为默认字幕组 / 默认语言修改后会立刻影响已有计划：不是，改完通常要重新扫描生成新计划。
- 以为会话里改并发数会回写 `.env`：不会，当前只是本次会话级修改。
- 以为 `DRY-RUN` 会写临时文件：不会，直接跳过真实执行与替换。
- 以为 `/llog` 会读完整日志文件：不会，当前只尾读。
- 以为所有字幕格式都支持：当前只覆盖 `.ass` / `.sup`。
- 以为改一行文档也会触发 GHCR Docker 构建：现在 workflow 已配置 `paths-ignore`，纯 Markdown / `文档/**` 改动不触发 push 构建。

---

## 10. CI / Workflow 约定

### 10.1 当前 workflow
- `.github/workflows/docker-image.yml`
- 作用：构建并推送 GHCR Docker 镜像

### 10.2 当前触发规则
当前对 `push`：
- 分支：`main` / `master`
- tag：`v*`
- 手动：`workflow_dispatch`

并且已配置：

```yaml
paths-ignore:
  - "*.md"
  - "**/*.md"
  - ".env.example"
  - "文档/**"
```

### 10.3 这意味着什么
- **只改文档或示例配置**（如 `README.md`、`DEPLOY.md`、`AGENTS.md`、`.env.example`、`文档/**`）
  - 不会触发 push 构建
- **改 Python / Docker / workflow / compose / env 示例**
  - 仍会触发 push 构建
- **打 tag `v*`**
  - 仍会触发构建（`paths-ignore` 不影响 tag 发布这类明确发版动作）

> 结论：如果你的诉求是“更新文档不要触发 Docker 构建”，当前规则已经满足。

---

## 11. 快速定位建议

### 改 `/ass`
1. `ass_service.py`
2. `ass_formatter.py`
3. `ass_mux_planner.py` 或 `ass_pipeline.py`
4. `ass_mux_pipeline.py`
5. `handlers.py`
6. `.env.example` / `README.md` / `AGENTS.md`

### 改 Docker / 挂载 / 依赖 / 健康检查
1. `Dockerfile`
2. `docker-compose.yml`
3. `main.py`
4. `.env.example`
5. `README.md`
6. `DEPLOY.md`

### 改直链解析或普通消息路由
1. `handlers.py` 的 `handle_direct_link()`
2. `utils.py`

### 改 workflow
1. `.github/workflows/docker-image.yml`
2. 看是否会影响 tag 发版、主分支 push、手动触发

---

## 12. 当前生产约束

- `/ass` 整体仍由 `ass_service.py` 内部互斥锁串行化，避免字体处理与字幕内封并发打架。
- `/ass` 运行细节主要走 Docker stdout/stderr，便于 `docker compose logs -f media_bot` 观察。
- `/llog` 仅在显式开启文件日志时可用；否则应提示用户直接看 Docker 日志。
- 配置全部通过 `.env` 提供；交互输入全部通过 Telegram 会话完成，不依赖 stdin。
- Compose 当前默认本地构建 `build: .`，不是默认远程镜像。
- 健康检查依赖 `python main.py --healthcheck` 与心跳文件，不要随意删改。
- 挂载目录新增文件时，若直接写挂载目录曾出现权限/写入问题，优先先写 workspace 再复制进仓库。
