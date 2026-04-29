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
5. **ASS 字幕纯 TTF 子集化字体内封**
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
│   ├── ass_service.py            # /ass 命令入口
│   ├── danmu_service.py          # /danmu
│   ├── strm_service.py           # STRM watcher 服务门面
│   └── strm_prune_service.py     # /rm_strm 服务门面
├── checkin_scheduler.py          # 自动签到 cron
├── session_manager.py            # HTTP-only 兼容空实现
├── ass_*.py                      # ASS 子集化/内封流水线
└── strm_*.py                     # STRM 监控 / 状态 / 命名 / 通知 / 清理
```

---

## 4. 先记住这些“非直觉事实”

这些点很重要，很多优化如果忽略它们，会改错地方：

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
- `ass_config.py`：每次执行 `/ass` 时重新读环境变量
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
- 直接链接解析

> 结论：改功能时不要默认“所有逻辑都放这里继续堆”；能下沉到 service / formatter / utils 的尽量下沉。

### 4.7 STRM 按“一级目录批次”处理，而不是全局一个大队列
当 `STRM_ONLY_FIRST_LEVEL_DIR=1` 时：
- `WATCH_DIR/剧名A/...` -> 一个批次
- `WATCH_DIR/剧名B/...` -> 另一个批次
- 根目录下直接出现的 `.strm` 文件 -> **单文件模式**，直接归档，不参与目录批次 finalize

> 结论：改 STRM finalize / manifest / 通知聚合时，要先区分“目录批次”和“根目录单文件”两种路径。

### 4.8 `/rm_strm` 默认只预览，不会直接删
- 命令 `/rm_strm` 先跑 `apply_changes=False`
- Telegram 按钮确认后才真正删
- 并且有发起人限制与确认 TTL

### 4.9 `/ass` 的关键前提不是直接内封，而是先构建“纯 TTF 字体池”
流程是：
1. 扫描 ASS / 字体目录 / 压缩包
2. 自动解压 `7z/zip`
3. 把 OTF 前置转成 TTF
4. 复制原字体 name table
5. 基于纯 TTF/TTC 建库
6. 再做 subset + embed

> 结论：如果你看到字体匹配异常，不一定是 `assfonts` 本身，可能是字体池构建环节。

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
- `/hdc`（取消自动添加到 Symedia）
- `/llog`
- `/hdt`
- `/hdm`

### 5.2 非命令入口
- 直接发送 HDHive 链接（受 `HDHIVE_PARSE_INCOMING_LINKS` 控制）
- 点击资源按钮 / 解锁按钮 / 分页按钮 / 清理确认按钮

### 5.3 主要回调类型
- `pf:`：资源网盘筛选
- `tmdb_page:`：TMDB 候选分页
- `movie_#:` / `tv_#:`：资源选择
- `unlock:`：确认解锁
- `cancel_unlock`
- `send_to_group:`：手动推送 115 到 Symedia
- `rm_strm_confirm:` / `rm_strm_cancel:`：空目录清理确认
- `select_tmdb:`：选择 TMDB 候选项

---

## 6. 模块职责总表

## 6.1 通用 / 入口层

| 文件 | 作用 | 改动时机 |
|---|---|---|
| `main.py` | 进程入口、日志初始化、后台服务启动/停止 | 改启动顺序、加新后台服务、调全局日志 |
| `config.py` | 全局环境变量读取、静态配置、配置校验 | 新增全局配置、改校验规则、改默认值 |
| `session_manager.py` | 旧接口兼容层，当前是空实现 | 仅在真的要恢复会话层时修改 |
| `utils.py` | HDHive 链接解析、分享网盘识别、少量通用工具 | 改链接识别规则、网盘识别、提取码逻辑 |
| `formatter.py` | Telegram 文案格式化、按钮构建、标签分类 | 改 UI 文案、按钮布局、资源列表展示 |

## 6.2 Telegram 编排层

| 文件 | 作用 | 改动时机 |
|---|---|---|
| `handlers.py` | 所有命令 / 回调 / 权限 / 状态缓存 / 编排主入口 | 改命令、改交互流程、修回调逻辑、接新服务 |

