# 通用 HTTPS 自托管示例

所有域名、路径和账户都是示例。请替换为自己控制的配置；不依赖任何作者服务。下面按 Linux、systemd 与 Caddy 举例。

## 1. 独立服务账户和私有状态

使用专用非 root 用户 `planner`，将代码放在 `/srv/to-do/`，运行数据放 `/var/lib/to-do/data/`。目录应仅服务用户可写，日志会在 `/var/lib/to-do/logs/` 生成。安装 Python 3.11+；只有 QQ 网关需要 Node.js 24+。

在仓库外准备仅服务用户可读的一行强口令文件，例如 `/etc/to-do/access-password.txt`（权限 `600`）。不要使用公开示例密码，不要将口令提交或作为进程参数。

## 2. 服务单元

`deploy/to-do.service.example` 仅是模板：先替换路径、用户与 `PLANNER_PUBLIC_ORIGIN`，再安装为系统服务。默认禁用 QQ；需要时按 [QQ.md](QQ.md) 添加独立环境文件并移除 `--no-reminders`。

服务使用 `UMask=0077`，只监听 `127.0.0.1:8766`。首次启动创建空数据库，不会导入作者的数据。

## 3. HTTPS 代理

将自己域名的 DNS 指向自己的服务器，安装并配置 Caddy。例如：

```caddyfile
planner.example.com {
    reverse_proxy 127.0.0.1:8766
}
```

将服务环境 `PLANNER_PUBLIC_ORIGIN` 改成相同 HTTPS origin（不带尾部 `/`），再启用 `--trust-proxy`。Caddy 默认保留 Host，并设置转发协议头；若使用其他代理，必须确保这两项行为正确，不允许客户端注入转发头。

公开端口仅应包含自己的 HTTPS 代理需要的端口，**不对外开放 8766**。代理和应用必须位于相同受信任主机/网络边界。应用的静态白名单负责阻止数据库下载，不能改成对整个代码目录直接 `file_server`。

## 4. 验证

- 未登录访问 `/api/v1/tasks` 应为 401；正确登录后可以读取空任务列表。
- 数据库、日志、源码、配置和备份路径应为 404。
- 写操作缺少 CSRF、Origin 不匹配或 Host 不匹配应被拒绝。
- HTTPS 登录 Cookie 应包含 Secure / HttpOnly；HTTP 不能承载真实凭据。
- 重启后数据保留；不要复用开发实例的目录或 QQ 接收人状态。

## 5. 备份和升级

网页 JSON 导出包含任务及来源文本，请加密保存。整体 JSON 替换前会自动保存 SQLite 备份。课表和 QQ 私有状态需按实际目录另行备份。

升级前先做一致性备份，在独立目录与虚构数据上通过测试后再切换服务。示例不提供覆盖线上目录、SSH 上传、自动 DNS 修改或一键删除历史数据的脚本。
