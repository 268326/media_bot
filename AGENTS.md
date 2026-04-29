# Media Bot AI 协作速览（AGENTS）

> 目标：让 AI / 代理在**不重新通读每个脚本**的前提下，快速理解本项目的模块边界、调用关系、修改入口与高风险点。
>
> 适用场景：vibe coding、功能优化、重构、修 bug、加命令、调 STRM/ASS/签到/解锁流程。
>
> 如果你是 AI，建议先读本文件，再按“**任务路由表**”进入对应文件，而不是从 `handlers.py` 到处全文扫描。

---

## 1. 项目一句话

这是一个基于 **aiogram + HDHive Open API** 的 Telegram Bot，核心能力分为 6 组：

1. **HDHive 资源检索 / 提取 / 解锁**
2. **TMDB 关键词检索与候选选择**
3. **每日签到与自动签到**
4. **B 站弹幕 XML 下载**
5. **ASS 菜单：子集化字体内封 / MKV 字幕内封**
6. **STRM 监控重命名 / 归档 / 通知 / 空目录清理**

它已经是“模块化处理”的项目，但**编排层仍集中在 `handlers.py`**，因此做优化时要先判断：

- 这是**UI/消息编排问题**，还是
- **业务逻辑问题**，还是
- **底层服务 / 状态机 / 队列问题**。

---

## 2. 启动入口与总生命周期

### 2.1 程序入口
- `main.py`

### 2.2 启动时做的事
1. 读取 `.env`
2. 初始化日志
3. 创建 `Bot` 和 `Dispatcher`
4. 启动以下后台组件：
   - `strm_notifier`
   - `hdhive_unlock_service`
   - `session_manager`（兼容空实现）
   - `checkin_scheduler`
   - `strm_service`
5. 注册 `handlers.router`
6. `start_polling()` 启动 Bot

### 2.3 停止时做的事
按相反顺序停止后台组件，避免残留任务 / watcher / 队列 worker。

---

## 3. 顶层模块分层

```text
main.py
├── config.py                     # 全局配置与校验
├── handlers.py                   # Telegram 命令 / 回调 / 业务编排总入口
│   ├── formatter.py              # Telegram 文案和按钮构建
│   ├── utils.py                  # 链接解析 / 网盘识别
│   ├── tmdb_api.py               # TMDB 搜索与详情
│   ├── hdhive_client.py          # HDHive facade
│   │   └── hdhive_http_api.py    # HDHive Open API 真正实现
│   │       ├── hdhive_auth.py    # API 认证与请求包装
│   │       └── hdhive_unlock_service.py # 解锁/提取统一排队限速
│   ├── checkin_service.py        # 手动签到
│   ├── ass_service.py            # /ass 菜单服务门面
│   ├── danmu_service.py          # /danmu
│   ├── strm_service.py           # STRM watcher 服务门面
│   └── strm_prune_service.py     # /rm_strm 服务门面
├── checkin_scheduler.py          # 自动签到 cron
├── session_manager.py            # HTTP-only 兼容空实现
├── ass_*.py                      # ASS 子集化/内封流水线
└── strm_*.py                     # STRM 监控 / 状态 / 命名 / 通知 / 清理
```

其中 ASS 相关现在分为两条线：

```text
ass_config.py + ass_pipeline.py + ass_font_pool.py
└── /ass -> 子集化字体

ass_mux_config.py + ass_mux_planner.py + ass_mux_pipeline.py
└── /ass -> 内封字幕
```

---

## 4. 先记住这些“非直觉事实”

### 4.1 HDHive 关键词搜索并不在 HDHive 内实现
- `hdhive_http_api.search_resources()` 当前**直接返回空列表**。
- 原因：**HDHive Open API 没有关键词搜索接口**。
- 所以 `/hdt`、`/hdm` 的关键词流程实际上是：
  1. `tmdb_api.search_tmdb()` 先搜 TMDB
  2. 让用户选候选项
  3. 再用 `get_resources_by_tmdb_id()` 去 HDHive 拉资源列表

> 结论：如果要优化“关键词搜索体验”，优先看 `tmdb_api.py + handlers.py`，不是去补 `hdhive_http_api.search_resources()`。

### 4.2 “免费 / 已解锁资源”最终也走解锁队列拿真实链接
- `fetch_download_link()` 并不会总是直接返回最终网盘链接。
- 当前实现里，它主要负责：
  - 判断是否需要解锁
  - 返回资源网站类型等元信息
- 真正的最终链接提取，仍然统一走：
  - `unlock_resource()` / `unlock_and_fetch()`
  - 即 `hdhive_unlock_service.py` 的统一限速队列

> 结论：如果要优化“提取链接速度 / 排队 / 限速提示”，优先看 `hdhive_unlock_service.py` 和 `handlers.py` 的提取链路。

### 4.3 `session_manager.py` 现在是兼容空实现
- 它保留了旧调用接口：`start/stop/close_session/get_session_count`
- 但**不再维护任何浏览器会话**。

> 结论：不要把“会话状态逻辑”继续往这里塞，除非你要重新引入真正的会话层。

