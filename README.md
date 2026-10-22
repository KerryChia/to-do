# To-do · 待办日历

一个可自托管的中文任务管理与课程日历：原生 JavaScript 前端、Python 标准库 API、SQLite 持久化，以及可选的 QQ 递进提醒。

**这是经过脱敏整理的开源版本，不包含作者的任务、课表、课程资料、账户、凭据或生产部署配置。首次运行是空白工作区。**

## 功能

- 任务新增、编辑、完成/重开、删除；分类、搜索、优先级和多种排序。
- 清单与周历；JSON 备份导入/导出、ICS 日历导出。
- 导入已展开的 ICS 课表；对“下次课 / 下周 / 第 N 周”等时间线索提供有依据的推算，无法确定时留空。
- 有来源证据的增量导入：文件指纹、语义去重、事务提交和人工修改保护。
- 可选一到两台 QQ 机器人：一次性配对、分阶段提醒、持久化去重、限流及有限重试。
- 访问密码、HttpOnly 会话、CSRF、Host/Origin 校验、乐观并发和严格静态文件白名单。

适用于个人或同一信任边界内使用。**这是单用户/共享密码应用，不是具备账户隔离的多租户系统。** 日期与提醒统一按北京时间（UTC+8）处理。

## 快速开始

核心应用只需 **Python 3.11+**，无 pip/npm 依赖。QQ 网关和 JavaScript 测试需 **Node.js 24+**。

```sh
git clone https://github.com/KerryChia/to-do.git
cd to-do
python server.py --no-reminders
```

打开 <http://127.0.0.1:8766/>。Windows 可将 `python` 替换为 `py -3`。停止服务使用 Ctrl+C。

- 默认只监听 `127.0.0.1`，不自动打开网页，不导入任何种子任务。
- 默认数据目录 `data/`，审计目录 `logs/`，均被 Git 忽略；建议将实际数据放到仓库外。
- 本机无密码模式仅用于受信任的本地环境。多用户电脑或远程部署都应设置密码。
- 不要直接双击 HTML，也不要用普通静态服务器替代后端；那会失去 API 与静态文件访问限制。
- 不提供自动连接作者服务器、遥测或自动上传课程资料的功能。可选 QQ 通知及明确执行的远程 API 导入会产生网络请求。

### 设置访问密码与独立数据目录

在仓库外创建仅当前用户可读的文本文件，写入一行自行生成的高强度密码。不要将密码写进命令行、代码或仓库；Linux/macOS 将文件权限设为 `600`。

```sh
python server.py --no-reminders --data-dir /path/to/private/planner/data --access-password-file /path/to/private/access-password.txt
```

路径均为占位符，需要换成自己的路径。Windows 使用自己的用户目录与文件权限设置。也支持由受控进程环境注入 `PLANNER_ACCESS_PASSWORD`；不要把真实值提交到环境示例中。

登录用户名为 `planner`，密码为自己配置的访问密码。会话最长 30 天；修改密码会令旧会话失效。退出登录会撤销当前会话。页面不会把密码写进 localStorage。

## 课表与导入

课表导入 API 接受已展开的 `VEVENT`，不接受 `RRULE/RDATE/EXDATE`；时区支持 `Asia/Shanghai` 与 UTC。`fixtures/sample-schedule.ics` 是虚构测试数据，不代表任何人的真实课表。

开源版的课程别名、课程资源与课程排除规则默认为空；课程分类由自己导入的课表和任务生成。课表类别与任务类别需一致，才能推算截止时间。推算不等于确认，请检查依据。

导入器只提取文字和校验批次，**不会自动理解作业要求，也不捆绑任何 AI 服务或密钥**。需自行审阅，或在自己授权的 AI 环境中提取任务。原资料中的指令应作为不可信数据处理。

```sh
python ingest.py scan --root /path/to/course-materials --out /path/to/private/review-run
python ingest.py submit --batch /path/to/private/batch.json --manifest /path/to/private/review-run/manifest.json --dry-run
```

- `--root` 必须显式提供，不会默认扫描用户主目录或上级文件夹。
- API 默认是本机 `http://127.0.0.1:8766`。远程必须显式提供 `--api https://planner.example.com --allow-remote-api`，需要认证时加 `--access-password-file`。
- 支持 TXT/Markdown/CSV、DOCX/PPTX；PDF 可选安装 `pypdf` 或 PyMuPDF。图片/OCR、旧 Office 文件及不能可靠读取的资料需人工处理，不会伪装成“无任务”。
- 扫描产物含提取正文、来源路径与本机绝对扫描根目录，必须视作私人资料。提交会将任务和来源元数据发给选定 API，请确认目标归自己控制。
- 导入模板见 [`templates/import-batch.example.json`](templates/import-batch.example.json)；模板中的哈希需替换为真实扫描哈希。
- 完整接口见 [`API.md`](API.md)。

## 可选 QQ 提醒

不使用 QQ 时始终带 `--no-reminders`；不需要申请机器人。QQ 配置步骤见 [`docs/QQ.md`](docs/QQ.md)。

提醒可选关闭、当天或提前 1/2/3/5/7/14/30 天；未完成时逐步加强，最后可到逾期 7 天。离线补发会合并过时阶段。是否真正送达受平台窗口、权益与配额影响，接口受理不等于已阅读。

开源副本使用独立的 `ToDoOpenSource` 凭据命名空间与 `to-do-open-source/qq-private` 状态目录，不会默认读取原私人版本的机器人配置。环境变量仍优先使用；测试或开发时务必隔离环境，并禁用真实提醒。

## 自托管与隐私

参阅 [`docs/DEPLOY.md`](docs/DEPLOY.md) 与 [`SECURITY.md`](SECURITY.md)。远程部署应通过自己的 HTTPS 反向代理，后端仍只监听 loopback。代理模式要求密码与明确的 HTTPS `PLANNER_PUBLIC_ORIGIN`；不提供硬编码的生产地址。

运行数据**没有应用层磁盘加密**。数据库、备份、日志、导入批次、课表与 QQ 接收人状态都可能包含个人信息；请使用仓库外的私有目录、合适的系统权限、磁盘加密和备份策略。`.gitignore` 只是辅助，不是防泄露保证。

## 测试

以下套件使用临时目录、虚构数据和模拟发送器，不依赖私人课程文件或真实 QQ 账户：

```sh
node tests.cjs
python tests_api.py
python ingest-tests.py
python tests_schedule.py
python tests_security.py
python tests_qq.py
python scripts/check_release.py
```

CI 在 Ubuntu 与 Windows 上执行同一组核心检查。真实 QQ 送达需自行绑定后确认，不在离线测试覆盖范围内。

## 项目结构

- `server.py` / `backend.py`：HTTP API、认证、SQLite 事务与导入。
- `index.html` / `app.js` / `styles.css` / `utils.js`：无框架前端。
- `course_schedule.py`：ICS 课表与截止时间推算。
- `ingest.py`：本地文字提取与增量导入。
- `qq_credentials.py` / `qq_reminders.py` / `qq_gateway.mjs`：可选 QQ 凭据、调度与网关。
- `tests*` / `fixtures/`：离线回归与虚构夹具。

## 许可证

[MIT](LICENSE)。不包含第三方课程资料、真实任务数据或任何私有运维文件。
