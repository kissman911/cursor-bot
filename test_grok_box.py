"""grok_box 纯函数测试（不联网、不写用户目录）。Run: python test_grok_box.py"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

import grok_box
from grok_box import (
    BOX_RELAY_PATH,
    GrokBoxError,
    account_fingerprint,
    build_descriptor,
    connect_stream_has_end_frame,
    cursor_checksum,
    parse_ensure_reply,
    pb_bool,
    pb_iter_fields,
)


def _jwt(payload: dict) -> str:
    def seg(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    return f"{seg({'alg': 'none'})}.{seg(payload)}.sig"


def _pb_str(field: int, value: str) -> bytes:
    data = value.encode("utf-8")
    return grok_box._pb_varint((field << 3) | 2) + grok_box._pb_varint(len(data)) + data


def test_pb_bool_wake_bytes() -> None:
    # EnsureSandBoxRequest 字段 2 = true，线上 bytes 0x10 0x01（与注入 JS 的 Buffer.from([16,1]) 一致）。
    assert pb_bool(2, True) == b"\x10\x01"
    assert pb_bool(2, False) == b"\x10\x00"


def test_parse_ensure_reply_fields() -> None:
    body = (
        _pb_str(1, "cluster-a")
        + _pb_str(4, "nto-network")
        + _pb_str(10, "https://box.example.cursorvm.com")
        + _pb_str(11, "gateway-token")
        + grok_box._pb_varint((13 << 3) | 0)
        + grok_box._pb_varint(3)
        # 未知 fixed64 / fixed32 字段要能跳过。
        + grok_box._pb_varint((20 << 3) | 1)
        + b"\x00" * 8
        + grok_box._pb_varint((21 << 3) | 5)
        + b"\x00" * 4
    )
    reply = parse_ensure_reply(body)
    assert reply == {
        "baseUrl": "https://box.example.cursorvm.com",
        "token": "gateway-token",
        "networkToken": "nto-network",
        "runState": 3,
    }
    fields = list(pb_iter_fields(body))
    assert [f for f, _w, _v in fields] == [1, 4, 10, 11, 13, 20, 21]


def test_parse_ensure_reply_truncated() -> None:
    try:
        parse_ensure_reply(_pb_str(10, "https://x")[:-3])
    except GrokBoxError:
        pass
    else:
        raise AssertionError("expected GrokBoxError")


def test_account_fingerprint_uses_jwt_sub() -> None:
    token = _jwt({"sub": "auth0|user_01TEST", "exp": 4_000_000_000})
    scope = hashlib.sha256(b"auth0|user_01TEST").hexdigest()
    assert account_fingerprint(token) == hashlib.sha256(scope.encode()).hexdigest()[:16]
    # 非 JWT 按整段 token 算，也是 16 hex。
    assert len(account_fingerprint("not-a-jwt")) == 16


def test_cursor_checksum_shape_and_determinism() -> None:
    now_ms = 1_757_400_000_000
    a = cursor_checksum("", now_ms=now_ms)
    b = cursor_checksum("machine-x", now_ms=now_ms)
    assert len(a) == 8 and b == a + "machine-x"
    # 同一分钟内（epoch = ms // 1e6 约 16.7 分钟粒度）结果稳定，跨粒度会变。
    assert cursor_checksum("", now_ms=now_ms + 1000) == a
    assert cursor_checksum("", now_ms=now_ms + 1_000_000) != a


def test_cursor_checksum_matches_injected_js() -> None:
    """Python 端与注入 Cursor 的 __sandSum（自刷新用）逐位一致；没有 node 就跳过。"""
    node = shutil.which("node")
    if not node:
        print("skip: test_cursor_checksum_matches_injected_js (no node)")
        return
    now_ms = 1_757_400_000_000
    script = (
        "const __sandSum=(m,now)=>{const ep=Math.floor(now/1e6),"
        "b=new Uint8Array([ep>>40&255,ep>>32&255,ep>>24&255,ep>>16&255,ep>>8&255,255&ep]);"
        "let pv=165;for(let i=0;i<b.length;i++)b[i]=(b[i]^pv)+i%256&255,pv=b[i];"
        'return Buffer.from(b).toString("base64url")+String(m||"")};'
        f'process.stdout.write(__sandSum("mid", {now_ms}))'
    )
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, check=True).stdout
    assert out == cursor_checksum("mid", now_ms=now_ms), (out, cursor_checksum("mid", now_ms=now_ms))


def test_build_descriptor_contract() -> None:
    token = _jwt({"sub": "auth0|user_01TEST"})
    reply = {
        "baseUrl": "https://box.example.cursorvm.com",
        "token": "gateway-token",
        "networkToken": "nto-network",
        "runState": 3,
    }
    desc = build_descriptor(reply, token, "machine-x")
    assert desc["version"] == 1
    assert desc["baseUrl"] == reply["baseUrl"] and desc["token"] == "gateway-token"
    assert desc["headers"] == {"x-anyrun-network-token": "nto-network"}
    assert desc["relayPath"] == BOX_RELAY_PATH == "/sand-stream-relay/aiserver.v1.InferenceService/Stream"
    assert desc["accountFingerprint"] == account_fingerprint(token)
    assert desc["refreshAfterMs"] == 3_600_000 and desc["mintedAtMs"] > 0
    assert desc["refresh"] == {
        "backendUrl": grok_box.backend_url(),
        "accessToken": token,
        "machineId": "machine-x",
    }
    # 没有 network token 时 headers 为空对象而不是缺字段（注入 JS 用 Object.entries 遍历）。
    assert build_descriptor({**reply, "networkToken": ""}, token, "")["headers"] == {}


def test_connect_stream_end_frame() -> None:
    assert connect_stream_has_end_frame(b"\x02\x00\x00\x00\x00")
    assert connect_stream_has_end_frame(b"\x00\x00\x00\x00\x02ab" + b"\x02\x00\x00\x00\x00")
    assert not connect_stream_has_end_frame(b"\x00\x00\x00\x00\x00")
    assert not connect_stream_has_end_frame(b"<html>not a connect stream</html>")
    assert not connect_stream_has_end_frame(b"\x02\x00\x00\x00\x09short")


def test_box_url_keeps_base_prefix_and_rejects_http() -> None:
    desc = {"baseUrl": "https://gw.example.com/prefix/", "token": "t", "headers": {}}
    assert grok_box._box_url(desc, "/events") == "https://gw.example.com/prefix/events"
    assert grok_box._box_url(desc, BOX_RELAY_PATH) == "https://gw.example.com/prefix" + BOX_RELAY_PATH
    try:
        grok_box._box_url({"baseUrl": "http://gw.example.com", "token": "t"}, "/events")
    except GrokBoxError:
        pass
    else:
        raise AssertionError("expected GrokBoxError")


def test_box_headers_passthrough_and_reserved() -> None:
    desc = {
        "baseUrl": "https://gw.example.com",
        "token": "box-token",
        "headers": {"x-anyrun-network-token": "nto", "Authorization": "Bearer evil", "host": "x", "": "y"},
    }
    headers = grok_box._box_headers(desc, {"Accept": "text/event-stream"})
    assert headers["Authorization"] == "Bearer box-token"
    assert headers["x-anyrun-network-token"] == "nto"
    assert "host" not in headers and "" not in headers
    assert headers["Accept"] == "text/event-stream"


def test_descriptor_status_with_env_override() -> None:
    token = _jwt({"sub": "auth0|user_01TEST"})
    other = _jwt({"sub": "auth0|user_01OTHER"})
    tmp = tempfile.mkdtemp(prefix="sand-relay-test-")
    path = os.path.join(tmp, "grok-box-relay.json")
    old = os.environ.get(grok_box.RELAY_CONFIG_ENV)
    os.environ[grok_box.RELAY_CONFIG_ENV] = path
    try:
        assert grok_box.relay_config_path() == grok_box.Path(path)
        assert grok_box.load_descriptor() is None
        assert grok_box.descriptor_status(token)["present"] is False

        desc = build_descriptor(
            {"baseUrl": "https://gw.example.com", "token": "box-token", "networkToken": "", "runState": 3},
            token,
            "",
        )
        grok_box._write_json_atomic(grok_box.Path(path), desc)
        if os.name != "nt":
            assert (os.stat(path).st_mode & 0o777) == 0o600
        info = grok_box.descriptor_status(token)
        assert info["present"] and info["valid"] and info["hasRefresh"]
        assert info["accountMatches"] is True and info["ageMinutes"] == 0
        assert grok_box.descriptor_status(other)["accountMatches"] is False

        with open(path, "w", encoding="utf-8") as handle:
            handle.write('{"baseUrl": "http://insecure", "token": "x"}')
        bad = grok_box.descriptor_status(token)
        assert bad["present"] and not bad["valid"] and "https" in bad["error"]

        assert grok_box.remove_descriptor() is True
        assert not os.path.exists(path)
    finally:
        if old is None:
            os.environ.pop(grok_box.RELAY_CONFIG_ENV, None)
        else:
            os.environ[grok_box.RELAY_CONFIG_ENV] = old
        shutil.rmtree(tmp, ignore_errors=True)


def test_control_headers() -> None:
    headers = grok_box.control_headers("tok", "mid")
    assert headers["Authorization"] == "Bearer tok"
    assert headers["x-cursor-client-type"] == "sand"
    assert headers["x-cursor-client-version"] == "0.44.0"
    assert headers["x-sand-box-namespace"] == "prod"
    assert headers["x-ghost-mode"] == "true"
    assert headers["Content-Type"] == "application/proto"
    assert headers["Connect-Protocol-Version"] == "1"
    assert headers["x-cursor-checksum"].endswith("mid")
    assert len(headers["x-request-id"]) == 36


def main() -> int:
    tests = [
        test_pb_bool_wake_bytes,
        test_parse_ensure_reply_fields,
        test_parse_ensure_reply_truncated,
        test_account_fingerprint_uses_jwt_sub,
        test_cursor_checksum_shape_and_determinism,
        test_cursor_checksum_matches_injected_js,
        test_build_descriptor_contract,
        test_connect_stream_end_frame,
        test_box_url_keeps_base_prefix_and_rejects_http,
        test_box_headers_passthrough_and_reserved,
        test_descriptor_status_with_env_override,
        test_control_headers,
    ]
    for test in tests:
        test()
        print("ok:", test.__name__)
    print("all passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
