# Task API v1

Python 标准库 HTTP 服务；默认 `http://127.0.0.1:8766`。首次安装任务和课表为空。数据结构见 [`task.schema.json`](task.schema.json)。

## 认证与请求约束

- 配置密码时，除登录接口外的 API 必须带有效会话 Cookie 或 `X-Planner-Access` 访问密码头。不要将密码放在 URL、日志或命令行中。
- `POST /api/v1/session` 接受 `{"username":"planner","accessPassword":"自行配置的密码"}`，返回 `{token}` 并设置 HttpOnly 会话 Cookie。
- 认证后 `GET /api/v1/session` 获取进程级 CSRF token。除登录外的全部 POST/PATCH/PUT/DELETE 都需 `X-Planner-Token`；重启后重新获取。
- `POST /api/v1/logout` 接受 `{}`，撤销当前会话、清除 Cookie；同样需要 CSRF token。
- 本地无密码模式不要求访问密码，**仍要求写请求 CSRF token**。它只适用于受信任的本地环境。
- 直连 Host 限实际端口的 `127.0.0.1` / `localhost`；写请求 Origin 必须精确匹配。脚本可不带 Origin，但不能省略认证及 CSRF。
- 代理模式需 loopback 监听、访问密码和显式 HTTPS `PLANNER_PUBLIC_ORIGIN`。Host 与转发协议必须匹配该 origin。不要允许客户端绕过代理直达后端。
- JSON 写请求需 `Content-Type: application/json` 与有效 Content-Length，最大 12 MiB。拒绝重复键、非有限数字、未知任务字段和 bool 冒充整数。
- 不提供 CORS。成功响应未注明时为 200。错误为 `{error:"安全错误码"}`，不返回堆栈或原始异常。
- 只有页面路由与 `index.html`、`app.js`、`utils.js`、`styles.css` 可静态访问；源码、数据库、文档、凭据和备份均不在白名单内。

## 路由

| 方法和路径 | 请求 / 用途 | 返回 |
| --- | --- | --- |
| GET /api/v1/health | 健康信息；配置密码时也需认证 | `{ok,version,taskCount,revision,timezone,storage,accessRequired}` |
| GET /api/v1/session | 获取写请求令牌 | `{token}` |
| POST /api/v1/session | 登录 | `{token}` + Cookie |
| POST /api/v1/logout | `{}` | `{ok:true}` |
| GET /api/v1/tasks | 任务快照 | `{version:1,revision,tasks}` |
| POST /api/v1/tasks | 完整任务对象 | 201 `{task,revision}` |
| PATCH /api/v1/tasks/{id} | `{changes,expectedUpdatedAt}` | `{task,revision}` |
| DELETE /api/v1/tasks/{id} | `{expectedUpdatedAt}` | `{deleted,revision}` |
| PUT /api/v1/tasks | `{version:1,tasks,expectedRevision}` | 新快照；替换前自动备份 |
| GET /api/v1/schedule | 课表 | `{events,courses,resources,timezone,source,sha256}` |
| POST /api/v1/schedule | `{"ics":"BEGIN:VCALENDAR..."}` | `{ok,count,unchanged}` |
| POST /api/v1/deadlines/resolve | `{category,phrase,sourceDate?}` | `{due,basis,inferred,eventId?}` |
| GET /api/v1/imports | 文件指纹及处理状态 | `{files:[...]}` |
| POST /api/v1/imports | 增量批次，见下节 | `{added,skipped,fileCount,revision}` |
| GET /api/v1/events?limit=100 | 最近 1–200 条审计记录 | `{events:[...]}` |
| GET /api/v1/qq/status | 安全状态及机器人槽位 | `{configured,bound,gatewayReady,enabled,deliveryStatus,bots,...}` |
| POST /api/v1/qq/pairing | `{"bot":"bot-1"}` 或 `bot-2` | 短期一次性配对码或安全错误状态 |
| POST /api/v1/qq/test | `{}` 测全部，或指定 `bot` | 各槽位发送受理结果 |

`/__planner_health` 是不需访问密码的最小进程识别接口，只返回应用名和版本，不含任务或用户信息。

## 完整任务示例（虚构）

```json
{"id":"manual-example","title":"阅读示例资料","category":"示例课程","priority":3,"priorityDefault":true,"due":"","remind":4320,"duration":30,"completed":false,"notes":"","source":"","sourceNotes":"","sourceTiming":"","kind":"manual","firedKey":"","createdAt":"2026-01-01T00:00:00Z","updatedAt":"2026-01-01T00:00:00Z"}
```

- id：1–100 个 ASCII 字母/数字/下划线/连字符；title≤200、category≤100，不能空白。
- notes≤10000、source≤1000、sourceNotes≤3000、sourceTiming≤2000。
- priority：整数 1–4；duration：整数 1–1440 分钟；completed、priorityDefault：布尔值。
- due：空字符串或真实 `YYYY-MM-DDTHH:MM`，按北京时间解释，年份 1900–9998。
- remind：-1（关闭）、0（当天）、1440/2880/4320/7200/10080/20160/43200 分钟；兼容旧 5/15/30/60 并迁移为 1440。
- kind：assignment/milestone/suggestion/manual；firedKey 为空或 `due|remind`。
- createdAt/updatedAt：带时区 ISO 日期时间；POST 使用服务器时间覆盖，PATCH 严格递增 updatedAt。
- PATCH 不能改 id/createdAt/updatedAt/source/sourceNotes/sourceTiming/kind；日期或提醒变化会清空 firedKey。
- 并发版本不匹配返回 409；请重新 GET 后由用户决定，不要静默重试覆盖。

## 增量批次

```json
{"batchId":"example-import","files":[{"path":"示例课程/通知.txt","sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","status":"no_tasks","tasks":[],"error":""}]}
```

路径必须是相对路径，不含空段、`.`、`..`。sha256 是 64 位十六进制。状态为 processed/no_tasks/needs_review/failed；只有 processed 可携带非空任务。错误字段只能是安全错误码，不能含异常或原文。

- 相同路径/哈希且上次已 processed/no_tasks，跳过整个文件；失败或待核对允许重试。
- 文件修改后只新增；相同 ID、相同来源/分类/标题/类型的语义重复会跳过，不覆盖用户手动修改。
- 全批验证后同一事务提交；任一失败全部回滚。任务集合最多 5000 条。
- `PUT /tasks` 是备份整体恢复，不是增量导入；替换前保存 SQLite 一致性备份。

## 课表和数据保留

ICS 最大 2 MB、最多 3000 节；需预展开重复事件，只接受 UTC 或 Asia/Shanghai。课程别名、资源、排除列表在开源版默认为空。无法匹配的日期保持空白，推算依据附在备注中。

任务与导入清单在 `data-dir/tasks.sqlite3`；课表在 `data-dir/course-schedule.json`；备份在 `data-dir/backups/`；审计日志在 `data-dir` 的父目录 `logs/`。审计不记录完整正文或凭据，但可能记录任务 ID、操作时间及计数，同样应保护。

QQ 状态接口不会返回 AppID、密钥或收件人标识；配对码只返回给已授权请求方，不写审计日志。QQ 未配置/不可用不阻止任务 API 使用。