`handlers.py` 内当前还承载了这些内存状态：
- `pending_sa_tasks`：待自动推送到 Symedia 的倒计时任务
- `rm_strm_pending_confirms`：待确认的空目录清理任务
- `resource_website_cache`：资源 -> 网盘类型缓存
- `resource_list_state`：资源列表消息态
- `tmdb_search_state`：TMDB 候选分页态

> 如果出现“按钮点击错乱 / 过期 / 用户串操作 / 倒计时取消异常”，大概率先查这里。

## 6.3 HDHive 访问层

| 文件 | 作用 | 改动时机 |
|---|---|---|
| `hdhive_auth.py` | 认证 Session、统一请求包装、OpenAPIError | 新增 Open API 调用、改认证头、改错误包装 |
| `hdhive_http_api.py` | HDHive Open API 的真实实现 | 改资源详情、积分、解锁、TMDB 资源获取逻辑 |
| `hdhive_client.py` | Facade，当前只是转发到 `hdhive_http_api.py` | 基本不需要改，除非切换底层实现 |
| `hdhive_unlock_service.py` | 解锁/提取统一排队、限速、等待提示回调 | 改速率限制、排队策略、worker 模型、等待提示节奏 |

## 6.4 TMDB 层

| 文件 | 作用 | 改动时机 |
|---|---|---|
| `tmdb_api.py` | TMDB 搜索、详情获取、结果排序归一化 | 改候选排序、返回条数、海报/详情展示策略 |

## 6.5 签到层

| 文件 | 作用 | 改动时机 |
|---|---|---|
| `checkin_service.py` | 手动签到、签到前后积分读取 | 改签到请求、积分解析、错误返回结构 |
| `checkin_scheduler.py` | APScheduler 自动签到、失败通知 | 改 cron 行为、失败通知方式、时区处理 |

## 6.6 弹幕层

| 文件 | 作用 | 改动时机 |
|---|---|---|
| `danmu_service.py` | B 站链接解析、短链解析、CID 获取、XML 下载 | 扩展支持更多 B 站链接形式、改文件名规则 |

## 6.7 ASS 流水线

| 文件 | 作用 | 改动时机 |
|---|---|---|
| `ass_config.py` | `/ass` 运行时配置读取 | 加新配置、改默认目录/命令 |
| `ass_utils.py` | 扫描、目录准备、命令执行、异常封装 | 改扫描规则、命令执行包装、错误处理 |
| `ass_font_pool.py` | 字体池准备：复制 TTF/TTC、OTF->TTF、name table 复制 | 改字体池策略、支持更多字体格式 |
| `ass_pipeline.py` | `/ass` 核心流水线：扫描、解压、建库、subset、embed、汇总 | 改处理步骤、跳过规则、失败汇总 |
| `ass_service.py` | Telegram `/ass` 服务门面、互斥锁、通知 | 改并发控制、Telegram 汇总文案、通知目标 |

## 6.8 STRM 监控层

| 文件 | 作用 | 改动时机 |
|---|---|---|
| `strm_config.py` | STRM watcher 配置模型 | 加 watcher 配置字段 |
| `strm_reason.py` | 原因码 / 条目状态 / 批次状态 / 人类可读文案 | 扩展错误码、状态机状态 |
| `strm_probe.py` | 读取 `.strm` 里的 URL，执行 ffprobe | 改 ffprobe 参数、超时、重试策略 |
| `strm_naming.py` | 从 ffprobe 数据解析媒体信息并生成新文件名 | 改命名规则、音视频标签解析 |
| `strm_batch_state.py` | 批次 manifest 持久化、reconcile、finalize 判定、统计 | 改状态机、恢复策略、统计口径 |
| `strm_notifier.py` | STRM 归档通知聚合、延迟 flush、TG 推送 | 改通知文案、聚合粒度、flush 触发策略 |
| `strm_watcher.py` | STRM 核心：监听、扫描、提交任务、重命名、移动、finalize | 改 watcher 主行为、任务调度、归档逻辑 |
| `strm_service.py` | watcher 服务门面，提供 start/stop/scan/restart/status | 改外部控制接口、状态输出 |

## 6.9 STRM 空目录清理层

