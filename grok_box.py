"""Grok Bot Box relay：给本机 Cursor 的 Sand 流量申请 Box 网关并落盘描述符。

2026-09-09 起（Cursor 3.19.13 / Grok Bot 0.44.0）服务端不再接受「Cursor 登录票 + sand 身份」
直连 api2.cursor.sh 的 InferenceService/Stream（回 401 ERROR_NOT_LOGGED_IN /
"Sand traffic is not supported on this endpoint"）。可行路线是让 Grok Bot 的云端 Box 代转：

  1. 用本机 Cursor 登录票调 aiserver.v1.GrokBotService/EnsureSandBox，拿到该账号 Box 的
     网关 baseUrl + 短期 token（+ x-anyrun-network-token）；
  2. 写成 grok-box-relay.json。sand_patch 注入到 Cursor applyAuthorization 的 JS 每次
     Stream 请求都重读它：URL 改到 <baseUrl>/sand-stream-relay/aiserver.v1.InferenceService/Stream，
     Authorization 换成 Box token；票快过期时 JS 用描述符里的 Cursor 票自己重新 mint；
  3. Box 默认没有 relay 路由：首次通过 Box 网关 /api/createAgent + /api/sendPrompt 让 Box 内
     的 Agent 往 host-main.cjs 加这条路由，然后轮询探活直到 200 + Connect 结束帧。

请求形态（header、x-cursor-checksum、protobuf 字段号）与 SandClaimer 1.4.2 grok_box.py 和
cursor-sdk2api 的 sand-box-minter.ts 一致，均经 api2.cursor.sh 实测；描述符文件路径与
cursor-sdk2api（SAND_BOX_RELAY_SOURCE=file）共用同一份契约。只依赖标准库。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
import sys
import time
import uuid
from pathlib import Path
from typing import Callable, Dict, Iterator, Mapping, Optional, Tuple, Union

import local_cursor

GROK_BACKEND_DEFAULT_URL = "https://api2.cursor.sh"
ENSURE_SANDBOX_METHOD = "aiserver.v1.GrokBotService/EnsureSandBox"
BOX_RELAY_PATH = "/sand-stream-relay/aiserver.v1.InferenceService/Stream"
# 控制面与 relay 数据面都以 Grok Bot 桌面端身份出现；Cursor 自己的普通请求不改版本号。
BOX_CLIENT_TYPE = "sand"
BOX_CLIENT_VERSION = "0.44.0"
BOX_NAMESPACE = "prod"
RELAY_CONFIG_ENV = "SAND_GROK_BOX_RELAY_CONFIG"
# Box 网关票约 1 小时；注入的 JS 按 mintedAtMs + refreshAfterMs 或 JWT exp 判断是否重新 mint。
DESCRIPTOR_REFRESH_AFTER_MS = 3600 * 1000
ENSURE_POLL_SECONDS = 4
PROBE_POLL_SECONDS = 5
RUN_STATE_RUNNING = 3
RUN_STATE_LABELS: Dict[int, str] = {
    0: "UNSPECIFIED",
    1: "ABSENT",
    2: "HIBERNATED",
    3: "RUNNING",
    4: "STARTING",
}
# EnsureSandBoxResponse 字段号（实测）：10 网关 baseUrl，11 网关 token，4 x-anyrun-network-token，13 runState。
_F_NETWORK_TOKEN = 4
_F_GATEWAY_URL = 10
_F_GATEWAY_TOKEN = 11
_F_RUN_STATE = 13
# 同一 relay agent 15 分钟内不重复发指令，避免重复扣费。
_RESEND_PROMPT_AFTER_MS = 15 * 60 * 1000

BOX_RELAY_PROVISION_PROMPT = r"""
请为当前 Box 幂等安装 Cursor Sand Stream relay。开始前先检查
/home/box/sand-host/host-main.cjs：如果已经存在唯一 POST 路由
/sand-stream-relay/aiserver.v1.InferenceService/Stream，则不要重复写入，只确认
host 正在使用该版本。