### 4.4 `config.py` 有导入副作用
- `config.py` 在导入时会：
  - 读取 `.env`
  - 执行 `validate_config()`
  - 缺配置时 `sys.exit(1)`

> 结论：任何新脚本如果直接 import `config.py`，都要意识到它会在导入阶段做配置校验。

### 4.5 配置分为“启动时静态读取”和“运行时热读取”两类

#### 启动时静态读取
- `config.py`
- `strm_service.py` 使用的 `STRM_SETTINGS`
- `main.py` 的日志配置

#### 运行时热读取
- `ass_config.py`：每次执行 `/ass -> 子集化字体` 时重新读环境变量
- `ass_mux_config.py`：每次执行 `/ass -> 内封字幕` 时重新读环境变量
- `strm_prune.py`：每次执行 `/rm_strm` 时重新读环境变量

> 结论：
> - 改 STRM watcher 主配置后，通常需要重启服务 / 容器才能完全生效。
> - 改 `/ass` 或 `/rm_strm` 配置时，不一定要重启，命令再次执行即可读到。

### 4.6 `handlers.py` 是编排中心，也是当前最大耦合点
它负责：
- 命令入口
- 回调入口
- 权限检查
- 缓存状态
- 自动推送倒计时任务
- 解锁确认/排队提示
- `/ass` 菜单与字幕内封会话交互
- 直接链接解析

> 结论：改功能时不要默认“所有逻辑都放这里继续堆”；能下沉到 service / formatter / utils 的尽量下沉。

### 4.7 STRM 按“一级目录批次”处理，而不是全局一个大队列
当 `STRM_ONLY_FIRST_LEVEL_DIR=1` 时：
- `WATCH_DIR/剧名A/...` -> 一个批次
- `WATCH_DIR/剧名B/...` -> 另一个批次
- 根目录下直接出现的 `.strm` 文件 -> **单文件模式**，直接归档，不参与目录批次 finalize

### 4.8 `/rm_strm` 默认只预览，不会直接删
- 命令 `/rm_strm` 先跑 `apply_changes=False`
- Telegram 按钮确认后才真正删
- 并且有发起人限制与确认 TTL

### 4.9 `/ass` 现在不是单一路径，而是菜单分流
- `/ass` 先弹 Telegram 菜单
- `🔤 子集化字体` 仍走旧的 `ass_pipeline.py`
- `🎞️ 内封字幕` 走新的 `ass_mux_*` 模块

> 结论：改 `/ass` 时先判断你要改的是“字体子集化线”还是“字幕内封线”。

### 4.10 字幕内封不是无脑扫全目录直接执行，而是“计划 -> 交互修正 -> 确认执行”
- `ass_mux_planner.py` 先扫描 `.mkv` 与同目录 `.ass/.sup`，生成计划
- 用户可在 Telegram 中改：
  - 默认字幕组
  - 默认语言
  - 单集字幕文件
  - 单条字幕轨道的字幕组 / 语言
- 最终才由 `ass_mux_pipeline.py` 调 `mkvmerge` 并发执行

> 结论：如果是“匹配不准 / 轨道名不对 / 想改交互”，优先看 `ass_mux_planner.py + ass_service.py`；如果是“执行慢 / 替换失败 / 并发策略”，优先看 `ass_mux_pipeline.py`。

---

## 5. 主要命令与功能入口

### 5.1 Telegram 命令
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
- `/hdc`
- `/llog`
- `/hdt`
- `/hdm`

### 5.2 `/ass` 的主要回调类型
- `ass_menu:subset`
- `ass_menu:mux_start`
- `ass_mux:toggle_delete`
- `ass_mux:toggle_dry`
- `ass_mux:prompt_group`
- `ass_mux:prompt_lang`
- `ass_mux:refresh`
- `ass_mux:page:<n>`
- `ass_mux:edit_item:<idx>`
- `ass_mux:prompt_subfile:<item>:<sub>`
- `ass_mux:prompt_subgroup:<item>:<sub>`
- `ass_mux:prompt_sublang:<item>:<sub>`
- `ass_mux:run_confirm`
- `ass_mux:run_now`
- `ass_mux:cancel`

---

## 6. 模块职责总表

### 6.1 通用 / 入口层

| 文件 | 作用 | 改动时机 |
|---|---|---|
| `main.py` | 进程入口、日志初始化、后台服务启动/停止 | 改启动顺序、加新后台服务、调全局日志 |
| `config.py` | 全局环境变量读取、静态配置、配置校验 | 新增全局配置、改校验规则、改默认值 |
| `session_manager.py` | 旧接口兼容层，当前是空实现 | 仅在真的要恢复会话层时修改 |
| `utils.py` | HDHive 链接解析、分享网盘识别、少量通用工具 | 改链接识别规则、网盘识别、提取码逻辑 |
| `formatter.py` | Telegram 文案格式化、按钮构建、标签分类 | 改 UI 文案、按钮布局、资源列表展示 |

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
| `ass_pipeline.py` | 子集化字体核心流水线：扫描、解压、建库、subset、embed、汇总 | 改处理步骤、跳过规则、失败汇总 |
| `ass_mux_config.py` | `/ass -> 内封字幕` 运行时配置读取 | 加新目录、并发、通知、临时目录配置 |
| `ass_mux_planner.py` | 扫描 MKV/ASS/SUP、自动匹配、生成/序列化计划 | 改匹配逻辑、轨道名策略、分页预览 |
| `ass_mux_pipeline.py` | 并发执行 `mkvmerge`、失败自动停、原文件替换、可删外挂字幕 | 改执行策略、临时文件、磁盘检查、错误处理 |
| `ass_service.py` | Telegram `/ass` 服务门面、互斥锁、菜单交互、通知 | 改并发控制、会话交互、Telegram 汇总文案 |