| 文件 | 作用 | 改动时机 |
|---|---|---|
| `strm_prune.py` | 空目录扫描与删除核心逻辑 | 改“什么算可删目录”、保护规则、根目录扫描逻辑 |
| `strm_prune_emby.py` | 删除后通知 Emby 局部刷新 / 库刷新 | 改 Emby 接口、重试、路径映射 |
| `strm_prune_service.py` | `/rm_strm` 服务门面、预览/实删结果汇总 | 改执行模式、输出摘要、状态缓存 |

---

## 7. 关键业务流

## 7.1 HDHive 搜索 / 提取 / 解锁主流程

### A. `/hdt` / `/hdm`
入口：`handlers.handle_search()`

流程：
1. 权限检查
2. 解析输入：
   - HDHive 资源链接 -> `handle_resource_link()`
   - HDHive TMDB 页链接 -> `handle_tmdb_link()`
   - 关键词 -> `handle_keyword_search()`

### B. 关键词搜索
入口：`handle_keyword_search()`

流程：
1. `tmdb_api.search_tmdb(keyword, media_type)`
2. 返回多个候选项 -> 发送分页候选按钮
3. 用户点击 `select_tmdb:` -> `callback_select_tmdb()`
4. 调 `get_tmdb_details()` 补详情（海报/简介）
5. 调 `get_resources_by_tmdb_id()` 获取资源列表
6. 用 `formatter.format_resource_list()` 渲染

### C. 资源选择 / 提取
入口：
- `handle_resource_link()`
- `callback_get_resource()`
- `handle_direct_link()`

共同流程：
1. `fetch_download_link()` 先判断资源状态
2. 若 `need_unlock=True` -> `handle_unlock_required()`
3. 若无需确认 -> `fetch_download_link_and_handle_result()`
4. 内部统一调用 `unlock_resource()` 进入限速队列
5. 提取成功后 -> `handle_link_extracted()`
6. 若是 115 且允许推送 -> 创建 `auto_add_to_sa()` 倒计时任务

### D. 你要改哪里？
- 改排队文案：`handlers.update_unlock_queue_notice()`
- 改限速策略：`hdhive_unlock_service.py`
- 改资源列表 UI：`formatter.py`
- 改自动推送到 Symedia：`handlers.handle_link_extracted()` + `auto_add_to_sa()`
- 改 HDHive 接口：`hdhive_http_api.py`

---

## 7.2 签到流程

### 手动签到
- 入口：`cmd_checkin()`
- 核心：`checkin_service.daily_check_in()`

### 自动签到
- 入口：`checkin_scheduler.start()`
- 定时任务：`_run_checkin_job()`
- 失败通知：`_notify_failure()` 发给 `ALLOWED_USER_ID`

### 修改入口
- 改签到 API：`checkin_service.py`
- 改自动签到触发：`checkin_scheduler.py`
- 改 TG 展示：`handlers.py`

---

## 7.3 `/ass` 流程

入口：`cmd_ass()` -> `ass_service.run()`

流水线：
1. `ass_config.load_ass_settings_from_env()` 读取配置
2. `ass_pipeline.run_ass_pipeline()` 真正执行
3. `scan_root()` 扫 ASS / 字体目录 / 压缩包
4. 解压 `7z/zip`
5. `FontPoolBuilder.build()` 构建纯 TTF 字体池
   - `.ttf/.ttc` 直接复制
   - `.otf` 转 `.ttf`
   - 复制 name table
   - `.otc` 当前跳过
6. `assfonts -b` 建字体数据库
7. 对每个 ASS：
   - `assfonts -s` 做 subset
   - `assfonts -e` 做 embed
8. 汇总成功/失败列表并发 Telegram 消息

### 修改入口
- 改扫描规则：`ass_utils.py`
- 改字体池构建：`ass_font_pool.py`
- 改处理步骤：`ass_pipeline.py`
- 改命令入口和互斥：`ass_service.py`

### 典型误区
- 以为 `/ass` 直接操作原字体目录：不是，它会先进入工作目录构建临时字体池。
- 以为所有字体都原样支持：当前 `.otc` 会跳过。

---

## 7.4 STRM watcher 流程

入口：`strm_service.start()` -> `StrmWatcher.start()`