若路由不存在：先备份将修改的文件；在现有 host gateway HTTP 服务的
handleRequest 中增加该唯一 POST 路由，并复用现有 isAuthorized /
SAND_GATEWAY_TOKEN 鉴权。原样接收 application/connect+proto 请求体并保持
流式转发、背压和客户端中止。转发到 backendUrl 的
/aiserver.v1.InferenceService/Stream；丢弃客户端 Authorization，调用 Box
内现有 getGrokBotToken 或 credential store 获取短期 token 后写入 Bearer；
设置 Grok Bot 0.44 sand headers；不修改 model request，不定义模型列表；
原样转发 HTTP 状态、Connect 关键响应头和响应流。不要打印、返回、保存或
暴露任何 token/credential。

完成后做不含真实生成内容的空 Connect 路由健康检查，并按当前 Box 的标准
方式重启 sand host。若没有安全挂载点，停止修改并明确报告原因。
""".strip()

Logger = Optional[Callable[[str], None]]


class GrokBoxError(RuntimeError):
    pass


def backend_url() -> str:
    for key in ("SAND_BACKEND_URL", "CURSOR_API_BASE_URL"):
        value = (os.environ.get(key) or "").strip().rstrip("/")
        if value.startswith("https://"):
            return value
    return GROK_BACKEND_DEFAULT_URL


# ---- 描述符文件（与注入 JS / cursor-sdk2api 共用的路径契约）----


def relay_config_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Local"
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support"
    else:
        root = Path.home() / ".config"
    return root / "SandClientModeStream" / "sand-client-cli"


def relay_config_path() -> Path:
    override = (os.environ.get(RELAY_CONFIG_ENV) or "").strip()
    if override:
        return Path(override).expanduser()
    return relay_config_dir() / "grok-box-relay.json"


def _provision_state_path() -> Path:
    return relay_config_dir() / "box-provision-state.json"


def _write_json_atomic(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.parent / f".{path.name}.{os.getpid()}-{time.time_ns()}.tmp"
    data = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        if temp.exists():
            try:
                temp.unlink()
            except OSError:
                pass


# ---- 本机 Cursor 登录态（只读）----


def _state_value(key: str) -> str:
    path = local_cursor.state_db_path()
    if not os.path.isfile(path):
        raise GrokBoxError(f"未找到 Cursor 状态库：{path}")
    uri = "file:{}?mode=ro&immutable=1".format(path.replace("\\", "/"))
    try:
        con = sqlite3.connect(uri, uri=True, timeout=5)
    except sqlite3.Error as exc:
        raise GrokBoxError("无法打开 Cursor 状态库") from exc
    try:
        row = con.execute(
            "SELECT value FROM ItemTable WHERE key=? LIMIT 1", (key,)
        ).fetchone()
    except sqlite3.Error as exc:
        raise GrokBoxError("读取 Cursor 状态库失败") from exc
    finally:
        con.close()
    if not row or row[0] is None:
        raise GrokBoxError(f"Cursor 状态库缺少 {key}；请确认已在 Cursor 登录")
    raw = row[0]
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    raw = str(raw)
    try:
        decoded = json.loads(raw)
        if isinstance(decoded, str):
            return decoded
    except Exception:
        pass
    return raw


def read_cursor_login() -> Tuple[str, str]:
    """返回 (accessToken, serviceMachineId)。机器码缺失不算错：后端不校验它。"""
    token = _state_value("cursorAuth/accessToken")
    try:
        machine_id = _state_value("storage.serviceMachineId")
    except GrokBoxError:
        machine_id = ""
    return token, machine_id


# ---- JWT / 指纹 / checksum ----


def jwt_payload(token: str) -> Optional[Dict[str, object]]:
    parts = (token or "").split(".")
    if len(parts) != 3:
        return None
    try:
        raw = base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4))
        payload = json.loads(raw.decode("utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def token_expires_at_ms(token: str) -> int:
    payload = jwt_payload(token)
    exp = payload.get("exp") if payload else None
    return int(exp) * 1000 if isinstance(exp, (int, float)) else 0


def account_scope(access_token: str) -> str:
    """sha256(JWT sub 或整段 token)，Grok Bot 用它给每个账号的 Box 描述符分槽。"""
    payload = jwt_payload(access_token)
    subject = payload.get("sub") if payload else None
    principal = subject if isinstance(subject, str) and subject else access_token
    return hashlib.sha256(principal.encode("utf-8")).hexdigest()


def account_fingerprint(access_token: str) -> str:
    return hashlib.sha256(account_scope(access_token).encode("ascii")).hexdigest()[:16]


def account_label(access_token: str) -> str:
    payload = jwt_payload(access_token)
    if payload is None:
        return "未知（token 非 JWT）"
    for key in ("email", "user_email", "https://cursor.com/email"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    subject = payload.get("sub")
    if isinstance(subject, str) and subject:
        return f"sub:{subject}"
    return "未知（无 email/sub 声明）"


def cursor_checksum(machine_id: str, now_ms: Optional[int] = None) -> str:
    """Grok Bot 0.44 createCursorChecksum 的逐位移植（JS >> 是 int32 语义，位移数 &31）。

    六字节由粗粒度时间戳（ms // 1e6）派生、异或链式混淆后 base64url，再拼机器码。
    后端不校验机器码（空串也过），时间戳前缀才是关键。
    """
    epoch = int(time.time() * 1000 if now_ms is None else now_ms) // 1_000_000

    def js_shr(value: int, count: int) -> int:
        v = value & 0xFFFFFFFF
        if v & 0x80000000:
            v -= 0x100000000
        return v >> (count & 31)

    raw = [
        js_shr(epoch, 40) & 255,
        js_shr(epoch, 32) & 255,
        js_shr(epoch, 24) & 255,
        js_shr(epoch, 16) & 255,
        js_shr(epoch, 8) & 255,
        epoch & 255,
    ]
    previous = 165
    for index in range(len(raw)):
        raw[index] = ((raw[index] ^ previous) + (index % 256)) & 255
        previous = raw[index]
    prefix = base64.urlsafe_b64encode(bytes(raw)).decode("ascii").rstrip("=")
    return prefix + (machine_id or "")


# ---- 最小 protobuf（只用 wire 0 / 2）----


def _pb_varint(value: int) -> bytes:
    if value < 0:
        value += 1 << 64
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _pb_read_varint(data: bytes, offset: int) -> Tuple[int, int]:
    result = 0
    shift = 0
    while True:
        if offset >= len(data):
            raise GrokBoxError("EnsureSandBox 响应被截断（varint）")
        byte = data[offset]
        offset += 1
        result |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            return result, offset
        shift += 7
        if shift > 70:
            raise GrokBoxError("EnsureSandBox 响应格式错误（varint 过长）")


def pb_bool(field: int, value: bool) -> bytes:
    return _pb_varint((field << 3) | 0) + _pb_varint(1 if value else 0)


def pb_iter_fields(data: bytes) -> Iterator[Tuple[int, int, Union[int, bytes]]]:
    offset = 0
    length = len(data)
    while offset < length:
        key, offset = _pb_read_varint(data, offset)
        field = key >> 3
        wire = key & 7
        if wire == 0:
            value, offset = _pb_read_varint(data, offset)
            yield field, wire, value
        elif wire == 2:
            size, offset = _pb_read_varint(data, offset)
            end = offset + size
            if end > length:
                raise GrokBoxError("EnsureSandBox 响应格式错误（长度越界）")
            yield field, wire, data[offset:end]
            offset = end
        elif wire == 1:
            yield field, wire, data[offset : offset + 8]
            offset += 8
        elif wire == 5:
            yield field, wire, data[offset : offset + 4]
            offset += 4
        else:
            raise GrokBoxError(f"EnsureSandBox 响应含不支持的 wire 类型 {wire}")


def parse_ensure_reply(body: bytes) -> Dict[str, object]:
    reply: Dict[str, object] = {
        "baseUrl": "",
        "token": "",
        "networkToken": "",
        "runState": 0,
    }
    for field, wire, value in pb_iter_fields(body):
        if wire == 2 and isinstance(value, bytes):
            text = value.decode("utf-8", errors="replace")
            if field == _F_GATEWAY_URL:
                reply["baseUrl"] = text
            elif field == _F_GATEWAY_TOKEN:
                reply["token"] = text
            elif field == _F_NETWORK_TOKEN:
                reply["networkToken"] = text
        elif wire == 0 and field == _F_RUN_STATE and isinstance(value, int):
            reply["runState"] = value
    return reply


def run_state_label(run_state: int) -> str:
    return RUN_STATE_LABELS.get(int(run_state), str(run_state))


# ---- 控制面：GrokBotService ----


def control_headers(access_token: str, machine_id: str) -> Dict[str, str]:
    return {
        "Authorization": "Bearer " + access_token,
        "Connect-Protocol-Version": "1",
        "Content-Type": "application/proto",
        "x-cursor-checksum": cursor_checksum(machine_id),
        "x-cursor-client-type": BOX_CLIENT_TYPE,
        "x-cursor-client-version": BOX_CLIENT_VERSION,
        "x-sand-box-namespace": BOX_NAMESPACE,
        "x-ghost-mode": "true",
        "x-request-id": str(uuid.uuid4()),
    }


def grok_rpc(
    method: str,
    request: bytes,
    access_token: str,
    machine_id: str,
    timeout: float = 30,
    retries: int = 5,
) -> bytes:
    from urllib.error import HTTPError, URLError
    from urllib.request import Request, urlopen

    url = backend_url() + "/" + method
    attempt = 0
    while True:
        req = Request(
            url,
            data=request,
            headers=control_headers(access_token, machine_id),
            method="POST",
        )
        try:
            with urlopen(req, timeout=timeout) as response:
                body = response.read(4 * 1024 * 1024 + 1)
                if len(body) > 4 * 1024 * 1024:
                    raise GrokBoxError("Grok Bot 后端响应过大")
                return body
        except HTTPError as exc:
            try:
                detail = exc.read(4096).decode("utf-8", errors="replace")
            finally:
                exc.close()
            # 429/5xx 多为 Box 还在启动或后端瞬时不可用，服务端也标 isRetryable。
            if exc.code in (429, 500, 502, 503, 504) and attempt < retries:
                attempt += 1
                time.sleep(min(2**attempt, 15))
                continue
            if exc.code in (401, 403):
                hint = "（Cursor 登录票被拒或该账号没有 Grok Bot Box 资格）"
            elif exc.code in (429, 500, 502, 503, 504):
                hint = "（服务暂时不可用，可稍后重试；请确认 Box 已启动）"
            else:
                hint = ""
            raise GrokBoxError(
                f"Grok Bot {method.rsplit('/', 1)[-1]} 失败：HTTP {exc.code}{hint} {detail[:300]}"
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            if attempt < retries:
                attempt += 1
                time.sleep(min(2**attempt, 15))
                continue
            raise GrokBoxError(f"无法连接 Grok Bot 后端（{method}）：{exc}") from exc


def ensure_sand_box(access_token: str, machine_id: str, wake: bool = True) -> Dict[str, object]:
    """EnsureSandBox 一次；wake=True 会拉起休眠的 Box（请求字段 2 = true）。"""
    body = pb_bool(2, True) if wake else b""
    return parse_ensure_reply(grok_rpc(ENSURE_SANDBOX_METHOD, body, access_token, machine_id))


def wait_box_running(
    access_token: str,
    machine_id: str,
    wait_seconds: float = 150,
    log: Logger = None,
) -> Dict[str, object]:
    """轮询 EnsureSandBox 直到 RUNNING（或超时），返回最后一次响应。"""
    deadline = time.monotonic() + wait_seconds
    while True:
        reply = ensure_sand_box(access_token, machine_id, wake=True)
        run_state = int(reply.get("runState") or 0)
        if run_state == RUN_STATE_RUNNING and reply.get("baseUrl") and reply.get("token"):
            return reply
        if time.monotonic() >= deadline:
            return reply
        if log is not None:
            log(f"Box 尚未 RUNNING（状态 {run_state_label(run_state)}），等待启动 ...")
        time.sleep(ENSURE_POLL_SECONDS)


def build_descriptor(
    reply: Mapping[str, object], access_token: str, machine_id: str
) -> Dict[str, object]:
    headers: Dict[str, str] = {}
    network_token = str(reply.get("networkToken") or "")
    if network_token:
        headers["x-anyrun-network-token"] = network_token
    descriptor: Dict[str, object] = {
        "version": 1,
        "baseUrl": str(reply.get("baseUrl") or ""),
        "token": str(reply.get("token") or ""),
        "headers": headers,
        "relayPath": BOX_RELAY_PATH,
        "accountFingerprint": account_fingerprint(access_token),
        "runState": int(reply.get("runState") or 0),
        "mintedAtMs": int(time.time() * 1000),
        "refreshAfterMs": DESCRIPTOR_REFRESH_AFTER_MS,
        # 注入 JS 用这一块在票快过期时自己重新 EnsureSandBox；Cursor 票有效期以月计。文件 0600。
        "refresh": {
            "backendUrl": backend_url(),
            "accessToken": access_token,
            "machineId": machine_id or "",
        },
    }
    return descriptor


def mint_descriptor(
    access_token: Optional[str] = None,
    machine_id: Optional[str] = None,
    wait_seconds: float = 150,
    log: Logger = None,
) -> Dict[str, object]:
    """EnsureSandBox → 等 Box RUNNING → 写 grok-box-relay.json；返回描述符。"""
    if not access_token:
        access_token, login_machine_id = read_cursor_login()
        if machine_id is None:
            machine_id = login_machine_id
    machine_id = machine_id or ""
    if log is not None:
        log(f"Cursor 账号：{account_label(access_token)}")
        log("通过 EnsureSandBox 获取该账号的 Box gateway ...")
    reply = wait_box_running(access_token, machine_id, wait_seconds=wait_seconds, log=log)
    run_state = int(reply.get("runState") or 0)
    if run_state != RUN_STATE_RUNNING or not reply.get("baseUrl") or not reply.get("token"):
        raise GrokBoxError(
            f"该账号的 Grok Bot Box 未进入 RUNNING（状态 {run_state_label(run_state)}）；"
            "可能没有 Grok Bot 资格 / Box 尚未开通，或启动超时，请稍后重试"
        )
    if not str(reply["baseUrl"]).startswith("https://"):
        raise GrokBoxError("EnsureSandBox 返回的 Box gateway 地址不是 https")
    descriptor = build_descriptor(reply, access_token, machine_id)
    _write_json_atomic(relay_config_path(), descriptor)
    if log is not None:
        log(f"Box gateway 描述符已写入：{relay_config_path()}")
    return descriptor


def load_descriptor() -> Optional[Dict[str, object]]:
    """读描述符；不存在返回 None，存在但不合法抛 GrokBoxError。"""
    path = relay_config_path()
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise GrokBoxError(f"Box gateway 描述符不是合法 JSON：{path}") from exc
    if not isinstance(value, dict):
        raise GrokBoxError("Box gateway 描述符不是 JSON 对象")
    if not str(value.get("baseUrl") or "").startswith("https://"):
        raise GrokBoxError("Box gateway 描述符缺少 https baseUrl")
    if not isinstance(value.get("token"), str) or not value["token"]:
        raise GrokBoxError("Box gateway 描述符缺少 token")
    if not isinstance(value.get("headers"), dict):
        value["headers"] = {}
    return value


def remove_descriptor() -> bool:
    removed = False
    for path in (relay_config_path(), _provision_state_path()):
        try:
            path.unlink()
            removed = True
        except FileNotFoundError:
            continue
    return removed


def descriptor_status(access_token: Optional[str] = None) -> Dict[str, object]:
    """给状态面板用：描述符是否存在、是否属于当前登录账号、多久前 mint、能否自刷新。"""
    path = relay_config_path()
    info: Dict[str, object] = {
        "present": False,
        "path": str(path),
        "valid": False,
        "error": "",
        "accountMatches": None,
        "hasRefresh": False,
        "ageMinutes": None,
        "tokenExpired": None,
        "runState": None,
    }
    try:
        descriptor = load_descriptor()
    except GrokBoxError as exc:
        info["present"] = True
        info["error"] = str(exc)
        return info
    if descriptor is None:
        return info
    info["present"] = True
    info["valid"] = True
    refresh = descriptor.get("refresh")
    info["hasRefresh"] = bool(
        isinstance(refresh, dict) and refresh.get("accessToken") and refresh.get("backendUrl")
    )
    minted = descriptor.get("mintedAtMs")
    if isinstance(minted, (int, float)) and minted > 0:
        info["ageMinutes"] = int(max(0, time.time() * 1000 - minted) // 60000)
    run_state = descriptor.get("runState")
    if isinstance(run_state, int):
        info["runState"] = run_state_label(run_state)
    exp = token_expires_at_ms(str(descriptor.get("token") or ""))
    if exp:
        info["tokenExpired"] = exp <= time.time() * 1000
    if access_token is None:
        try:
            access_token, _machine = read_cursor_login()
        except GrokBoxError:
            access_token = None
    fingerprint = descriptor.get("accountFingerprint")
    if access_token and isinstance(fingerprint, str) and fingerprint:
        info["accountMatches"] = fingerprint == account_fingerprint(access_token)
    return info


# ---- Box 网关：探活 + 首次装 relay 路由 ----


def _box_url(descriptor: Mapping[str, object], path: str) -> str:
    from urllib.parse import urlsplit, urlunsplit

    base = urlsplit(str(descriptor["baseUrl"]))
    if (
        base.scheme != "https"
        or not base.netloc
        or base.username is not None
        or base.password is not None
    ):
        raise GrokBoxError("Grok Bot Box gateway 地址无效")
    # relay 路由与网关 /events、/api/* 都挂在 baseUrl 自带的路径前缀下，不能丢。
    joined = base.path.rstrip("/") + "/" + path.lstrip("/")
    return urlunsplit((base.scheme, base.netloc, joined, "", ""))


def _box_headers(
    descriptor: Mapping[str, object], extra: Optional[Mapping[str, str]] = None
) -> Dict[str, str]:
    blocked = {"authorization", "host", "content-length", "transfer-encoding"}
    headers: Dict[str, str] = {}
    values = descriptor.get("headers")
    if isinstance(values, dict):
        for name, value in values.items():
            if (
                isinstance(name, str)
                and name
                and isinstance(value, str)
                and value
                and name.casefold() not in blocked
            ):
                headers[name] = value
    headers["Authorization"] = "Bearer " + str(descriptor["token"])
    if extra:
        overridden = {name.casefold() for name in extra}
        headers = {k: v for k, v in headers.items() if k.casefold() not in overridden}
        headers.update(extra)
    return headers


def _box_request(
    descriptor: Mapping[str, object],
    path: str,
    *,
    method: str,
    data: Optional[bytes] = None,
    headers: Optional[Mapping[str, str]] = None,
    timeout: float = 20,
) -> Tuple[int, Dict[str, str], bytes]:
    from urllib.error import HTTPError, URLError
    from urllib.request import Request, urlopen

    request = Request(
        _box_url(descriptor, path),
        data=data,
        headers=_box_headers(descriptor, headers),
        method=method,
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read(1024 * 1024 + 1)
            if len(body) > 1024 * 1024:
                raise GrokBoxError("Grok Bot Box gateway 响应过大")
            return response.status, dict(response.headers.items()), body
    except HTTPError as exc:
        try:
            body = exc.read(64 * 1024)
        finally:
            exc.close()
        return exc.code, dict(exc.headers.items()), body
    except (URLError, TimeoutError, OSError) as exc:
        raise GrokBoxError(f"无法连接 Grok Bot Box gateway：{exc}") from exc


def connect_stream_has_end_frame(body: bytes) -> bool:
    """Connect 流帧 = flags(1) + 大端长度(4) + 负载；relay 转发的 Stream 必以 end-stream 帧（flags&2）收尾。"""
    offset = 0
    saw_end = False
    while offset + 5 <= len(body):
        flags = body[offset]
        length = int.from_bytes(body[offset + 1 : offset + 5], "big")
        offset += 5
        if offset + length > len(body):
            return False
        if flags & 0x02:
            saw_end = True
        offset += length
    return saw_end and offset == len(body)


def probe_relay(descriptor: Mapping[str, object]) -> Tuple[int, str, bool]:
    """POST 一个空 Connect 帧到 relay 路由；200 + connect+proto + 结束帧才算路由真的挂上了。"""
    status, response_headers, body = _box_request(
        descriptor,
        str(descriptor.get("relayPath") or BOX_RELAY_PATH),
        method="POST",
        data=b"\x00\x00\x00\x00\x00",
        headers={
            "Content-Type": "application/connect+proto",
            "Connect-Protocol-Version": "1",
            "X-Request-Id": str(uuid.uuid4()),
        },
        timeout=25,
    )
    content_type = next(
        (value for name, value in response_headers.items() if name.casefold() == "content-type"),
        "",
    )
    ok = (
        status == 200
        and content_type.casefold().startswith("application/connect+proto")
        and connect_stream_has_end_frame(body)
    )
    return status, content_type, ok


def _find_agent_id(value: object) -> str:
    stack = [value]
    fallback = ""
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            for key in ("agentId", "id"):
                candidate = current.get(key)
                if isinstance(candidate, str) and candidate:
                    if key == "agentId":
                        return candidate
                    if not fallback:
                        fallback = candidate
            stack.extend(v for v in current.values() if isinstance(v, (dict, list)))
        elif isinstance(current, list):
            stack.extend(current)
    return fallback


def _create_box_agent(descriptor: Mapping[str, object]) -> str:
    # 后端列出的 agent 对 Box 协调器是未知的（"Sand agent ... does not exist"），
    # 必须用 Box 网关自己的 /api/createAgent 在 Box 内实例化。
    body = json.dumps(
        {
            "name": "Cursor Sand Relay",
            "description": "Hosts the Cursor Sand Stream relay route.",
            "creationRoute": {"kind": "box"},
            "harness": "box",
            "isIntroductionSuppressed": True,
            "isKickstartRequested": False,
            "clientNonce": str(uuid.uuid4()),
            "supportsTemporalHarness": True,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    status, _headers, response_body = _box_request(
        descriptor,
        "/api/createAgent",
        method="POST",
        data=body,
        headers={"Content-Type": "application/json", "x-sand-slim-avatars": "1"},
        timeout=30,
    )
    detail = response_body.decode("utf-8", errors="replace")
    if status < 200 or status >= 300:
        raise GrokBoxError(f"在 Box 内创建 relay agent 失败：HTTP {status} {detail[:400]}")
    try:
        record = json.loads(detail)
    except Exception as exc:
        raise GrokBoxError("Box 网关 createAgent 返回格式无效") from exc
    agent_id = _find_agent_id(record)
    if not agent_id:
        raise GrokBoxError(f"Box 网关 createAgent 未返回 agent id：{detail[:300]}")
    return agent_id


def _send_provision_prompt(descriptor: Mapping[str, object], agent_id: str) -> None:
    from urllib.request import Request, urlopen

    # Box 协调器要求先挂着一个 /events 订阅才接受 /api/sendPrompt（否则 500），
    # 与桌面端的时序一致：先开 SSE，POST 期间保持不关。
    events_resp = None
    try:
        try:
            events_resp = urlopen(
                Request(
                    _box_url(descriptor, "/events"),
                    headers=_box_headers(descriptor, {"Accept": "text/event-stream"}),
                    method="GET",
                ),
                timeout=15,
            )
        except Exception:
            events_resp = None
        body = json.dumps(
            {
                "prompt": BOX_RELAY_PROVISION_PROMPT,
                "agentId": agent_id,
                "clientNonce": str(uuid.uuid4()),
                "source": "desktop",
                "sessionId": "",
            },
            ensure_ascii=False,
        ).encode("utf-8")
        status, _headers, response_body = _box_request(
            descriptor,
            "/api/sendPrompt",
            method="POST",
            data=body,
            headers={"Content-Type": "application/json", "x-sand-slim-avatars": "1"},
            timeout=25,
        )
    finally:
        if events_resp is not None:
            try:
                events_resp.close()
            except Exception:
                pass
    if status < 200 or status >= 300:
        detail = response_body.decode("utf-8", errors="replace")[:400]
        raise GrokBoxError(f"向 Grok Bot Box Agent 发送初始化指令失败：HTTP {status} {detail}")
    try:
        response = json.loads(response_body.decode("utf-8"))
    except Exception as exc:
        raise GrokBoxError("Grok Bot Box Agent 返回格式无效") from exc
    if not isinstance(response, dict) or response.get("accepted") is not True:
        raise GrokBoxError("Grok Bot Box Agent 未接受初始化指令")


def _load_provision_state() -> Dict[str, object]:
    try:
        value = json.loads(_provision_state_path().read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _save_provision_state(state: Mapping[str, object]) -> None:
    try:
        _write_json_atomic(_provision_state_path(), dict(state))
    except Exception:
        pass


def ensure_relay_route(
    descriptor: Mapping[str, object],
    log: Logger = None,
    wait_seconds: float = 240,
) -> str:
    """确认 Box 上的 relay 路由可用；没有就让 Box 内 Agent 装，然后轮询。

    返回 "already-installed" / "installed" / "provisioning"（Box 仍在后台改写，未等到 200）。
    """

    def emit(message: str) -> None:
        if log is not None:
            log(message)

    emit("探测 Box relay 路由 ...")
    status, content_type, relay_ok = probe_relay(descriptor)
    emit(f"relay 探测：HTTP {status}，Content-Type={content_type or 'unknown'}")
    if relay_ok:
        return "already-installed"
    if status in (401, 403):
        raise GrokBoxError("Box gateway 拒绝鉴权（描述符 token 失效）；请确认 Cursor 已登录且账号有 Grok Bot 资格后重试")
    if status == 200:
        raise GrokBoxError(
            f"Box gateway 返回 200 但不是 Connect 流，relay 尚未正确挂载；Content-Type={content_type or 'unknown'}"
        )
    if status != 404:
        raise GrokBoxError(f"无法确认 Box relay 状态：HTTP {status}，Content-Type={content_type or 'unknown'}")

    # 同一账号复用已创建的 relay agent：重跑不再新建、不重复扣费，只继续轮询。
    account_fp = str(descriptor.get("accountFingerprint") or "")
    state = _load_provision_state()
    entry = state.get(account_fp) if isinstance(state.get(account_fp), dict) else {}
    agent_id = entry.get("relayAgentId") if isinstance(entry.get("relayAgentId"), str) else ""
    last_sent_ms = entry.get("lastSentMs") if isinstance(entry.get("lastSentMs"), (int, float)) else 0
    need_send = False
    if not agent_id:
        emit("在 Box 内创建 relay agent（网关 /api/createAgent）...")
        agent_id = _create_box_agent(descriptor)
        need_send = True
    else:
        emit(f"复用已创建的 relay agent：{agent_id}")
        need_send = (time.time() * 1000 - last_sent_ms) > _RESEND_PROMPT_AFTER_MS
    if need_send:
        try:
            _send_provision_prompt(descriptor, agent_id)
        except GrokBoxError as exc:
            if "does not exist" not in str(exc):
                raise
            emit("原 relay agent 已不在 Box 内，重新创建 ...")
            agent_id = _create_box_agent(descriptor)
            _send_provision_prompt(descriptor, agent_id)
        last_sent_ms = int(time.time() * 1000)
        emit("指令已受理，Box 正在改写 host-main.cjs 并重启（可能数分钟）...")
    else:
        emit("relay agent 已在处理中，直接轮询等待其完成 ...")
    state[account_fp] = {"relayAgentId": agent_id, "lastSentMs": last_sent_ms}
    _save_provision_state(state)

    deadline = time.monotonic() + wait_seconds
    started = time.monotonic()
    while time.monotonic() < deadline:
        time.sleep(PROBE_POLL_SECONDS)
        try:
            status, _content_type, relay_ok = probe_relay(descriptor)
        except GrokBoxError:
            continue
        emit(f"等待中（{int(time.monotonic() - started)}s）relay 探测：HTTP {status}")
        if relay_ok:
            return "installed"
        if status in (401, 403):
            raise GrokBoxError("Box 初始化期间网关拒绝鉴权（token 可能过期），请确认 Cursor 登录有效后重试")
    return "provisioning"


def provision_box_relay(
    access_token: Optional[str] = None,
    machine_id: Optional[str] = None,
    log: Logger = None,
    wait_seconds: float = 240,
) -> str:
    """一键：mint 描述符 + 确认/安装 Box relay 路由。返回值同 ensure_relay_route。"""
    descriptor = mint_descriptor(access_token, machine_id, log=log)
    return ensure_relay_route(descriptor, log=log, wait_seconds=wait_seconds)
