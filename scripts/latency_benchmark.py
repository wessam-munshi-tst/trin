#!/usr/bin/env python3
"""Measure chatbot first-audio latency using the existing admin speak endpoint.

This benchmark:
1. Opens a client WebSocket session.
2. Sends POST /admin/speak.
3. Measures time until first binary audio chunk arrives.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib import error, parse, request

import websockets


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Latency benchmark for first audio byte")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="Base URL, e.g. http://127.0.0.1:8000")
    parser.add_argument("--tenant", default="", help="Tenant id (optional)")
    parser.add_argument("--iterations", type=int, default=5, help="Measured runs")
    parser.add_argument("--warmup", type=int, default=1, help="Warmup runs (not included in summary)")
    parser.add_argument("--audio-timeout", type=float, default=30.0, help="Seconds to wait for first audio")
    parser.add_argument("--http-timeout", type=float, default=10.0, help="HTTP timeout in seconds")
    parser.add_argument("--text", default="Latency benchmark ping.", help="Text sent to /admin/speak")
    parser.add_argument("--output-json", default="", help="Optional JSON output file path")
    return parser.parse_args()


def build_urls(base_url: str, tenant: str) -> tuple[str, str]:
    parsed = parse.urlparse(base_url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("base-url must start with http:// or https://")
    if not parsed.netloc:
        raise ValueError("base-url must include host and port")

    tenant_prefix = f"/{tenant.strip('/')}" if tenant else ""
    ws_scheme = "wss" if parsed.scheme == "https" else "ws"

    ws_url = f"{ws_scheme}://{parsed.netloc}{tenant_prefix}/ws/audio"
    speak_url = f"{parsed.scheme}://{parsed.netloc}{tenant_prefix}/admin/speak"
    return ws_url, speak_url


def post_admin_speak(url: str, text: str, timeout: float) -> tuple[int, str]:
    payload = json.dumps({"text": text}).encode("utf-8")
    req = request.Request(
        url,
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return resp.status, body
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return exc.code, body


def percentile(sorted_values: list[float], p: float) -> float:
    if not sorted_values:
        return 0.0
    idx = max(0, min(len(sorted_values) - 1, math.ceil(p * len(sorted_values)) - 1))
    return sorted_values[idx]


async def benchmark_once(ws_url: str, speak_url: str, text: str, audio_timeout: float, http_timeout: float) -> float:
    async with websockets.connect(ws_url, max_size=None) as websocket:
        t0 = time.perf_counter()
        status, body = await asyncio.to_thread(post_admin_speak, speak_url, text, http_timeout)
        if status != 200:
            raise RuntimeError(f"POST /admin/speak failed with {status}: {body}")

        deadline = time.perf_counter() + audio_timeout
        while True:
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                raise TimeoutError(f"Timed out waiting {audio_timeout}s for first audio")

            message = await asyncio.wait_for(websocket.recv(), timeout=remaining)
            if isinstance(message, (bytes, bytearray)):
                return (time.perf_counter() - t0) * 1000.0


async def run() -> int:
    args = parse_args()
    ws_url, speak_url = build_urls(args.base_url, args.tenant)

    print(f"WebSocket: {ws_url}")
    print(f"Speak API: {speak_url}")
    print(f"Warmup: {args.warmup}, Iterations: {args.iterations}")

    # Warmup runs
    for i in range(args.warmup):
        try:
            latency_ms = await benchmark_once(ws_url, speak_url, args.text, args.audio_timeout, args.http_timeout)
            print(f"warmup {i + 1}: {latency_ms:.1f} ms")
        except Exception as exc:
            print(f"warmup {i + 1}: FAILED ({exc})")

    values: list[float] = []
    for i in range(args.iterations):
        try:
            latency_ms = await benchmark_once(ws_url, speak_url, args.text, args.audio_timeout, args.http_timeout)
            values.append(latency_ms)
            print(f"run {i + 1}: {latency_ms:.1f} ms")
        except Exception as exc:
            print(f"run {i + 1}: FAILED ({exc})")

    if not values:
        print("No successful runs.")
        return 1

    ordered = sorted(values)
    summary = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "base_url": args.base_url,
        "tenant": args.tenant,
        "iterations_requested": args.iterations,
        "iterations_succeeded": len(values),
        "first_audio_latency_ms": {
            "min": round(min(values), 2),
            "max": round(max(values), 2),
            "avg": round(statistics.fmean(values), 2),
            "p50": round(percentile(ordered, 0.50), 2),
            "p95": round(percentile(ordered, 0.95), 2),
        },
        "runs_ms": [round(v, 2) for v in values],
    }

    print("\nSummary")
    print(json.dumps(summary, indent=2))

    if args.output_json:
        output_path = Path(args.output_json)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(f"Saved summary to {output_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