主要组件：
- `Coordinator`：内存态协调器，管理 inflight、recent_done、folder state
- `StrmBatchState`：磁盘 manifest 状态机
- `ThreadPoolExecutor`：并发处理 `.strm`
- `inotifywait`：增量监听
- `finalize_loop()`：目录批次完成后整体归档

### 单个 `.strm` 的处理流程
1. `submit_one()` 提交处理任务
2. 若属于目录批次，先 `mark_processing()`
3. `process_strm_file()`：
   - 读出 URL (`read_strm_url`)
   - `ffprobe` 探测 (`run_ffprobe`)
   - `parse_media_info()` 解析音视频信息
   - `generate_new_name()` 生成标准名
   - 若同名 -> `already_ok`
   - 若不同名 -> 重命名 `.strm`
   - 同步重命名同目录字幕
4. 成功：
   - 目录批次 -> 写 manifest done/already_ok
   - 根目录单文件 -> 直接移到 DONE
5. 失败：
   - 写 manifest failed
   - 文件移到 FAILED
6. 同步记录给 `strm_notifier`

### 目录批次 finalize 流程
1. `finalize_loop()` 周期性检查目录是否满足 finalize 条件
2. `StrmBatchState.finalize_decision()` 判断是否 ready
3. ready 时 `move_done_folder()` 整个目录搬到 DONE
4. `strm_notifier.record_folder_completed()` 聚合发送通知

### 字幕联动规则
- 同基名字幕扩展名支持：`.ass/.srt/.sup`
- 重命名 STRM 时同步重命名字幕
- 移动 DONE/FAILED 时同步移动字幕

### 修改入口
- 改命名规则：`strm_naming.py`
- 改 ffprobe 策略：`strm_probe.py`
- 改状态机：`strm_batch_state.py` + `strm_reason.py`
- 改目录 finalize 时机：`strm_watcher.py`
- 改 TG 汇总通知：`strm_notifier.py`
- 改对外服务接口：`strm_service.py`

### 典型误区
- 以为 watcher 只看 inotify：不是，启动时还会 `scan_existing_and_submit()` 补扫存量。
- 以为 finalize 只看“目录空闲时间”：不是，还会结合 manifest 阻塞状态、missing、processing lease、最近补扫结果。
- 以为根目录和子目录逻辑相同：不是，根目录 `.strm` 走单文件归档路径。

---

## 7.5 `/rm_strm` 空目录清理流程

入口：`cmd_rm_strm()` -> `strm_prune_service.run(apply_changes=False)`

流程：
1. 运行时读取 `.env`
2. 预扫描所有根目录
3. 自底向上判断哪些目录树**完全没有 `.strm`**
4. 默认只返回预览
5. 用户点击确认按钮后再 `apply_changes=True`
6. 删除后可选通知 Emby 局部刷新和库刷新

### 修改入口
- 改“什么目录可删”：`strm_prune.py`
- 改 Emby 通知：`strm_prune_emby.py`
- 改预览/实删摘要：`strm_prune_service.py`
- 改 Telegram 确认流：`handlers.py`

### 典型误区
- 这不是“删没有媒体文件的目录”，而是删**没有任何 `.strm` 子树**的空壳目录树。
- 当前设计非常强调“先预览、再确认”，不要轻易绕开确认流程。

---

## 8. STRM 状态机术语表

| 术语 | 含义 |
|---|---|
| `folder_key` | `WATCH_DIR` 下一级目录名，作为一个批次的唯一键 |
| manifest | `STRM_STATE_DIR` 下的 JSON，记录某个批次内每个 `.strm` 的状态 |
| `pending` | 等待处理 |
| `processing` | 正在处理，带 lease |
| `done` | 已重命名完成 |
| `already_ok` | 文件名本来就符合规则，无需重命名 |
| `failed` | 处理失败 |
| `missing` | 文件在完成前消失 |
| batch `active` | 批次仍未闭环 |
| batch `completed` | 批次可视为完成并已归档 |
| batch `failed:*` | 批次整体归档失败或出现批次级失败 |

> 如果要改 manifest 结构，必须同步检查：
> - `reconcile()`
> - `mark_processing()`
> - `mark_completed()`
> - `mark_failed()`
> - `finalize_decision()`
> - `folder_report()`
> - `list_manifests_summary()`

