---
name: spug-ops
description: >
  Spug 运维平台助手（Spug ops assistant）。当用户提到 spug、运维、部署、发布、
  主机批量执行命令、SSH 终端、主机列表、部署状态、命令模板、cron 任务等场景，
  且目标是操作 openspug/spug 运维平台（实例地址由配置文件 ~/.spug-ops/config.json 指定）时
  使用本技能。通过 Spug 的 HTTP API 与 WebSocket 协议驱动：列出主机、批量执行命令、
  交互式 SSH 会话、命令模板、部署申请状态查询等。
---

# Spug 运维平台助手

本技能让 Agent 通过命令行助手 `spug.py` 操作 Spug 运维平台（免装 Agent，底层走 SSH）。
**所有命令最终都经由 Spug 平台在目标主机上执行，不是在本机直接 SSH。**

## 依赖与脚本位置

- 需要 Python 3.8+ 与 `websocket-client`（`pip install websocket-client`）
- 助手脚本路径：`${CLAUDE_PLUGIN_ROOT}/scripts/spug.py`
- 配置保存在 `~/.spug-ops/config.json`（含 Spug 地址与访问令牌），由 `config`/`login` 子命令维护
- **Git Bash / MSYS 路径转换提醒**：凡涉及远程路径（`--path`/`--remote`）或以 `/` 开头的命令（`--command`），必须加 `MSYS_NO_PATHCONV=1` 前缀，否则 Git Bash 会把 `/tmp` 之类的参数转成 Windows 路径导致报错。本地路径 `--local` 请传 Windows 风格路径（如 `C:/Users/.../file.txt`）。脚本对误转换有检测并会给出修复提示。

## 认证与 Token 自动续期（重要）

Spug 的访问令牌（access_token）默认有效期为 8 小时。为了避免会话过期导致操作中断，现已支持**凭据自动续期**：

1. **配置用户名和密码（推荐，一次配置永久自动续期）**：
   ```bash
   python spug.py config --username <用户名> --password <密码>
   ```
   配置保存后，当 access_token 8 小时过期失效时，脚本在检测到 401 认证失败时会**自动重新发起登录、刷新 token 并自动重试请求**，无需每次手动重新获取 token。
2. **直接登录**：
   ```bash
   python spug.py login --username <用户名> --password <密码>
   ```
   登录成功后会自动将凭据与 token 一并写入 `~/.spug-ops/config.json`。
3. **纯 Token 模式（备用）**：
   若账户启用了 MFA 或双重验证无法脚本登录，可在浏览器登录后读取 `localStorage.getItem('token')`，通过 `python spug.py config --token <32位token>` 写入。

**安全要求：**
- 配置文件权限为 0600，且在终端打印展示配置时，密码与 token 会自动打码脱敏。
- 不得在最终输出给用户的文本中明文打印完整 token 或密码。

## 目标机器使用限制与跨主机安全拦截（核心安全规范）

为了防止误操作未授权服务器，本技能实施了**严格的主机白名单与确认机制**：

1. **必须显式指定目标机器**：
   所有执行命令（`ssh`、`exec`、`file-*` 等）**必须明确传入 `--host <id>` 或 `--hosts <id>`**，严禁在未确认目标主机的情况下执行。
2. **默认授权机器与白名单**：
   默认白名单为空。可通过 `python spug.py config --allowed-hosts <id>` 或环境变量 `SPUG_ALLOWED_HOSTS=<id1,id2>` 显式配置授权主机。
3. **涉及其他机器时必须先询问用户（硬性规则）**：
   - 当任务或命令涉及到白名单之外的**其他机器**时，底层脚本会自动进行安全拦截并阻止执行。
   - **Agent 严禁擅自在后台执行，严禁在未经用户同意前自动附加 `--confirm` 参数！**
   - **必须先在对话中向用户提问确认**：
     > “当前操作将涉及主机 **<ID>**（<主机名称>/<IP>），不在默认授权列表中。请确认是否允许在此机器上执行该操作？”
   - **只有当用户在对话中明确回复同意/确认后**，Agent 方可在命令中附带 `--confirm` 参数继续执行。

## 常用命令

| 场景 | 命令 |
|---|---|
| 查看配置 | `python spug.py config` |
| 配置凭据与白名单 | `python spug.py config --username <u> --password <p> --allowed-hosts <id>` |
| 登录并保存凭据 | `python spug.py login --username <u> --password <p>` |
| 查看主机列表 | `python spug.py hosts`（`--json` 输出结构化） |
| 查看命令模板 | `python spug.py exec-templates` |
| 批量执行命令 | `python spug.py exec --hosts <id> --command "uptime"` |
| 交互式 SSH（单命令） | `MSYS_NO_PATHCONV=1 python spug.py ssh --host <id> --command "df -h"` |
| 跨未授权主机操作（需用户确认） | `MSYS_NO_PATHCONV=1 python spug.py ssh --host <other-id> --command "uptime" --confirm` |
| 终止执行 | `python spug.py terminate --token <exec的token> [--host <id>]` |
| 列出主机目录/文件 | `MSYS_NO_PATHCONV=1 python spug.py file-list --host <id> --path /etc` |
| 上传文件到主机 | `MSYS_NO_PATHCONV=1 python spug.py file-upload --host <id> --local C:/path/file --remote /tmp` |
| 从主机下载文件 | `MSYS_NO_PATHCONV=1 python spug.py file-download --host <id> --remote /tmp/file --local C:/save` |
| 删除主机文件 | `MSYS_NO_PATHCONV=1 python spug.py file-delete --host <id> --remote /tmp/file` |
| 部署状态 | `python spug.py deploy --id <申请ID>` |
| 部署列表 | `python spug.py deploy-list` |

