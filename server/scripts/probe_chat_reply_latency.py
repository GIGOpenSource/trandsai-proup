#!/usr/bin/env python3
"""
探测一轮对话回复周期时长（WebSocket）。

指标（每轮）：
  - t_typing_s   : send → 首个 typing
  - ttfr_s       : send → 首条 assistant message（用户可见首气泡）
  - t_full_s     : send → 本轮最后一条 assistant message（静默 settle 后判定结束）
  - bubbles      : 本轮助手气泡数
  - gap_s[]      : 气泡间隔

用法：
  # 已有 token + companion
  python scripts/probe_chat_reply_latency.py \\
    --base-url http://127.0.0.1:8000 \\
    --token USER_TOKEN \\
    --companion-id COMPANION_ID \\
    --rounds 3

  # 自动注册临时用户并创建 companion
  python scripts/probe_chat_reply_latency.py \\
    --base-url http://127.0.0.1:8000 \\
    --auto-setup \\
    --rounds 3
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
import uuid
from typing import Any, Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

try:
    import websockets
except ImportError:
    raise SystemExit("请先安装: pip install websockets")


def _http_json(method: str, url: str, body: Optional[dict] = None, token: Optional[str] = None) -> Any:
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["x-token"] = token
    req = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else None
    except HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code} {url}: {detail}") from e
    except URLError as e:
        raise RuntimeError(f"连接失败 {url}: {e}") from e


def auto_setup(base: str) -> tuple[str, str]:
    uname = f"latprobe_{uuid.uuid4().hex[:10]}"
    password = "probe123456"
    print(f"[setup] register user={uname}")
    reg = _http_json(
        "POST",
        f"{base}/api/auth/register",
        {
            "username": uname,
            "password": password,
            "nickname": uname,
            "gender": "男",
        },
    )
    token = reg["token"]
    profile = {
        "name": "测延时",
        "age": 24,
        "gender": "女",
        "city": "上海",
        "personality": "温柔体贴，说话简洁",
        "mbti": "INFJ",
        "sexual_orientation": "heterosexual",
        "speech_style": "口语短句，轻松自然",
        "hobbies": "看电影、散步",
        "background": "普通上班族，平时喜欢聊天",
        "avatar_url": "",
        "language": "zh",
    }
    print("[setup] create companion…")
    companion = _http_json("POST", f"{base}/companions", profile, token=token)
    cid = companion.get("id") or companion.get("profile", {}).get("id")
    if not cid:
        raise RuntimeError(f"create companion 无 id: {companion}")
    print(f"[setup] companion_id={cid}")
    return token, cid


def to_ws_base(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.startswith("https://"):
        return "wss://" + base[len("https://") :]
    if base.startswith("http://"):
        return "ws://" + base[len("http://") :]
    return base


async def one_round(
    ws,
    text: str,
    timeout: float,
    settle: float,
) -> Dict[str, Any]:
    """发一条消息并收集本轮周期指标。"""
    payload = {
        "text": text,
        "lang": "zh",
        "user_gender": "",
        "tz": "Asia/Shanghai",
        "tz_offset": -480,
    }
    t0 = time.perf_counter()
    await ws.send(json.dumps(payload, ensure_ascii=False))

    t_typing: Optional[float] = None
    bubble_ts: List[float] = []
    bubble_texts: List[str] = []
    err: Optional[str] = None
    deadline = t0 + timeout
    last_event = t0

    while time.perf_counter() < deadline:
        # 已有气泡后，静默 settle 秒则认为本轮结束
        if bubble_ts and (time.perf_counter() - last_event) >= settle:
            break
        remaining = deadline - time.perf_counter()
        wait = min(remaining, settle if bubble_ts else 5.0)
        if wait <= 0:
            break
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=wait)
        except asyncio.TimeoutError:
            if bubble_ts:
                break
            continue
        last_event = time.perf_counter()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        typ = data.get("type")
        if typ == "typing" and t_typing is None:
            t_typing = last_event - t0
        elif typ == "message" and data.get("role") == "assistant":
            bubble_ts.append(last_event - t0)
            bubble_texts.append((data.get("text") or "")[:80])
        elif typ == "error":
            err = str(data.get("text") or data)
            break

    if not bubble_ts:
        return {
            "ok": False,
            "error": err or "timeout_no_reply",
            "t_typing_s": t_typing,
            "ttfr_s": None,
            "t_full_s": None,
            "bubbles": 0,
            "gaps_s": [],
            "preview": [],
        }

    gaps = [bubble_ts[i] - bubble_ts[i - 1] for i in range(1, len(bubble_ts))]
    return {
        "ok": True,
        "error": err,
        "t_typing_s": t_typing,
        "ttfr_s": bubble_ts[0],
        "t_full_s": bubble_ts[-1],
        "bubbles": len(bubble_ts),
        "gaps_s": gaps,
        "preview": bubble_texts,
    }


def _pct(sorted_vals: List[float], p: float) -> float:
    if not sorted_vals:
        return float("nan")
    idx = max(0, min(len(sorted_vals) - 1, int(round((p / 100.0) * (len(sorted_vals) - 1)))))
    return sorted_vals[idx]


async def run(args: argparse.Namespace) -> int:
    base = args.base_url.rstrip("/")
    token = args.token
    companion_id = args.companion_id

    if args.auto_setup:
        token, companion_id = auto_setup(base)
    if not token or not companion_id:
        print("需要 --token + --companion-id，或使用 --auto-setup", file=sys.stderr)
        return 2

    # 探活
    try:
        health = _http_json("GET", f"{base}/api/health")
        print(f"[health] {json.dumps(health, ensure_ascii=False)[:200]}")
    except Exception as e:
        print(f"[warn] health 失败: {e}")

    ws_url = f"{to_ws_base(base)}/ws/chat/{companion_id}?lang=zh&token={token}"
    print(
        f"Probe: rounds={args.rounds} timeout={args.timeout}s settle={args.settle}s "
        f"companion={companion_id}"
    )

    results: List[Dict[str, Any]] = []
    async with websockets.connect(ws_url, open_timeout=20, max_size=2**22) as ws:
        # 丢弃握手后可能推来的历史/状态帧（短窗口）
        drain_until = time.perf_counter() + 1.5
        while time.perf_counter() < drain_until:
            try:
                await asyncio.wait_for(ws.recv(), timeout=drain_until - time.perf_counter())
            except asyncio.TimeoutError:
                break

        for i in range(args.rounds):
            text = args.text if args.rounds == 1 else f"{args.text}（第{i + 1}轮 {time.strftime('%H:%M:%S')}）"
            print(f"\n--- round {i + 1}/{args.rounds} ---")
            r = await one_round(ws, text, args.timeout, args.settle)
            results.append(r)
            if r["ok"]:
                print(
                    f"ttfr={r['ttfr_s']:.2f}s  full={r['t_full_s']:.2f}s  "
                    f"typing={r['t_typing_s']:.2f}s  bubbles={r['bubbles']}"
                    if r["t_typing_s"] is not None
                    else f"ttfr={r['ttfr_s']:.2f}s  full={r['t_full_s']:.2f}s  "
                    f"typing=n/a  bubbles={r['bubbles']}"
                )
                if r["gaps_s"]:
                    print(f"bubble_gaps_s={[round(g, 3) for g in r['gaps_s']]}")
                for j, prev in enumerate(r["preview"], 1):
                    print(f"  [{j}] {prev}")
            else:
                print(f"FAIL: {r['error']}")
            if i + 1 < args.rounds:
                await asyncio.sleep(args.pause)

    ok_rows = [r for r in results if r["ok"]]
    print("\n======== summary ========")
    print(f"ok={len(ok_rows)}/{len(results)}")
    if ok_rows:
        for key, label in (
            ("ttfr_s", "TTFR (send→first bubble)"),
            ("t_full_s", "FULL (send→last bubble)"),
            ("t_typing_s", "typing (send→first typing)"),
        ):
            vals = sorted(r[key] for r in ok_rows if r.get(key) is not None)
            if not vals:
                print(f"{label}: n/a")
                continue
            mean = statistics.mean(vals)
            print(
                f"{label}: mean={mean:.2f}s  p50={_pct(vals, 50):.2f}s  "
                f"p95={_pct(vals, 95):.2f}s  min={vals[0]:.2f}s  max={vals[-1]:.2f}s"
            )
        bubbles = [r["bubbles"] for r in ok_rows]
        print(f"bubbles/round: mean={statistics.mean(bubbles):.1f}  min={min(bubbles)}  max={max(bubbles)}")
    return 0 if ok_rows else 1


def main() -> None:
    p = argparse.ArgumentParser(description="Probe chat reply cycle latency")
    p.add_argument("--base-url", default="http://127.0.0.1:8000")
    p.add_argument("--token", default="")
    p.add_argument("--companion-id", default="")
    p.add_argument("--auto-setup", action="store_true", help="注册临时用户并创建 companion")
    p.add_argument("--rounds", type=int, default=3)
    p.add_argument("--timeout", type=float, default=180.0, help="单轮最大等待秒")
    p.add_argument("--settle", type=float, default=2.5, help="末气泡后静默多久视为本轮结束")
    p.add_argument("--pause", type=float, default=1.0, help="轮间暂停秒")
    p.add_argument("--text", default="你好，随便聊两句就行，今天过得怎么样？")
    args = p.parse_args()
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