---

## 9. 任务路由表：遇到什么需求先看哪些文件

| 需求 | 先看这些文件 | 通常不用先看 |
|---|---|---|
| 改资源搜索交互 | `handlers.py`, `tmdb_api.py`, `formatter.py` | `strm_*`, `ass_*` |
| 改资源提取/解锁 | `handlers.py`, `hdhive_http_api.py`, `hdhive_unlock_service.py` | `checkin_*`, `danmu_service.py` |
| 改限速/排队提示 | `hdhive_unlock_service.py`, `handlers.py` | `formatter.py` 之外的大多数模块 |
| 改 115 自动推送到 Symedia | `handlers.py` 中 `handle_link_extracted()` / `auto_add_to_sa()` | `hdhive_http_api.py` |
| 改 Telegram 文案/按钮 | `formatter.py`, `handlers.py` | 底层 service |
| 加新 Bot 命令 | `handlers.py` + 对应 service 文件 | `main.py` 一般不用改 |
| 改签到 | `checkin_service.py`, `checkin_scheduler.py`, `handlers.py` | `hdhive_unlock_service.py` |
| 改弹幕下载 | `danmu_service.py`, `handlers.py` | STRM / ASS |
| 改 ASS 处理流程 | `ass_service.py`, `ass_pipeline.py`, `ass_font_pool.py`, `ass_utils.py` | `handlers.py` 只需看命令入口 |
| 改 STRM 文件命名 | `strm_naming.py`, `strm_watcher.py` | `strm_prune.py` |
| 改 STRM 批次完成条件 | `strm_watcher.py`, `strm_batch_state.py`, `strm_reason.py` | `formatter.py` |
| 改 STRM 通知文案/聚合方式 | `strm_notifier.py`, `strm_watcher.py` | `hdhive_*` |
| 改 `/rm_strm` 删除规则 | `strm_prune.py`, `strm_prune_service.py`, `handlers.py` | `strm_watcher.py` |
| 改 Emby 刷新 | `strm_prune_emby.py` | `strm_naming.py` |
| 加新环境变量 | `config.py` 或 `ass_config.py` 或 `strm_prune.py`，以及 `.env.example`, `README.md` | 无 |

---

## 10. 哪些模块适合放心改，哪些模块改动要谨慎

### 10.1 相对安全、边界清晰
- `formatter.py`
- `utils.py`
- `tmdb_api.py`
- `checkin_service.py`
- `danmu_service.py`
- `strm_naming.py`
- `ass_font_pool.py`

这些文件更偏“单一职责”，通常局部改动影响面可控。

### 10.2 改动要谨慎
- `handlers.py`
- `hdhive_unlock_service.py`
- `strm_watcher.py`
- `strm_batch_state.py`
- `strm_notifier.py`
- `strm_prune.py`

原因：
- 涉及并发 / 队列 / 状态机 / 持久化 / 长生命周期状态
- 改动容易引发“看似无关”的回归问题

---

## 11. 当前高耦合点 / 重构优先级建议

这些不是必须立刻改，但 AI 优化时应优先知道：

### 11.1 `handlers.py` 过大
它同时承担：
- 命令路由
- 回调路由
- 权限检查
- 资源提取编排
- 解锁确认逻辑
- Symedia 倒计时逻辑
- STRM 清理确认逻辑
- 直接链接解析

**建议的未来拆分方向**：
- `handlers_hdhive.py`
- `handlers_strm.py`
- `handlers_misc.py`
- `permission.py`
- `sa_push_service.py`

### 11.2 HDHive 提取流程分两段，语义不够直观
当前：
- `fetch_download_link()` 负责“前置判断”
- `unlock_resource()` 负责“真正拿链接”

对不了解项目的人会误以为“fetch_download_link 已经拿到最终链接”。

**如果以后做重构**，可以考虑统一命名成：
- `inspect_resource_access()`
- `extract_final_share_link()`

### 11.3 STRM watcher 是项目复杂度最高的地方
它同时耦合：
- inotify
- ffprobe
- 文件重命名
- 字幕联动
- manifest 状态机
- 目录 finalize
- Telegram 通知

