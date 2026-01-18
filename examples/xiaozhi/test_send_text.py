import argparse
import asyncio
import json
import uuid

import websockets



def _build_headers(token: str, device_id: str | None, client_id: str | None) -> dict[str, str]:
    headers: dict[str, str] = {"Protocol-Version": "1"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if device_id:
        headers["Device-Id"] = device_id
    headers["Client-Id"] = client_id or str(uuid.uuid4())
    return headers


async def _run(
    url: str,
    text: str,
    token: str,
    device_id: str | None,
    client_id: str | None,
    timeout_s: float,
) -> int:
    hello_message = {
        "type": "hello",
        "version": 1,
        "transport": "websocket",
        "audio_params": {
            "format": "opus",
            "sample_rate": 16000,
            "channels": 1,
            "frame_duration": 60,
        },
    }

    headers = _build_headers(token=token, device_id=device_id, client_id=client_id)
    async with websockets.connect(uri=url, additional_headers=headers) as ws:
        await ws.send(json.dumps(hello_message))
        print(f"connected: {url}")

        session_id = ""
        hello_deadline = asyncio.get_running_loop().time() + min(5.0, timeout_s)
        while asyncio.get_running_loop().time() < hello_deadline:
            msg = await ws.recv()
            if isinstance(msg, (bytes, bytearray)):
                continue
            try:
                data = json.loads(msg)
            except Exception:
                continue
            if data.get("type") == "hello":
                session_id = str(data.get("session_id") or "")
                print(f"recv hello: session_id={session_id}")
                break

        await ws.send(json.dumps({"session_id": session_id, "type": "stt", "text": text}))
        print(f"sent stt: {text} (session_id={session_id or '<empty>'})")

        async def _recv_loop():
            audio_frames = 0
            audio_bytes = 0
            async for message in ws:
                if isinstance(message, (bytes, bytearray)):
                    audio_frames += 1
                    audio_bytes += len(message)
                    if audio_frames == 1 or audio_frames % 50 == 0:
                        print(f"recv audio: frames={audio_frames} bytes={audio_bytes}")
                    continue

                try:
                    data = json.loads(message)
                except Exception:
                    print(f"recv text: {message}")
                    continue

                msg_type = data.get("type")
                if msg_type == "tts":
                    print(f"recv tts: state={data.get('state')} text={data.get('text','')}")
                else:
                    print(f"recv json: {data}")

        try:
            await asyncio.wait_for(_recv_loop(), timeout=timeout_s)
        except asyncio.TimeoutError:
            print(f"timeout after {timeout_s}s")
            return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Send fixed STT text to XiaoZhi server (no config.py)")
    parser.add_argument("--url", type=str, required=True, help="e.g. ws://127.0.0.1:8001/xiaozhi/v1/")
    parser.add_argument("--token", type=str, default="")
    parser.add_argument("--device-id", type=str, default="")
    parser.add_argument("--client-id", type=str, default="")
    parser.add_argument("--text", type=str, default="你好小智，做一个连通性测试")
    parser.add_argument("--timeout", type=float, default=15.0)
    args = parser.parse_args()
    return asyncio.run(
        _run(
            url=args.url,
            text=args.text,
            token=args.token,
            device_id=args.device_id or None,
            client_id=args.client_id or None,
            timeout_s=args.timeout,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
