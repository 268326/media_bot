# Media Bot 上线前回归 Checklist

> 目标：在正式生产前，用最短路径验证“能启动、权限正确、核心命令可用、可观测性正常”。

## 0. 启动前准备

- [ ] 已复制 `.env.example` 为 `.env`
- [ ] 已填 `BOT_TOKEN`
- [ ] 已填 `HDHIVE_API_KEY`
- [ ] 如使用 OpenAPI 应用而非个人 API Key，已按官方文档配置 `HDHIVE_ACCESS_TOKEN`（Bearer 用户令牌）
- [ ] 已填 `bot_user_id`
- [ ] 已填 `bot_chat_id`
- [ ] 如需关键词搜索，已填 `TMDB_API_KEY`
- [ ] 如需 Symedia 推送，已填 `SA_URL` / `SA_PARENT_ID`
- [ ] 如需 STRM，已填并检查 `STRM_WATCH_DIR` / `STRM_DONE_DIR` / `STRM_FAILED_DIR`
- [ ] 如需 `/ass`，已检查目标目录挂载路径

## 1. 本地 / 容器启动检查

### 本地

```bash
python3 -m py_compile *.py
python3 main.py --healthcheck
```

期望：
- healthcheck 返回 0
- 仅出现配置警告，不出现 traceback

### Docker

```bash
docker compose up -d --build
docker ps
docker logs -f media_bot
```

期望：
- 容器状态为 `Up`
- 若 healthcheck 生效，最终为 `healthy`
- 日志中无持续 traceback / 重启循环

## 2. Telegram 权限验证

### 2.1 允许用户
- [ ] 使用 `bot_user_id` 中的账号发送 `/start`
- [ ] 能正常收到欢迎消息

### 2.2 非允许用户（如有条件测试）
- [ ] 使用未授权账号发送命令
- [ ] 应被拒绝，不能正常操作敏感功能

## 3. 基础命令回归

### `/start`
- [ ] 正常返回欢迎消息

### `/help`
- [ ] 正常返回帮助文案

### `/points`
- [ ] 能正常查询积分
- [ ] 若 API Key 权限不足，应返回可理解错误，不应崩溃

### `/checkin`
- [ ] 正常签到或返回“今日已签到”
- [ ] 不应出现 traceback

## 4. 搜索与解锁链路回归

### `/hdt 关键词`
- [ ] 若配置 `TMDB_API_KEY`，应返回候选列表
- [ ] 分页按钮正常

### `/hdm 关键词`
- [ ] 行为同上

### 直发 HDHive 链接
- [ ] 直接发送资源链接能触发解析
- [ ] 免费 / 已解锁资源能返回结果
- [ ] 需解锁资源能进入解锁确认

### 解锁链路
- [ ] 手动解锁流程可完成
- [ ] 若开启自动解锁，低于阈值资源可自动走通
- [ ] 多次快速请求时，排队提示正常显示

## 5. Symedia / 115 推送（按需）

### 自动推送
- [ ] 115 链接提取后能显示倒计时
- [ ] `/hdc` 能取消最近一次自动添加任务
- [ ] 倒计时结束后可成功推送到 Symedia

### 手动推送按钮
- [ ] “发送到 Symedia”按钮可正常工作
- [ ] 失败时有明确错误提示

## 6. `/ass` 功能回归（按需）

### 菜单入口
- [ ] 发送 `/ass` 能出现菜单
- [ ] 能进入“子集化字体”
- [ ] 能进入“内封字幕”

### 子集化字体
- [ ] 能扫描目标目录
- [ ] 运行中消息正常
- [ ] 完成后能返回汇总
- [ ] 失败时错误消息可读

### 字幕内封
- [ ] 能创建会话
- [ ] 能重新扫描生成计划
- [ ] 能翻页预览
- [ ] 能修改默认字幕组 / 默认语言
- [ ] 能修改单条字幕文件 / 语言 / 字幕组
- [ ] DRY-RUN 正常
- [ ] 执行确认页正常显示

## 7. STRM 功能回归（按需）

### `/strm_status`
- [ ] 能显示当前 watcher 状态

### `/strm_scan`
- [ ] 可手动触发扫描

### `/strm_restart`
- [ ] 可手动重启 watcher
- [ ] 重启后状态恢复正常

### watcher 实际处理
- [ ] 放入测试 `.strm` 后能被处理
- [ ] 成功文件能进入 done
- [ ] 失败文件能进入 failed
- [ ] 通知文案正常

## 8. `/rm_strm` 回归（按需）

- [ ] 发送 `/rm_strm` 默认先预览
- [ ] 点击确认后才实际删除
- [ ] 一级分类目录保护符合预期
- [ ] 若启用 Emby 通知，删除后刷新正常

## 9. 可观测性检查

- [ ] `docker logs -f media_bot` 可持续查看日志
- [ ] 关键失败路径有错误日志
- [ ] 不存在明显静默失败
- [ ] `python main.py --healthcheck` 通过

## 10. 上线后首日观察项

- [ ] 是否出现频繁 traceback
- [ ] 是否出现 Telegram 回调超时
- [ ] 解锁队列是否长期堆积
- [ ] STRM 是否有卡在 processing 的批次
- [ ] `/ass` 是否出现异常长耗时或空间不足

## 11. 建议的最终上线判定

满足以下条件再正式生产：

- [ ] healthcheck 正常
- [ ] 基础命令 `/start /help /points /checkin` 正常
- [ ] 至少完成一次资源解析 / 解锁全链路
- [ ] 若启用 `/ass`，至少完成一次 DRY-RUN 或小样本真执行
- [ ] 若启用 STRM，至少完成一次测试样本流转
- [ ] 日志中无新的启动级 traceback