---

## 7. 关键业务流

### 7.1 `/ass -> 子集化字体`

入口：`cmd_ass()` -> `callback_ass_menu()` -> `ass_service.run_subset()`

流程：
1. `ass_config.load_ass_settings_from_env()` 读取配置
2. `ass_pipeline.run_ass_pipeline()` 真正执行
3. `scan_root()` 扫 ASS / 字体目录 / 压缩包
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
1. 读取 `ass_mux_config.py` 运行时配置
2. 进入 Telegram 控制面板
3. `ass_mux_planner.build_mux_plan()` 扫描目录并生成计划
4. 用户在 Telegram 中可修改：
   - 默认字幕组 / 默认语言
   - 某集使用的字幕文件
   - 某条字幕轨的字幕组 / 语言
5. 用户切换 `DRY-RUN` / `删除外挂字幕`
6. 用户点击确认执行
7. `ass_mux_pipeline.run_mux_plan()` 并发调用 `mkvmerge`
8. 失败时触发 stop_event，终止其他并发任务
9. 汇总成功/失败并发 Telegram 消息

### 7.3 `/ass -> 内封字幕` 的高风险点
- `ASS_MUX_TMP_DIR` 默认要和视频尽量在同一文件系统；否则若 `ASS_MUX_ALLOW_CROSS_FS=0` 会拒绝执行
- `mkvmerge` 是“写临时文件 -> `os.replace()` 覆盖原 MKV”模式
- 若开启“删除外挂字幕”，只有当前计划里成功执行后的字幕会尝试删除
- 当前只支持 `.ass` / `.sup`，未接 `.srt`
- 当前匹配逻辑要求 **MKV 与字幕在同目录**；递归模式也只是扫描多层目录，但匹配仍按“各自所在目录”进行

---

## 8. 任务路由表

| 需求 | 先看这些文件 | 通常不用先看 |
|---|---|---|
| 改 `/ass` 菜单交互 | `handlers.py`, `ass_service.py` | `strm_*` |
| 改字体子集化流程 | `ass_service.py`, `ass_pipeline.py`, `ass_font_pool.py`, `ass_utils.py` | `ass_mux_*` |
| 改字幕自动匹配 | `ass_mux_planner.py`, `ass_service.py` | `ass_pipeline.py` |
| 改内封执行/并发/替换策略 | `ass_mux_pipeline.py`, `ass_mux_config.py` | `ass_font_pool.py` |
| 改资源搜索交互 | `handlers.py`, `tmdb_api.py`, `formatter.py` | `strm_*`, `ass_*` |
| 改资源提取/解锁 | `handlers.py`, `hdhive_http_api.py`, `hdhive_unlock_service.py` | `checkin_*` |
| 改 STRM 通知/状态机 | `strm_notifier.py`, `strm_watcher.py`, `strm_batch_state.py` | `ass_*` |

---

## 9. 典型误区

- 以为 `/ass` 还是一个“直接开跑”的单命令：不是，现在先弹菜单。
- 以为字幕内封会自动跨目录找字幕：不是，当前按“同目录匹配”设计。
- 以为字幕内封先改计划就会立刻生效：不是，默认字幕组/语言改完后要重新生成计划。
- 以为 `DRY-RUN` 会写临时文件：不会，直接跳过执行与替换。
- 以为所有字幕格式都支持：当前计划/执行只覆盖 `.ass` / `.sup`。

---

## 10. 快速定位建议

### 改 `/ass`
1. `ass_service.py`
2. `ass_mux_planner.py` 或 `ass_pipeline.py`
3. `ass_mux_pipeline.py` 或 `ass_font_pool.py`
4. `handlers.py`
5. `.env.example` / `README.md`

### 改 Docker / 挂载 / 依赖
1. `Dockerfile`
2. `docker-compose.yml`
3. `.env.example`
4. `README.md`

### 改直链解析或普通消息路由
1. `handlers.py` 的 `handle_direct_link()`
2. `utils.py`

---

## 11. 当前生产约束

- `/ass` 任务整体仍由 `ass_service.py` 内的互斥锁串行化，避免字体处理与字幕内封并发打架。
- 字幕内封详细过程必须走 Docker stdout/stderr，便于 `docker compose logs -f media_bot` 直接观察。
- 配置全部通过 `.env` 提供；交互输入全部通过 Telegram 会话完成，不依赖 stdin。
- 挂载目录新增文件时优先先写 workspace 再复制进仓库，避免直接在挂载目录写文件失败的历史问题。