命令失败时退出码非 0，并输出 `错误: ...` 到 stderr。

**权限说明（重要）**：不同模块需要对应权限，账号权限不足时会报 `权限拒绝`：
- 交互式 SSH（`ssh`）只需**主机访问权限**（`has_host_perm`），一般普通运维账号都有
- 批量执行（`exec`/`exec-templates`）需要 `exec.task.do` 等权限
- 部署（`deploy`/`deploy-list`）需要 `deploy.request.view` 等权限
- 若批量执行被拒，**改用 `ssh --host <id> --command "..."` 逐台执行**，这是通用兜底路径

## 典型工作流

### 1. 查看主机与状态
```
python spug.py hosts
```
先确认主机存在且当前用户有权访问；无权时会返回"无权访问主机"。

### 2. 批量执行命令（最常用）
```
python spug.py exec --hosts <id1,id2,...> --command "<shell命令>"
```
- 输出会按主机分节展示，结尾标注每台成功/失败
- 命令里的引号要小心转义；复杂命令建议先在本机 `echo` 验证拼写
- 长命令或慢命令可加 `--timeout <秒>`（默认 600）
- 需要持续观察的输出可用 `--raw` 保留原始格式
- 终止进行中的任务：把 exec 返回的任务 token 记下来，用 `terminate` 终止
- **若返回"权限拒绝"**：当前账号没有批量执行权限，改用 `python spug.py ssh --host <id> --command "..."` 逐台执行

### 3. 交互式 SSH 单条命令
```
python spug.py ssh --host <id> --command "df -h && free -m"
```
- 适合需要看终端语义（提示符、颜色）的只读命令
- `--quiet <秒>` 控制"命令结束"判定（默认 2 秒无新输出即认为结束）
- 连接失败/凭据错误时返回红色错误文本，原样转述给用户

### 4. 文件上传 / 下载（SSH 页面文件管理能力）
```
MSYS_NO_PATHCONV=1 python spug.py file-list   --host <id> --path <远程目录>     # 先看目录
MSYS_NO_PATHCONV=1 python spug.py file-upload --host <id> --local <本地文件> --remote <远程目录>
MSYS_NO_PATHCONV=1 python spug.py file-download --host <id> --remote <远程文件> --local <本地目录>
MSYS_NO_PATHCONV=1 python spug.py file-delete --host <id> --remote <远程文件>
```
- 上传走 Spug 服务端 SFTP（复用主机在 Spug 中配置的凭据），Agent 无需持有主机密码
- `--remote` 是目标**目录**，文件以本地文件名落到该目录；`--local` 用 Windows 路径
- 需要 `host.console.upload` / `host.console.del` 权限（普通运维账号通常已开）
- 上传/下载/删除是不可逆或有影响的操作，先与用户确认目标路径

### 5. 部署状态查询
```
python spug.py deploy --id <申请ID>
python spug.py deploy-list
```
状态含义：`0 待审核 / 1 待发布 / 2 发布中 / 3 发布成功 / -1 已驳回 / -3 发布异常`。

## 安全与边界（必须遵守）

1. **指定机器约束**：所有运维执行操作必须明确指定机器 ID（如 `--host <id>`），不得使用通配或省略目标机器。
2. **跨主机操作强制先询问用户**：白名单通过 config 配置。若任务涉及到白名单之外的任何其他主机，Agent **绝对不可擅自执行**，也**绝对不可自行附加 `--confirm` 参数**。必须先在会话中明确向用户说明涉及的主机与计划操作，待用户明确回复允许后，方可添加 `--confirm` 执行。
3. **只读优先**：除非用户明确要求修改系统，默认只执行查看类操作（`hosts`、状态查询、`uptime`/`df`/`free` 等只读命令）。
4. **高危操作须先确认**：涉及删除、覆盖、重启服务、改配置、触发构建/发布/回滚、格式化等不可逆或影响面大的操作，**必须先向用户说明并征得同意**再执行。
5. **触发部署申请需用户明确指示**：本技能只提供状态查询；创建/审核/触发部署要等用户明确提出。
6. **无法处理交互式密码提示**：`sudo` 密码、密钥口令等提示出现时，Agent 没有凭据也无法代答，应把提示原样转述给用户，由用户在浏览器终端完成或提供处理方案。
7. **不越权**：操作只在用户账号权限范围内；报"无权访问主机/权限拒绝"时如实告知，不要尝试绕过。
8. **凭据保密**：不在输出中泄露完整 token 或密码；配置文件含敏感信息，不要把它作为内容粘贴给用户或写入仓库。
9. **执行环境注意**：命令在目标主机以该主机在 Spug 中配置的用户身份执行，不是 root；需要 root 的操作请提示用户。

## 故障排查

- `验证失败，请重新登录` / HTTP 401：token 失效或未写入，重新读取浏览器 token 或重新 `login`
- `权限拒绝`：当前账号缺少该模块权限。批量执行→改用 `ssh --command` 兜底；部署/模板→提示用户用有权限的账号，或在 Spug 后台为该账号（角色）开启对应权限
- `缺少 websocket-client 库`：先 `pip install websocket-client`
- `路径疑似被 Git Bash 路径转换污染` / 远程路径报 `No such file`：命令前加 `MSYS_NO_PATHCONV=1`，见"依赖与脚本位置"
- `本地文件不存在`：`--local` 要用 Windows 风格路径，不是 Git Bash 的 `/tmp/...` 写法
- exec 全部"未完成/超时"：目标主机 SSH 不通或 worker 未运行，用 `ssh --command "uptime"` 单机复测
- 部署状态一直是"待审核"：说明申请卡在审核环节，提示用户去 Spug 界面审核
- 交互 SSH 无输出：可能命令本身无输出，或终端在等待输入（如交互式程序），如实反馈
