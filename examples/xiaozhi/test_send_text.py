import asyncio, json, uuid, wave
import websockets
import opuslib

WS_URL = "ws://192.168.7.127:8001/xiaozhi/v1/"   # 改成你的服务端
TOKEN  = ""                        # auth 关着一般也可随便填；开了就填正确 token
DEVICE_ID = "11:22:33:44:55:66"              # 随便写个像 MAC 的即可
CLIENT_ID = str(uuid.uuid4())

def frame_size(sample_rate: int, frame_duration_ms: int) -> int:
    # Opus 解码需要指定一帧有多少采样点
    return int(sample_rate * frame_duration_ms / 1000)

async def main(text: str, out_wav: str = "out.wav"):
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Protocol-Version": "1",
        "Client-Id": CLIENT_ID,
        "Device-Id": DEVICE_ID,
    }

    async with websockets.connect(WS_URL, additional_headers=headers, ping_interval=None) as ws:
        # 1) hello（先用你本地配置的默认值发，服务端会回它实际用的参数）
        hello = {
            "type": "hello",
            "version": 1,
            "transport": "websocket",
            "audio_params": {
                "format": "opus",
                "sample_rate": 16000,
                "channels": 1,
                "frame_duration": 60
            }
        }
        await ws.send(json.dumps(hello, ensure_ascii=False))

        # 2) 等服务端 hello，拿 session_id + audio_params
        session_id = None
        sr = 16000
        ch = 1
        fd = 60

        while True:
            msg = await ws.recv()
            if isinstance(msg, (bytes, bytearray)):
                continue
            data = json.loads(msg)
            if data.get("type") == "hello":
                session_id = data["session_id"]
                ap = data.get("audio_params", {})
                sr = int(ap.get("sample_rate", sr))
                ch = int(ap.get("channels", ch))
                fd = int(ap.get("frame_duration", fd))
                break

        dec = opuslib.Decoder(sr, ch)
        fs = frame_size(sr, fd)

        # 发送“文本输入”（用 listen/detect，而不是 stt）
        await ws.send(json.dumps({
            "type": "listen",
            "state": "detect",
            "text": text,
            "source": "text",
            "session_id": session_id
        }, ensure_ascii=False))


        # 4) 收 tts 音频 binary 帧，解码写 wav，直到 tts stop
        wf = wave.open(out_wav, "wb")
        wf.setnchannels(ch)
        wf.setsampwidth(2)      # 16-bit PCM
        wf.setframerate(sr)

        in_tts = False
        while True:
            msg = await ws.recv()

            if isinstance(msg, (bytes, bytearray)):
                if in_tts:
                    pcm = dec.decode(msg, fs, decode_fec=False)
                    wf.writeframes(pcm)
                continue

            data = json.loads(msg)
            t = data.get("type")

            if t == "tts":
                state = data.get("state")
                if state in ("start", "sentence_start"):
                    in_tts = True
                elif state == "stop":
                    break

        wf.close()
        print(f"Saved: {out_wav}")

if __name__ == "__main__":
    asyncio.run(main("你好，给我一句日语自我介绍，并用可爱的语气。", "reply.wav"))
