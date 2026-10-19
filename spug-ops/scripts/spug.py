#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
spug.py — Spug 运维平台命令行助手
=================================

通过 Spug 的 HTTP REST API + WebSocket 协议，在 Agent 会话中驱动运维操作。
协议细节已对照 openspug/spug v3.3.3 源码核实：

  * 认证：登录接口返回 access_token（32 位），REST 请求头用 X-Token，WS 用 ?x-token=
  * 批量执行：POST /api/exec/do/ 创建任务 -> WS /api/ws/subscribe/<token>/ 订阅
    -> PATCH /api/exec/do/ 下发任务 -> worker 通过 SSH 执行并把输出发布到该 channel
  * 交互终端：WS /api/ws/ssh/<host_id>/ ，客户端发送 {"data": "..."}，服务端回原始终端文本

依赖：Python 3.8+，pip install websocket-client
"""

import argparse
import json
import os
import re
import socket
import sys
import time
import uuid
import urllib.request
import urllib.error

# ---------- 智能网卡绑定（防御 TUN/代理虚拟网卡导致的连接劫持） ----------
def get_physical_bind_ip():
    bind_ip = os.environ.get("SPUG_BIND_IP")
    if bind_ip:
        return bind_ip
    try:
        host = socket.gethostname()
        ips = [info[4][0] for info in socket.getaddrinfo(host, None, socket.AF_INET)]
        # 优先 192.168.x.x 或 10.x.x.x，排除 TUN (198.18.*)、Hyper-V/WSL (172.16-31.*)、回环
        priv = [ip for ip in ips if ip.startswith("192.168.") or ip.startswith("10.")]
        if priv:
            return priv[0]
        fallback = [ip for ip in ips if not ip.startswith("198.18.") and not ip.startswith("127.") and not ip.startswith("172.")]
        if fallback:
            return fallback[0]
    except Exception:
        pass
    return None

def _auto_bind_physical_ip():
    bind_ip = get_physical_bind_ip()
    if bind_ip:
        orig_create = socket.create_connection
        def _bound_create(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None):
            return orig_create(address, timeout=timeout, source_address=(bind_ip, 0))
        socket.create_connection = _bound_create

_auto_bind_physical_ip()

try:
    import websocket
except ImportError:
    websocket = None

DEFAULT_CONFIG = os.path.join(os.path.expanduser("~"), ".spug-ops", "config.json")
VERSION = "0.3.0"

DEPLOY_STATUS = {
    "-3": "发布异常",
    "-1": "已驳回",
    "0": "待审核",
    "1": "待发布",
    "2": "发布中",
    "3": "发布成功",
}

# ---------- ANSI / 终端文本处理 ----------

ANSI_OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
ANSI_CSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
ANSI_OTHER = re.compile(r"\x1b[()][0-9A-Za-z]")


def strip_ansi(text):
    text = ANSI_OSC.sub("", text)
    text = ANSI_CSI.sub("", text)
    text = ANSI_OTHER.sub("", text)
    return text.replace("\r\n", "\n").replace("\r", "\n")


# ---------- 配置与安全策略 ----------


class SpugError(Exception):
    pass


def parse_host_list(val):
    if not val:
        return []
    if isinstance(val, list):
        return [int(x) for x in val if str(x).strip().isdigit()]
    res = []
    for part in re.split(r"[,\s]+", str(val).strip()):
        if part.isdigit():
            res.append(int(part))
    return sorted(set(res))


def load_config(args):
    path = getattr(args, "config", None) or os.environ.get("SPUG_CONFIG") or DEFAULT_CONFIG
    cfg = {
        "url": os.environ.get("SPUG_URL"),
        "token": os.environ.get("SPUG_TOKEN"),
        "username": os.environ.get("SPUG_USERNAME"),
        "password": os.environ.get("SPUG_PASSWORD"),
        "type": os.environ.get("SPUG_TYPE", "default"),
        "api_key": os.environ.get("SPUG_API_KEY"),
        "allowed_hosts": parse_host_list(os.environ.get("SPUG_ALLOWED_HOSTS")) or [],
    }
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                saved = json.load(f)
                if saved.get("allowed_hosts") is not None:
                    saved["allowed_hosts"] = parse_host_list(saved.get("allowed_hosts"))
                cfg.update({k: v for k, v in saved.items() if v is not None})
        except Exception as e:
            raise SpugError(f"配置文件 {path} 解析失败: {e}")
    if getattr(args, "url", None):
        cfg["url"] = args.url.rstrip("/")
    if getattr(args, "token", None):
        cfg["token"] = args.token
    if getattr(args, "username", None):
        cfg["username"] = args.username
    if getattr(args, "password", None):
        cfg["password"] = args.password
    if getattr(args, "type", None):
        cfg["type"] = args.type
    if getattr(args, "api_key", None):
        cfg["api_key"] = args.api_key
    if getattr(args, "allowed_hosts", None) is not None:
        cfg["allowed_hosts"] = parse_host_list(args.allowed_hosts)

    if not cfg["url"]:
        raise SpugError("未配置 Spug 地址。请先运行: spug.py config --url http://<host>:<port>")
    return cfg


def save_config(cfg, path=None):
    path = path or DEFAULT_CONFIG
    os.makedirs(os.path.dirname(path), exist_ok=True)
    to_save = dict(cfg)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(to_save, f, ensure_ascii=False, indent=2)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def do_login(cfg, config_path=None):
    """使用配置的用户名和密码自动登录并刷新保存 token。"""
    username = cfg.get("username")
    password = cfg.get("password")
    if not username or not password:
        return None
    url = cfg["url"].rstrip("/") + "/api/account/login/"
    body = {
        "username": username,
        "password": password,
        "type": cfg.get("type", "default")
    }
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
    except Exception as e:
        raise SpugError(f"自动登录请求异常: {e}")
    try:
        obj = json.loads(raw)
    except ValueError:
        raise SpugError(f"自动登录返回非 JSON 响应: {raw[:200]}")
    if obj.get("error"):
        raise SpugError(f"账号密码自动登录失败: {obj['error']}")
    data_res = obj.get("data")
    if isinstance(data_res, dict) and data_res.get("access_token"):
        new_token = data_res["access_token"]
        cfg["token"] = new_token
        save_config(cfg, config_path)
        print(f"[提示] token 已通过用户名密码自动续期成功 (user={data_res.get('nickname', username)})", file=sys.stderr)
        return new_token
    raise SpugError(f"登录成功但未返回 token: {data_res}")


def require_token(cfg, config_path=None):
    if cfg.get("token"):
        return cfg["token"]
    if cfg.get("username") and cfg.get("password"):
        token = do_login(cfg, config_path)
        if token:
            return token
    raise SpugError("未配置访问令牌或账号密码。请运行: spug.py config --username <u> --password <p> 或 spug.py login")


def check_host_safety(cfg, target_hosts, confirm_flag=False):
    """
    安全限制检查：
    1. 必须显式指定目标机器 ID。
    2. 目标机器必须在白名单（allowed_hosts，默认为空，须先通过 config --allowed-hosts 显式配置）内。
       若包含非白名单机器且未提供 --confirm，抛出安全拦截异常。
    """
    if not target_hosts:
        raise SpugError("【安全限制】必须明确指定目标机器 ID 才能执行操作（例如 --host <id> 或 --hosts <id1,id2>）。")
    allowed = cfg.get("allowed_hosts")
    if not allowed:
        allowed = []
    unauthorized = [h for h in target_hosts if h not in allowed]
    if unauthorized:
        if not confirm_flag:
            raise SpugError(
                f"【安全拦截】操作涉及非预定授权的主机 ID: {unauthorized}（当前白名单主机: {allowed}）！\n"
                "【安全规则】每次操作涉及到服务器上的其他机器时，必须先在对话中明确询问用户并取得授权。\n"
                "获得用户同意后，在命令中添加 --confirm 参数方可继续执行。"
            )
        else:
            print(f"[安全确认] 已授权执行涉及其他主机 {unauthorized} 的操作。", file=sys.stderr)


# ---------- HTTP ----------


def api(cfg, method, path, body=None, timeout=60, extra_headers=None, retry_auth=True, config_path=None):
    token = cfg.get("token")
    if not token and cfg.get("username") and cfg.get("password") and retry_auth:
        try:
            token = do_login(cfg, config_path)
        except Exception:
            pass

    url = cfg["url"].rstrip("/") + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if cfg.get("token"):
        req.add_header("X-Token", cfg["token"])
    if extra_headers:
        for k, v in extra_headers.items():
            req.add_header(k, v)
    status = 200
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        status = e.code
    except urllib.error.URLError as e:
        raise SpugError(f"无法连接 {cfg['url']}: {e.reason}")

    # 判断是否为认证失效
    is_auth_error = (status == 401)
    try:
        obj = json.loads(raw)
        if obj.get("error") and any(k in str(obj["error"]) for k in ["验证失败", "重新登录", "身份验证失败", "token"]):
            is_auth_error = True
    except ValueError:
        obj = None

    if is_auth_error and retry_auth and cfg.get("username") and cfg.get("password"):
        print("[提示] 接口检测到 token 失效，正在使用账号密码自动重新登录...", file=sys.stderr)
        try:
            new_token = do_login(cfg, config_path)
            if new_token:
                return api(cfg, method, path, body, timeout, extra_headers, retry_auth=False, config_path=config_path)
        except Exception as err:
            raise SpugError(f"登录重试失败: {err}")

    if obj is None:
        raise SpugError(f"HTTP {status}: 非 JSON 响应: {raw[:200]}")
    if obj.get("error"):
        raise SpugError(obj["error"])
    return obj.get("data")


def build_ws_url(cfg, path, token):
    scheme = "wss" if cfg["url"].startswith("https") else "ws"
    base = re.sub(r"^https?://", "", cfg["url"].rstrip("/"))
    return f"{scheme}://{base}{path}?x-token={token}"


def multipart_post(cfg, path, fields, file_field, file_name, file_data, timeout=600, retry_auth=True, config_path=None):
    """构造 multipart/form-data 上传（Spug 文件上传接口的请求体）。"""
    token = cfg.get("token")
    if not token and cfg.get("username") and cfg.get("password") and retry_auth:
        try:
            token = do_login(cfg, config_path)
        except Exception:
            pass

    boundary = "----spug-ops" + uuid.uuid4().hex
    body = bytearray()
    for k, v in fields.items():
        body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode("utf-8")
    body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; "
             f"filename=\"{file_name}\"\r\nContent-Type: application/octet-stream\r\n\r\n").encode("utf-8")
    body += file_data
    body += f"\r\n--{boundary}--\r\n".encode("utf-8")
    url = cfg["url"].rstrip("/") + path
    req = urllib.request.Request(url, data=bytes(body), method="POST")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    if cfg.get("token"):
        req.add_header("X-Token", cfg["token"])
    status = 200
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        status = e.code
    except urllib.error.URLError as e:
        raise SpugError(f"无法连接 {cfg['url']}: {e.reason}")

    is_auth_error = (status == 401)
    try:
        obj = json.loads(raw)
        if obj.get("error") and any(k in str(obj["error"]) for k in ["验证失败", "重新登录", "身份验证失败", "token"]):
            is_auth_error = True
    except ValueError:
        obj = None

    if is_auth_error and retry_auth and cfg.get("username") and cfg.get("password"):
        print("[提示] 文件上传检测到 token 失效，正在使用账号密码自动重新登录...", file=sys.stderr)
        new_token = do_login(cfg, config_path)
        if new_token:
            return multipart_post(cfg, path, fields, file_field, file_name, file_data, timeout, retry_auth=False, config_path=config_path)

    if obj is None:
        raise SpugError(f"HTTP {status}: 非 JSON 响应: {raw[:200]}")
    if obj.get("error"):
        raise SpugError(obj["error"])
    return obj.get("data")


class RawWebSocket:
    def __init__(self, sock):
        self.sock = sock

    def send(self, text):
        import struct, os
        payload = text.encode('utf-8')
        mask = os.urandom(4)
        masked = bytearray(b ^ mask[i % 4] for i, b in enumerate(payload))
        l = len(payload)
        if l < 126:
            head = bytes([0x81, 0x80 | l])
        elif l < 65536:
            head = bytes([0x81, 0x80 | 126]) + struct.pack('>H', l)
        else:
            head = bytes([0x81, 0x80 | 127]) + struct.pack('>Q', l)
        self.sock.sendall(head + mask + bytes(masked))

    def recv(self):
        import struct
        head = self.sock.recv(2)
        if not head or len(head) < 2:
            raise Exception("Connection closed")
        b1, b2 = head[0], head[1]
        opcode = b1 & 0x0f
        if opcode == 8:
            raise Exception("Connection closed by peer")
        l = b2 & 0x7f
        if l == 126:
            l = struct.unpack('>H', self.sock.recv(2))[0]
        elif l == 127:
            l = struct.unpack('>Q', self.sock.recv(8))[0]
        payload = b''
        while len(payload) < l:
            chunk = self.sock.recv(l - len(payload))
            if not chunk: break
            payload += chunk
        return payload.decode('utf-8', errors='replace')

    def settimeout(self, t):
        self.sock.settimeout(t)

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass

def ws_connect(cfg, path, timeout=30, config_path=None, retry_auth=True):
    import socket, base64, os, urllib.parse
    token = require_token(cfg, config_path)
    p = urllib.parse.urlparse(cfg["url"])
    host, port = p.hostname, p.port or (443 if p.scheme == 'https' else 80)
    bind_ip = get_physical_bind_ip()
    source_addr = (bind_ip, 0) if bind_ip else None
    s = socket.create_connection((host, port), timeout=timeout, source_address=source_addr)
    key = base64.b64encode(os.urandom(16)).decode()
    lines = [
        f"GET {path}?x-token={token} HTTP/1.1",
        f"Host: {p.netloc}",
        "Upgrade: websocket",
        "Connection: Upgrade",
        f"Sec-WebSocket-Key: {key}",
        "Sec-WebSocket-Version: 13",
        "",
        ""
    ]
    req = "\r\n".join(lines).encode("utf-8")
    s.sendall(req)
    resp = s.recv(4096)
    if b'101 ' not in resp:
        err_msg = resp.decode('latin1', errors='replace')
        if retry_auth and (b'401 ' in resp or b'403 ' in resp or '身份验证失败' in err_msg or '重新登录' in err_msg):
            if cfg.get("username") and cfg.get("password"):
                print("[提示] WebSocket 握手认证失效，正在使用账号密码自动重新登录...", file=sys.stderr)
                try:
                    s.close()
                except Exception:
                    pass
                new_token = do_login(cfg, config_path)
                if new_token:
                    return ws_connect(cfg, path, timeout, config_path, retry_auth=False)
        raise SpugError('WebSocket 握手失败: ' + err_msg[:200])
    return RawWebSocket(s)


# ---------- 子命令：config / login / whoami ----------


def cmd_config(args):
    cfg = load_config(args)
    if getattr(args, "token", None) is not None:
        cfg["token"] = args.token
    if getattr(args, "username", None) is not None:
        cfg["username"] = args.username
    if getattr(args, "password", None) is not None:
        cfg["password"] = args.password
    if getattr(args, "type", None) is not None:
        cfg["type"] = args.type
    if getattr(args, "allowed_hosts", None) is not None:
        cfg["allowed_hosts"] = parse_host_list(args.allowed_hosts)
    if getattr(args, "api_key", None) is not None:
        cfg["api_key"] = args.api_key
    if getattr(args, "url", None):
        cfg["url"] = args.url.rstrip("/")
    save_config(cfg, getattr(args, "config", None))
    shown = dict(cfg)
    if shown.get("token"):
        shown["token"] = str(shown["token"])[:6] + "***"
    if shown.get("password"):
        shown["password"] = "***"
    if shown.get("api_key"):
        shown["api_key"] = str(shown["api_key"])[:6] + "***"
    print("已保存配置到", getattr(args, "config", None) or DEFAULT_CONFIG)
    print(json.dumps(shown, ensure_ascii=False, indent=2))


def cmd_login(args):
    cfg = load_config(args)
    if not args.username:
        raise SpugError("缺少用户名，请用 --username 指定")
    cfg["username"] = args.username
    cfg["password"] = args.password
    cfg["type"] = getattr(args, "type", "default")
    # 调用统一登录
    token = do_login(cfg, getattr(args, "config", None))
    print(f"登录成功！access_token 已保存到 {getattr(args, 'config', None) or DEFAULT_CONFIG}")
    print("用户名和密码已一并保存，未来 token 8小时过期后将自动透明续期，无需重新手工登录。")


def cmd_whoami(args):
    cfg = load_config(args)
    data = api(cfg, "GET", "/api/account/self/")
    if isinstance(data, dict):
        print(f"当前登录用户: {data.get('nickname', '?')}")
    else:
        print("token 有效")


# ---------- 子命令：hosts ----------


def cmd_hosts(args):
    cfg = load_config(args)
    hosts = api(cfg, "GET", "/api/host/")
    if args.json:
        print(json.dumps(hosts, ensure_ascii=False))
        return
    if not hosts:
        print("（无主机）")
        return
    print(f"{'ID':<5}{'名称':<24}{'地址':<20}{'用户':<14}{'端口':<6}分组")
    for h in hosts:
        g = ",".join(str(x) for x in h.get("group_ids", []))
        print(f"{h.get('id',''):<5}{str(h.get('name','')):<24}"
              f"{str(h.get('hostname','')):<20}{str(h.get('username','')):<14}"
              f"{str(h.get('port','')):<6}{g}")


# ---------- 子命令：exec（批量命令执行） ----------


def parse_host_ids(s):
    ids = []
    for part in re.split(r"[,\s]+", s.strip()):
        if part:
            try:
                ids.append(int(part))
            except ValueError:
                raise SpugError(f"无效的主机 ID: {part}")
    if not ids:
        raise SpugError("请用 --hosts 指定至少一个主机 ID，例如 --hosts 1,2,3")
    return sorted(set(ids))


_MSYS_DRIVE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]|^/[A-Za-z]:")


def guard_remote_path(value, arg_name="路径"):
    """Git Bash（MSYS）会把以 / 开头的命令行参数转成 Windows 路径，
    导致远程路径错误（表现为 'No such file'）。这里做防御性检测并给出修复提示。"""
    if isinstance(value, str) and (_MSYS_DRIVE.search(value) or value.lower().startswith("c:/program files/git")):
        raise SpugError(
            f"{arg_name}参数疑似被 Git Bash 路径转换污染（实际值: {value!r}）。\n"
            "请在命令前加 MSYS_NO_PATHCONV=1 运行，例如：\n"
            "  MSYS_NO_PATHCONV=1 python spug.py <子命令> ..."
        )
    return value


def cmd_exec(args):
    cfg = load_config(args)
    host_ids = parse_host_ids(args.hosts)
    check_host_safety(cfg, host_ids, getattr(args, "confirm", False))

    # 1) 创建执行任务，拿到任务 token
    token = api(cfg, "POST", "/api/exec/do/", body={
        "host_ids": host_ids,
        "command": args.command,
        "interpreter": args.interpreter,
    }, config_path=getattr(args, "config", None))

    # 2) 订阅结果通道
    ws = ws_connect(cfg, f"/api/ws/subscribe/{token}/", timeout=30, config_path=getattr(args, "config", None))
    ws.send("ok")

    # 3) 下发任务（真正触发 SSH 执行）；失败时关闭订阅连接再报错
    try:
        api(cfg, "PATCH", "/api/exec/do/", body={
            "token": token,
            "cols": args.cols,
            "rows": args.rows,
        }, config_path=getattr(args, "config", None))
    except Exception:
        try:
            ws.close()
        except Exception:
            pass
        raise

    outputs = {hid: {"data": "", "status": -2} for hid in host_ids}
    deadline = time.time() + args.timeout
    last_activity = time.time()

    while time.time() < deadline:
        remaining = deadline - time.time()
        try:
            ws.settimeout(min(10, max(1, remaining)))
            msg = ws.recv()
            last_activity = time.time()
        except websocket.WebSocketTimeoutException:
            continue
        except websocket.WebSocketConnectionClosedException:
            break
        except Exception:
            break
        if msg == "pong":
            # 与前端行为一致：收到 pong 后回一个非数字文本，触发下一轮拉取
            ws.send("ok")
            continue
        try:
            frame = json.loads(msg)
        except ValueError:
            continue
        key = frame.get("key")
        if key is None:
            continue
        outputs.setdefault(key, {"data": "", "status": -2})
        if frame.get("data"):
            outputs[key]["data"] += frame["data"]
            if not args.raw:
                outputs[key]["data"] = outputs[key]["data"]
        if "status" in frame:
            outputs[key]["status"] = frame["status"]
        if all(o["status"] != -2 for o in outputs.values()):
            break

    try:
        ws.close()
    except Exception:
        pass

    failed = False
    for hid in sorted(outputs):
        out = outputs[hid]
        text = out["data"] if args.raw else strip_ansi(out["data"])
        status = out["status"]
        if status == 0:
            label = "成功"
        elif status == -2:
            label = "未完成/超时"
            failed = True
        else:
            label = f"失败(code={status})"
            failed = True
        print("=" * 64)
        print(f"[主机 {hid}] {label}")
        print("-" * 64)
        print(text.strip() or "（无输出）")
    if failed:
        sys.exit(1)


def cmd_exec_templates(args):
    cfg = load_config(args)
    data = api(cfg, "GET", "/api/exec/template/")
    templates = data.get("templates", []) if isinstance(data, dict) else data or []
    if args.json:
        print(json.dumps(templates, ensure_ascii=False))
        return
    if not templates:
        print("（无命令模板）")
        return
    print(f"{'ID':<5}{'名称':<28}{'类型':<10}{'解释器':<10}主机数")
    for t in templates:
        n = len(t.get("host_ids", [])) if isinstance(t.get("host_ids"), list) else "-"
        print(f"{str(t.get('id','')):<5}{str(t.get('name','')):<28}"
              f"{str(t.get('type','')):<10}{str(t.get('interpreter','')):<10}{n}")


def cmd_terminate(args):
    cfg = load_config(args)
    if args.host:
        check_host_safety(cfg, [int(args.host)], getattr(args, "confirm", False))
    body = {"token": args.token}
    if args.host:
        body["target"] = int(args.host)
    data = api(cfg, "POST", "/api/exec/terminate/", body=body, config_path=getattr(args, "config", None))
    print("已请求终止执行")


# ---------- 子命令：ssh（交互终端 / 单命令） ----------


def drain_ws(ws, min_wait, max_wait):
    """收集 WS 输出直到静默期 min_wait 或累计 max_wait。返回 (文本, 连接是否关闭)。"""
    buf = []
    last = time.time()
    start = time.time()
    closed = False
    while True:
        remaining_silent = min_wait - (time.time() - last)
        remaining_total = max_wait - (time.time() - start)
        if remaining_total <= 0:
            break
        wait = max(0.1, min(remaining_silent, remaining_total, 1.0))
        try:
            ws.settimeout(wait)
            msg = ws.recv()
        except websocket.WebSocketTimeoutException:
            if time.time() - last >= min_wait:
                break
            continue
        except websocket.WebSocketConnectionClosedException:
            closed = True
            break
        except Exception:
            closed = True
            break
        if msg:
            buf.append(msg)
            last = time.time()
    return "".join(buf), closed


def cmd_ssh(args, retry_auth=True):
    cfg = load_config(args)
    check_host_safety(cfg, [int(args.host)], getattr(args, "confirm", False))
    if args.command:
        args.command = guard_remote_path(args.command, "命令(--command)")
    config_path = getattr(args, "config", None)

    try:
        ws = ws_connect(cfg, f"/api/ws/ssh/{args.host}/", timeout=30, config_path=config_path)
    except SpugError as e:
        if retry_auth and cfg.get("username") and cfg.get("password") and any(k in str(e) for k in ["401", "403", "验证失败", "重新登录"]):
            print("[提示] 终端连接认证失效，正在使用账号密码自动重新登录...", file=sys.stderr)
            do_login(cfg, config_path)
            return cmd_ssh(args, retry_auth=False)
        raise

    # 设置一个合理的终端尺寸，避免输出因换行错乱
    ws.send(json.dumps({"resize": [args.cols, args.rows]}))

    if args.command:
        # 先等待连接建立与初始提示输出
        initial, _ = drain_ws(ws, min_wait=0.4, max_wait=3.0)
        # 核心检查：如果服务端在数据流里返回了身份验证失败
        if "用户身份验证失败" in initial:
            try:
                ws.close()
            except Exception:
                pass
            if retry_auth and cfg.get("username") and cfg.get("password"):
                print("[提示] 终端通道检测到 token 已过期，正在使用账号密码自动重新登录...", file=sys.stderr)
                new_token = do_login(cfg, config_path)
                if new_token:
                    return cmd_ssh(args, retry_auth=False)
            raise SpugError("用户身份验证失败，请重新登录或更新 token。")

        ws.send(json.dumps({"data": args.command + "\n"}))
        output, closed = drain_ws(ws, min_wait=args.quiet, max_wait=args.timeout)
        try:
            ws.close()
        except Exception:
            pass
        text = initial + output
        print(text if args.raw else strip_ansi(text))
        return

    # 交互模式：后台线程打印输出，主线程逐行发送命令
    import threading

    stop = threading.Event()

    def reader():
        while not stop.is_set():
            try:
                ws.settimeout(0.3)
                msg = ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            except Exception:
                break
            if msg:
                sys.stdout.write(msg if args.raw else strip_ansi(msg))
                sys.stdout.flush()
        try:
            ws.close()
        except Exception:
            pass

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    print("=== Spug SSH 交互终端（Ctrl+D / 输入 exit 结束）===")
    try:
        for line in sys.stdin:
            line = line.rstrip("\n")
            if line in ("exit", "quit"):
                break
            ws.send(json.dumps({"data": line + "\n"}))
    except (KeyboardInterrupt, EOFError):
        pass
    stop.set()
    t.join(timeout=1)


# ---------- 子命令：file（SSH 页面文件管理：列表/上传/下载/删除） ----------


def cmd_file_list(args):
    cfg = load_config(args)
    check_host_safety(cfg, [int(args.host)], getattr(args, "confirm", False))
    path = guard_remote_path(args.path, "远程目录(--path)")
    if not path.startswith("/"):
        path = "/" + path
    data = api(cfg, "GET", f"/api/file/?id={args.host}&path={path}", config_path=getattr(args, "config", None))
    if args.json:
        print(json.dumps(data, ensure_ascii=False))
        return
    print(f"目录: {path}")
    if not data:
        print("（空目录）")
        return
    print(f"{'名称':<30}{'类型':<4}{'大小':<12}{'修改时间':<20}权限")
    for it in data:
        kind = "目录" if it.get("kind") == "d" else "文件"
        print(f"{str(it.get('name','')):<30}{kind:<4}{str(it.get('size','')):<12}"
              f"{str(it.get('date','')):<20}{str(it.get('code',''))}")


def cmd_file_upload(args):
    cfg = load_config(args)
    check_host_safety(cfg, [int(args.host)], getattr(args, "confirm", False))
    local = os.path.abspath(args.local)
    if not os.path.isfile(local):
        raise SpugError(f"本地文件不存在: {local}")
    with open(local, "rb") as f:
        data = f.read()
    remote_dir = guard_remote_path(args.remote, "远程目录(--remote)")
    if not remote_dir.startswith("/"):
        remote_dir = "/" + remote_dir
    fname = os.path.basename(local)
    fields = {"id": str(args.host), "token": uuid.uuid4().hex, "path": remote_dir}
    multipart_post(cfg, "/api/file/object/", fields, "file", fname, data, timeout=args.timeout, config_path=getattr(args, "config", None))
    print(f"上传成功: {local} -> {remote_dir}/{fname} (host {args.host}, {len(data)} 字节)")


def cmd_file_download(args, retry_auth=True):
    cfg = load_config(args)
    check_host_safety(cfg, [int(args.host)], getattr(args, "confirm", False))
    remote = guard_remote_path(args.remote, "远程文件(--remote)")
    if not remote.startswith("/"):
        remote = "/" + remote
    config_path = getattr(args, "config", None)
    token = cfg.get("token")
    if not token and cfg.get("username") and cfg.get("password") and retry_auth:
        try:
            token = do_login(cfg, config_path)
        except Exception:
            pass
    url = cfg["url"].rstrip("/") + f"/api/file/object/?id={args.host}&file={remote}"
    req = urllib.request.Request(url)
    if cfg.get("token"):
        req.add_header("X-Token", cfg["token"])
    try:
        with urllib.request.urlopen(req, timeout=args.timeout) as resp:
            data = resp.read()
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        if e.code == 401 and retry_auth and cfg.get("username") and cfg.get("password"):
            print("[提示] 文件下载检测到 token 失效，正在使用账号密码自动重新登录...", file=sys.stderr)
            new_token = do_login(cfg, config_path)
            if new_token:
                return cmd_file_download(args, retry_auth=False)
        try:
            obj = json.loads(raw)
            err = obj.get("error") or raw
        except ValueError:
            err = raw
        raise SpugError(f"下载失败: {err}")
    local = os.path.join(args.local or os.getcwd(), os.path.basename(remote))
    local = os.path.normpath(local)
    os.makedirs(os.path.dirname(os.path.abspath(local)), exist_ok=True)
    with open(local, "wb") as f:
        f.write(data)
    print(f"下载成功: {remote} -> {os.path.abspath(local)} ({len(data)} 字节)")


def cmd_file_delete(args):
    cfg = load_config(args)
    check_host_safety(cfg, [int(args.host)], getattr(args, "confirm", False))
    remote = guard_remote_path(args.remote, "远程文件(--remote)")
    if not remote.startswith("/"):
        remote = "/" + remote
    api(cfg, "DELETE", f"/api/file/object/?id={args.host}&file={remote}", config_path=getattr(args, "config", None))
    print(f"已删除: {remote} (host {args.host})")


# ---------- 子命令：deploy ----------


def cmd_deploy(args):
    cfg = load_config(args)
    data = api(cfg, "GET", f"/api/deploy/request/{args.id}/")
    status = str(data.get("status", "?"))
    print(f"部署申请 #{args.id} 状态: {DEPLOY_STATUS.get(status, status)}")
    outputs = data.get("outputs", {}) or {}
    if args.json:
        print(json.dumps({"status": data.get("status"), "outputs": outputs}, ensure_ascii=False))
        return
    for key, item in outputs.items():
        title = item.get("title", key)
        text = item.get("data", "")
        if args.raw:
            shown = text
        else:
            shown = strip_ansi(text)
        print("=" * 64)
        print(f"[{title}]")
        print("-" * 64)
        print(shown.strip() or "（暂无输出）")


def cmd_deploy_list(args):
    cfg = load_config(args)
    data = api(cfg, "GET", "/api/deploy/request/")
    items = data if isinstance(data, list) else (data.get("list", []) if isinstance(data, dict) else [])
    if args.json:
        print(json.dumps(items, ensure_ascii=False))
        return
    if not items:
        print("（无部署申请）")
        return
    print(f"{'ID':<6}{'名称':<30}{'状态':<10}{'版本':<14}{'申请人':<12}")
    for it in items:
        status = str(it.get("status", "?"))
        print(f"{str(it.get('id','')):<6}{str(it.get('name','')):<30}"
              f"{DEPLOY_STATUS.get(status, status):<10}{str(it.get('version') or ''):<14}"
              f"{str(it.get('create_user') or it.get('created_by') or ''):<12}")


# ---------- 入口 ----------


def main():
    p = argparse.ArgumentParser(prog="spug.py", description="Spug 运维平台命令行助手")
    p.add_argument("--url", help="Spug 地址，例如 http://<your-spug-host>:<port>")
    p.add_argument("--token", help="access_token（浏览器登录后读取）")
    p.add_argument("--username", help="Spug 用户名")
    p.add_argument("--password", help="Spug 密码")
    p.add_argument("--allowed-hosts", help="允许操作的主机白名单（逗号分隔，如 1,2）")
    p.add_argument("--confirm", action="store_true", help="确认执行涉及非白名单机器的操作")
    p.add_argument("--api-key", dest="api_key", help="开放服务 API 密钥（备用）")
    p.add_argument("--config", help=f"配置文件路径（默认 {DEFAULT_CONFIG}）")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_config = sub.add_parser("config", help="写入/更新配置")
    p_config.add_argument("--token")
    p_config.add_argument("--username")
    p_config.add_argument("--password")
    p_config.add_argument("--type", choices=["default", "ldap"])
    p_config.add_argument("--allowed-hosts", help="允许操作的主机白名单（如 1 或 1,2）")
    p_config.add_argument("--api-key", dest="api_key")
    p_config.set_defaults(func=cmd_config)

    p_login = sub.add_parser("login", help="账号密码登录并保存 token 与凭据")
    p_login.add_argument("--username", required=True)
    p_login.add_argument("--password", required=True)
    p_login.add_argument("--type", default="default", choices=["default", "ldap"])
    p_login.set_defaults(func=cmd_login)

    p_who = sub.add_parser("whoami", help="校验 token 是否有效")
    p_who.set_defaults(func=cmd_whoami)

    p_hosts = sub.add_parser("hosts", help="列出主机")
    p_hosts.add_argument("--json", action="store_true")
    p_hosts.set_defaults(func=cmd_hosts)

    p_tpl = sub.add_parser("exec-templates", help="列出命令模板")
    p_tpl.add_argument("--json", action="store_true")
    p_tpl.set_defaults(func=cmd_exec_templates)

    p_exec = sub.add_parser("exec", help="在主机上批量执行命令（SSH 底层）")
    p_exec.add_argument("--hosts", required=True, help="主机 ID，逗号分隔，如 1,2")
    p_exec.add_argument("--command", required=True, help="要执行的命令")
    p_exec.add_argument("--interpreter", default="sh")
    p_exec.add_argument("--timeout", type=float, default=600, help="整体超时秒数（默认 600）")
    p_exec.add_argument("--cols", type=int, default=120)
    p_exec.add_argument("--rows", type=int, default=30)
    p_exec.add_argument("--raw", action="store_true", help="保留 ANSI 控制字符")
    p_exec.add_argument("--confirm", action="store_true", help="确认执行涉及其他非白名单机器的操作")
    p_exec.set_defaults(func=cmd_exec)

    p_term = sub.add_parser("terminate", help="终止正在执行的命令")
    p_term.add_argument("--token", required=True, help="exec 返回的任务 token")
    p_term.add_argument("--host", help="指定主机 ID（可选，默认全部）")
    p_term.add_argument("--confirm", action="store_true", help="确认执行操作")
    p_term.set_defaults(func=cmd_terminate)

    p_ssh = sub.add_parser("ssh", help="SSH 终端：--command 执行单条命令，否则进入交互模式")
    p_ssh.add_argument("--host", required=True, type=int, help="主机 ID")
    p_ssh.add_argument("--command", help="要执行的命令（不加则进入交互模式）")
    p_ssh.add_argument("--timeout", type=float, default=300, help="单命令超时秒数（默认 300）")
    p_ssh.add_argument("--quiet", type=float, default=2.0, help="命令静默判定秒数（默认 2）")
    p_ssh.add_argument("--cols", type=int, default=120)
    p_ssh.add_argument("--rows", type=int, default=30)
    p_ssh.add_argument("--raw", action="store_true")
    p_ssh.add_argument("--confirm", action="store_true", help="确认执行涉及其他非白名单机器的操作")
    p_ssh.set_defaults(func=cmd_ssh)

    p_f_list = sub.add_parser("file-list", help="列出主机上的目录/文件（SSH 页面文件管理）")
    p_f_list.add_argument("--host", required=True, type=int, help="主机 ID")
    p_f_list.add_argument("--path", default="/", help="远程目录（默认 /）")
    p_f_list.add_argument("--json", action="store_true")
    p_f_list.add_argument("--confirm", action="store_true", help="确认执行涉及其他非白名单机器的操作")
    p_f_list.set_defaults(func=cmd_file_list)

    p_f_up = sub.add_parser("file-upload", help="上传本地文件到主机（经 Spug SFTP，无需主机凭据）")
    p_f_up.add_argument("--host", required=True, type=int, help="主机 ID")
    p_f_up.add_argument("--local", required=True, help="本地文件路径")
    p_f_up.add_argument("--remote", default="/", help="远程目标目录（默认 /）")
    p_f_up.add_argument("--timeout", type=float, default=600, help="上传超时秒数（默认 600）")
    p_f_up.add_argument("--confirm", action="store_true", help="确认执行涉及其他非白名单机器的操作")
    p_f_up.set_defaults(func=cmd_file_upload)

    p_f_down = sub.add_parser("file-download", help="从主机下载文件到本地")
    p_f_down.add_argument("--host", required=True, type=int, help="主机 ID")
    p_f_down.add_argument("--remote", required=True, help="远程文件完整路径")
    p_f_down.add_argument("--local", help="本地保存目录（默认当前目录）")
    p_f_down.add_argument("--timeout", type=float, default=600)
    p_f_down.add_argument("--confirm", action="store_true", help="确认执行涉及其他非白名单机器的操作")
    p_f_down.set_defaults(func=cmd_file_download)

    p_f_del = sub.add_parser("file-delete", help="删除主机上的文件")
    p_f_del.add_argument("--host", required=True, type=int, help="主机 ID")
    p_f_del.add_argument("--remote", required=True, help="远程文件完整路径")
    p_f_del.add_argument("--confirm", action="store_true", help="确认执行涉及其他非白名单机器的操作")
    p_f_del.set_defaults(func=cmd_file_delete)

    p_dep = sub.add_parser("deploy", help="查询部署申请详情")
    p_dep.add_argument("--id", required=True, type=int)
    p_dep.add_argument("--json", action="store_true")
    p_dep.add_argument("--raw", action="store_true")
    p_dep.set_defaults(func=cmd_deploy)

    p_dep_list = sub.add_parser("deploy-list", help="列出最近部署申请")
    p_dep_list.add_argument("--json", action="store_true")
    p_dep_list.set_defaults(func=cmd_deploy_list)

    args = p.parse_args()
    try:
        args.func(args)
    except SpugError as e:
        print(f"错误: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