**任何大改最好分步做**：
1. 先保留现有 `strm_reason.py` 状态码语义
2. 再改 `strm_batch_state.py`
3. 最后再动 `strm_watcher.py`

---

## 12. 新增功能时的放置建议

### 12.1 加新命令
优先模式：
1. 在 `handlers.py` 加路由入口
2. 新建或复用对应 service 模块承载业务逻辑
3. 把 Telegram 文案抽到 `formatter.py`（如果这类文案可复用）

### 12.2 加新外部 API
优先模式：
1. 单独建 `xxx_auth.py` / `xxx_client.py` / `xxx_service.py`
2. 不要把 HTTP 请求直接散落在 `handlers.py`

### 12.3 加新 STRM 行为
优先模式：
- 命名 -> `strm_naming.py`
- 探测 -> `strm_probe.py`
- 状态 -> `strm_batch_state.py`
- 通知 -> `strm_notifier.py`
- 编排 -> `strm_watcher.py`

### 12.4 加新运行时配置
判断它属于哪类：
- **启动静态配置** -> `config.py`
- **/ass 运行时配置** -> `ass_config.py`
- **/rm_strm 运行时配置** -> `strm_prune.py`

并同步更新：
- `.env.example`
- `README.md`

---

## 13. 修改时不要破坏的约束

### 13.1 回调按钮权限约束
- `ALLOWED_USER_ID != 0` 时，只允许该用户操作
- `ALLOWED_USER_ID == 0` 时，也会尽量限制为“消息发起人”操作
- 逻辑在：`check_callback_permission()`

> 改按钮交互时不要绕开这个逻辑，否则群聊里容易误点别人的按钮。

### 13.2 STRM 原因码和状态码要保持统一
- 所有人类可读错误提示最终应尽量通过 `strm_reason.py`
- 不要在 watcher 各处散落新的字符串状态而不登记到 `strm_reason.py`

### 13.3 `/rm_strm` 的默认安全模型是“预览优先”
除非用户明确要改产品行为，否则不要把默认模式改成直接删除。

### 13.4 `/ass` 必须串行执行
`ass_service.py` 里有互斥锁，避免多个 `/ass` 并发打架。

### 13.5 解锁/提取必须经过统一限速层
不要在别处直接绕开 `hdhive_unlock_service.py` 发起高频 unlock 请求。

---

## 14. AI 最小阅读路径

如果时间很紧，可以按下面的最小集合读文件：

### 改 HDHive 搜索 / 提取
1. `handlers.py`
2. `hdhive_http_api.py`
3. `hdhive_unlock_service.py`
4. `formatter.py`
5. `tmdb_api.py`

### 改 ASS
1. `ass_service.py`
2. `ass_pipeline.py`
3. `ass_font_pool.py`
4. `ass_utils.py`
5. `ass_config.py`

### 改 STRM 命名
1. `strm_naming.py`
2. `strm_watcher.py`
3. `strm_reason.py`

### 改 STRM 批次闭环 / finalize
1. `strm_watcher.py`
2. `strm_batch_state.py`
3. `strm_reason.py`
4. `strm_notifier.py`

### 改空目录清理
1. `strm_prune.py`
2. `strm_prune_service.py`
3. `strm_prune_emby.py`
4. `handlers.py`

### 改签到
1. `checkin_service.py`
2. `checkin_scheduler.py`
3. `handlers.py`

---

## 15. 对 AI 的最终建议

在这个项目里，**先判断改动属于哪条业务线**，再按模块边界下手：

- Telegram 交互问题 -> `handlers.py` / `formatter.py`
- HDHive 接口问题 -> `hdhive_http_api.py` / `hdhive_unlock_service.py`
- 搜索匹配问题 -> `tmdb_api.py`
- ASS 内封问题 -> `ass_pipeline.py` / `ass_font_pool.py`
- STRM 命名问题 -> `strm_naming.py`
- STRM 批次状态问题 -> `strm_batch_state.py`
- STRM 监听 / 归档问题 -> `strm_watcher.py`
- 空目录清理问题 -> `strm_prune.py`

不要一上来全量重读整个仓库；**先用本文件定位到业务线，再精读 2~4 个核心文件**，通常就能准确动手。
