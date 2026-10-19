# Spug Ops — Spug 运维平台助手 / Spug Ops Assistant

**Spug Ops** 是一个 ZCode / Claude Code 插件，让 AI Agent 通过命令行助手 `spug.py` 驱动开源运维平台 **Spug**（[openspug/spug](https://github.com/openspug/spug)），实现主机批量命令执行、交互式 SSH、文件上传下载、部署状态查询等运维操作。

**Spug Ops** is a ZCode / Claude Code plugin that lets an AI agent drive the open-source ops platform **Spug** ([openspug/spug](https://github.com/openspug/spug)) through a CLI helper `spug.py` — batch command execution, interactive SSH, file upload/download, and deploy status queries.

> Spug 是免安装 Agent 的运维平台：本插件的所有命令最终都经由 Spug 平台（SSH 底层）在目标主机上执行，Agent 不直接连接目标主机，也不接触主机密码。
> Spug is an agent-free ops platform: every command in this plugin is executed on the target host through Spug itself (SSH under the hood). The agent never connects to hosts directly and never touches host credentials.

---

## 特性 / Features

- ✅ **主机列表 Host inventory** — 列出 Spug 中已纳管的主机（`hosts`）
- ✅ **批量命令执行 Batch exec** — 在单台/多台主机上执行 Shell 命令并流式收集输出（`exec`，底层经 Spug SSH）
- ✅ **交互式 SSH Interactive SSH** — 通过 Spug 的 WebSocket 终端协议执行单条命令或进入 REPL（`ssh`）
- ✅ **文件管理 File management** — 列表 / 上传 / 下载 / 删除目标主机文件，走 Spug 服务端 SFTP（`file-*`）
- ✅ **命令模板 Command templates** — 列出 Spug 命令模板（`exec-templates`）
- ✅ **部署状态 Deploy status** — 查询部署申请与状态（`deploy` / `deploy-list`）
- ✅ **凭据自动续期 Auto credential renewal** — token 过期后自动用账号密码重新登录并重试请求
- ✅ **主机安全白名单 Host allowlist** — 未授权主机操作强制拦截，需用户确认（`--confirm`）
- ✅ **跨平台路径防护 MSYS path guard** — 检测 Git Bash 路径转换污染并给出修复提示

---

## 工作原理 / How It Works

`spug.py` 直接与 Spug 的 HTTP API 和 WebSocket 协议通信（协议已对照 Spug 源码核实）：

- **认证**：`POST /api/account/login/` 获取 access_token（32 位）；REST 请求用 `X-Token` 头，WS 用 `?x-token=` 参数
- **批量执行**：`POST /api/exec/do/` 创建任务 → WS `/api/ws/subscribe/<token>/` 订阅输出 → `PATCH /api/exec/do/` 下发 → worker 经 SSH 执行并发布结果
- **交互终端**：WS `/api/ws/ssh/<host_id>/`，客户端发送 `{"data": "..."}`，服务端回原始终端文本
- **文件管理**：`/api/file/` 与 `/api/file/object/`（列表 / 上传 multipart / 下载 / 删除），服务端 SFTP 代传

---

## 环境要求 / Requirements

- Python 3.8+
- `pip install websocket-client`（WebSocket 客户端库）
- 可访问的 Spug 实例（v3.x / v4.x 均可；API 路径与 WS 协议向下兼容）

---

## 安装 / Installation

### 方式一：插件市场（推荐）/ Via marketplace (recommended)

1. ZCode / Claude Code 插件市场 → 添加插件市场 → 粘贴本仓库目录（或 GitHub 仓库地址）
2. 添加名为 `spug-ops-market` 的市场 → 安装 **Spug Ops (spug-ops)**

### 方式二：手动 / Manual

```bash
git clone <this-repo> spug-ops
# 将 spug-ops 目录放入你的插件目录（或按插件系统要求安装）
pip install websocket-client
```

---

## 配置 / Configuration

所有配置保存在 `~/.spug-ops/config.json`（权限 0600），由 `config` / `login` 子命令维护。

All configuration is stored in `~/.spug-ops/config.json` (mode 0600), managed by the `config` / `login` subcommands.

```bash
# 配置 Spug 地址 / set Spug instance URL
python spug.py config --url http://<your-spug-host>:<port>

# 配置账号密码（推荐，支持 token 自动续期）/ set credentials (recommended, enables auto token renewal)
python spug.py config --username <username> --password <password>

# 配置主机白名单（安全拦截用）/ set host allowlist (used by the safety guard)
python spug.py config --allowed-hosts 1,2

# 或直接用浏览器登录后的 token / or use a token from an existing browser session
python spug.py config --token <32-char-access-token>

# 校验 / verify
python spug.py whoami
```

> 环境变量也可覆盖配置：`SPUG_URL`、`SPUG_TOKEN`、`SPUG_USERNAME`、`SPUG_PASSWORD`、`SPUG_ALLOWED_HOSTS`、`SPUG_API_KEY`。

---

## 命令参考 / Command Reference

| 场景 Scenario | 命令 Command |
|---|---|
| 查看配置 View config | `python spug.py config` |
| 查看主机 List hosts | `python spug.py hosts`（`--json` 结构化输出） |
| 查看命令模板 List templates | `python spug.py exec-templates` |
| 批量执行命令 Batch exec | `python spug.py exec --hosts 1,2 --command "uptime"` |
| 交互式 SSH（单命令） SSH one-shot | `python spug.py ssh --host 1 --command "df -h"` |
| 交互式 SSH（REPL） SSH REPL | `python spug.py ssh --host 1` |
| 终止执行 Terminate | `python spug.py terminate --token <exec-token> [--host <id>]` |
| 列出远程目录 List remote dir | `python spug.py file-list --host 1 --path /etc` |
| 上传文件 Upload | `python spug.py file-upload --host 1 --local C:/path/file --remote /tmp` |
| 下载文件 Download | `python spug.py file-download --host 1 --remote /tmp/file --local C:/save` |
| 删除远程文件 Delete | `python spug.py file-delete --host 1 --remote /tmp/file` |
| 部署状态 Deploy status | `python spug.py deploy --id <request-id>` |
| 部署列表 Deploy list | `python spug.py deploy-list` |
| 登录并保存 Login & save | `python spug.py login --username <u> --password <p>` |

> **Git Bash / MSYS 用户**：远程路径（`--path`/`--remote`）或以 `/` 开头的命令需加 `MSYS_NO_PATHCONV=1` 前缀；`--local` 用 Windows 风格路径。脚本会检测误转换并给出提示。
> **Git Bash / MSYS users**: prefix remote paths (`--path`/`--remote`) or commands starting with `/` with `MSYS_NO_PATHCONV=1`; use Windows-style paths for `--local`.

---

## 示例 / Examples

```bash
# 批量查看两台主机负载
python spug.py exec --hosts 1,2 --command "uptime && free -m"

# 交互式 SSH 查看磁盘
python spug.py ssh --host 1 --command "df -h && ss -tlnp"

# 上传配置文件到目标机 /opt/app
python spug.py file-upload --host 1 --local C:/tmp/app.ini --remote /opt/app

# 查看部署申请状态
python spug.py deploy --id 10
```

---

## 安全设计 / Safety

1. **指定机器约束**：所有执行操作必须显式指定主机 ID，禁止通配。
2. **主机白名单**：未授权主机默认被底层脚本拦截；Agent 必须先在对话中向用户确认，用户明确同意后才能加 `--confirm` 执行。
3. **只读优先**：默认只执行查看类命令；删除/覆盖/重启/触发发布等高危操作须先征得用户同意。
4. **凭据保密**：密码与 token 在输出中自动脱敏；配置不进仓库（见 `.gitignore`）。
5. **交互式密码提示**：`sudo` 密码等提示 Agent 无法代答，转述给用户处理。

---

## 故障排查 / Troubleshooting

| 现象 Symptom | 处理 Fix |
|---|---|
| `验证失败，请重新登录` / 401 | token 失效，重新 `config --token` 或 `login`（已配置账号密码则自动续期） |
| `权限拒绝` | 当前 Spug 账号缺该模块权限；批量执行被拒时改用 `ssh --command` 逐台执行 |
| `缺少 websocket-client 库` | `pip install websocket-client` |
| 远程路径报 `No such file` | Git Bash 路径转换污染，命令前加 `MSYS_NO_PATHCONV=1` |
| exec 全部"未完成/超时" | 目标主机 SSH 不通或 Spug worker 未运行，用 `ssh --command` 单机复测 |

---

## License

本项目为独立工具，仅通过 HTTP API / WebSocket 与 Spug 交互，不包含 Spug 代码。使用前请遵守 Spug 的 AGPL-3.0 许可与你们的使用条款。请按需为本项目添加 LICENSE。

This is an independent tool that interacts with Spug via its HTTP API / WebSocket only, and contains no Spug code. Respect Spug's AGPL-3.0 license and your own usage terms. Add a LICENSE to this repo as you see fit.
