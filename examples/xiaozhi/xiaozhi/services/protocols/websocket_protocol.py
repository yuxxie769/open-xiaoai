import asyncio
import json
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import requests
import websockets

from xiaozhi.ref import get_xiaozhi
from xiaozhi.services.protocols.protocol import Protocol
from xiaozhi.services.protocols.typing import DeviceState
from xiaozhi.utils.config import ConfigManager


class WebsocketProtocol(Protocol): 
    def __init__(self):
        super().__init__()
        # 获取配置管理器实例
        self.config = ConfigManager.instance()
        self.websocket = None
        self.server_sample_rate = 24000
        self.server_frame_duration = 60
        self.server_frame_size = int(
            self.server_sample_rate * (self.server_frame_duration / 1000)
        )
        self.connected = False
        self.hello_received = None  # 初始化时先设为 None
        self.WEBSOCKET_URL = self.config.get_config("NETWORK.WEBSOCKET_URL")
        self.WEBSOCKET_ACCESS_TOKEN = self.config.get_config(
            "NETWORK.WEBSOCKET_ACCESS_TOKEN"
        )
        self.VERBOSE_LOG = bool(self.config.get_config("NETWORK.VERBOSE_LOG", False))
        self.CLIENT_ID = self.config.get_client_id()
        self.DEVICE_ID = self.config.get_device_id()
        self._ota_websocket_info: dict | None = None

    def _log_info(self, message: str):
        if self.VERBOSE_LOG and message:
            print(message)

    def _mask_token(self, token: str) -> str:
        if not token:
            return ""
        if len(token) <= 12:
            return "***"
        return f"{token[:6]}***{token[-4:]}"

    def _to_bearer(self, token: str) -> str:
        token = (token or "").strip()
        if not token:
            return ""
        return token if token.startswith("Bearer ") else f"Bearer {token}"

    def _append_query(self, url: str, params: dict[str, str]) -> str:
        if not url:
            return url
        parts = urlsplit(url)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        for key, value in params.items():
            if value is None or value == "":
                continue
            # 覆盖已有值，确保鉴权/设备信息一致
            query[key] = value
        # 使用 quote 编码，保持空格为 %20（与浏览器 URLSearchParams 更一致）
        new_query = urlencode(query, doseq=True, quote_via=quote)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, parts.fragment))

    def _fetch_websocket_info_from_ota(self) -> dict:
        """对齐 test/ 的逻辑：先请求 OTA，再从响应里取 websocket.url/token。"""
        if self._ota_websocket_info is not None:
            return self._ota_websocket_info

        ota_url = self.config.get_config("NETWORK.OTA_URL")
        if not ota_url:
            raise ValueError("OTA_URL 为空，无法从 OTA 获取 WebSocket 信息")

        device_id = self.DEVICE_ID
        client_id = self.CLIENT_ID

        headers = {
            "Content-Type": "application/json",
            "Device-Id": device_id or "",
            "Client-Id": client_id or "",
        }

        payload = {
            "version": 0,
            "uuid": "",
            "application": {
                "name": "open-xiaoai-xiaozhi",
                "version": "1.0.0",
                "compile_time": "",
                "idf_version": "",
                "elf_sha256": "",
            },
            "ota": {"label": "open-xiaoai-xiaozhi"},
            "board": {
                "type": "open-xiaoai",
                "ssid": "",
                "rssi": 0,
                "channel": 0,
                "ip": self.config.get_local_ip(),
                "mac": device_id,
            },
            "flash_size": 0,
            "minimum_free_heap_size": 0,
            "mac_address": device_id,
            "chip_model_name": "",
            "chip_info": {"model": 0, "cores": 0, "revision": 0, "features": 0},
            "partition_table": [],
        }

        self._log_info(f"🌐 请求 OTA(WebSocket 信息)：{ota_url} (device_id={device_id})")
        resp = requests.post(ota_url, headers=headers, json=payload, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        websocket_info = None
        if isinstance(data, dict):
            websocket_info = data.get("websocket")
            if websocket_info is None and isinstance(data.get("data"), dict):
                websocket_info = data["data"].get("websocket")
            if websocket_info is None and isinstance(data.get("result"), dict):
                websocket_info = data["result"].get("websocket")

        if not isinstance(websocket_info, dict) or not websocket_info.get("url"):
            raise ValueError(f"OTA 响应缺少 websocket.url，response={str(data)[:500]}")

        self._ota_websocket_info = websocket_info
        return websocket_info

    async def _close_websocket(self):
        if self.websocket:
            try:
                await self.websocket.close()
                self.websocket = None
                self.connected = False
            except Exception:
                pass
    
    # 用于建立与WebSocket服务器的认证连接，并完成hello握手流程，确认业务层能正常通话。
    async def connect(self) -> bool:
        """连接到WebSocket服务器"""
        try:
            # 步骤1：清理旧连接（避免残留连接导致冲突）
            await self._close_websocket()

            # 步骤2：创建「等待服务端hello返回」的事件对象
            # asyncio.Event()相当于信号灯，wait()时开始激活“等待”信号并且程序等待在当前行不再继续执行，通过is_set()查询当前信号，set()会转为“通过”信号并继续执行后续代码
            # 作用：当发送hello信息后，使用该信号灯异步暂停当前协程，等确认服务端正常返回并信号标记为“通过”后，再放行
            self.hello_received = asyncio.Event()

            # 步骤3：读取基础配置（静态 WS URL/OTA地址）
            ws_url = self.WEBSOCKET_URL
            ota_url = self.config.get_config("NETWORK.OTA_URL")

            # 步骤4：准备认证token（优先用OTA动态获取，兜底用静态配置）
            token = str(self.WEBSOCKET_ACCESS_TOKEN or "")
            if ota_url:
                # 优先使用 OTA 返回的 websocket.url/token
                try:
                    ws_info = self._fetch_websocket_info_from_ota() # 利用ota url请求当前最新的ws url和token
                    ws_url = ws_info.get("url") or ws_url
                    if not token:
                        token = str(ws_info.get("token") or "") # 如果静态token为空，用OTA返回的token
                        if token:
                            self._log_info(f"🔑 使用 OTA 返回的 token：{self._mask_token(token)}")
                except Exception as e:
                    print(f"⚠️ 从 OTA 获取 WebSocket 信息失败：{e}")

            # 步骤5：格式化token为标准的“Bearer”格式（符合HTTP认证规范）
            # 比如 token是“123456” → 变成“Bearer 123456”
            authorization = self._to_bearer(token)

            # 步骤6：给WebSocket URL追加认证/设备参数（服务端识别设备的关键）
            # 追加 query（浏览器 test_page 的连接方式）
            ws_url = self._append_query(
                ws_url,
                {
                    "authorization": authorization,
                    "device-id": self.DEVICE_ID or "",
                    "client-id": self.CLIENT_ID or "",
                },
            )

            # 步骤7：构造WebSocket连接的请求头（服务端二次校验）
            headers = {
                "Protocol-Version": "1",
                "Device-Id": self.DEVICE_ID,  # 获取设备MAC地址
                "Client-Id": self.CLIENT_ID,
            }
            # 关键：服务端可能在“有 device-id header 时”不会从 query 里提取 authorization，
            # 所以这里即便 query ws_url 里带了 authorization，我们把认证信息也放到请求头里
            if authorization:
                headers["Authorization"] = authorization

            self._log_info(f"🌐 连接 WebSocket：{ws_url} (device_id={self.DEVICE_ID})")

            # 步骤9：建立WebSocket连接
            self.websocket = await websockets.connect(
                uri=ws_url, additional_headers=headers
            )

            # 步骤10：启动“消息监听协程”（后台持续接收服务端消息）
            asyncio.create_task(self._message_handler())

            # 步骤11：发送客户端“hello”消息
            hello_message = {
                "type": "hello",
                "version": 1,
                "transport": "websocket",
                # 对齐 test/：增加 device 信息（服务端可能要求）
                "device_id": self.DEVICE_ID,
                "device_mac": self.DEVICE_ID,
                "device_name": "open-xiaoai",
                "features": {"mcp": False},
                "audio_params": {
                    "format": "opus",
                    "sample_rate": 16000,
                    "channels": 1,
                    "frame_duration": 60,
                },
            }
            self._log_info("➡️ 发送 hello 握手")
            await self.send_text(json.dumps(hello_message))

            # 步骤12：等待服务端回复hello（最多等10秒，超时则失败）
            try:
                # 开始异步等待
                # asyncio.wait_for：设置超时，避免无限等待
                # self.hello_received.wait()：启动等待等待接受的信号，在被set前这个方法会一直循环跑
                # 正常情况下_message_handler会收到服务端回复并且执行self.hello_received.set()，结束等待
                await asyncio.wait_for(self.hello_received.wait(), timeout=10.0)
                # 收到hello后，标记连接成功
                self.connected = True
                self._log_info("✅ WebSocket 已连接")
                return True
            # 超时：服务端没回复hello，触发错误回调
            except asyncio.TimeoutError:
                if self.on_network_error:
                    self.on_network_error("等待响应超时")
                await self._close_websocket()
                return False
        # 步骤13：全局异常捕获
        except Exception as e:
            # 尝试输出更具体的 close code/reason，方便定位服务端为何主动断开
            code = getattr(e, "code", None)
            reason = getattr(e, "reason", None)
            if code is not None:
                msg = f"无法连接服务: close_code={code}, reason={reason or ''}"
            else:
                msg = f"无法连接服务: {str(e)}"
            if self.on_network_error:
                self.on_network_error(msg)
            return False

    # 持续监听服务端发来的所有消息，区分文本消息（JSON / 纯文本）和二进制音频消息，针对性处理
    async def _message_handler(self):
        """处理接收到的WebSocket消息"""
        try:
            # 步骤1：持续监听WebSocket消息（核心循环）
            # async for：异步遍历WebSocket连接的消息流，有消息就取，没消息就暂停（不占CPU）
            # self.websocket：已建立的WebSocket连接对象
            async for message in self.websocket:
                # 步骤2：判断是否是json消息，根据type参数区分处理方法
                if isinstance(message, str):
                    try:
                        data = json.loads(message)
                        msg_type = data.get("type")
                        # 处理首次连接hello消息 
                        if msg_type == "hello":
                            await self._handle_server_hello(data)
                        # 处理普通消息 
                        else:
                            if self.on_incoming_json:
                                self.on_incoming_json(data)
                    # 如果解析JSON失败，直接把内容打印出来
                    except json.JSONDecodeError:
                        # 服务端在认证失败等情况下可能返回纯文本（例如“认证失败”）
                        text = (message or "").strip()
                        if text:
                            print(f"📩 收到文本消息: {text[:300]}")
                # 步骤3：二进制消息（主要是音频数据）
                elif self.on_incoming_audio:
                    self.on_incoming_audio(message)
        # 步骤4：监听过程中如果出现异常，conncted参数设置为false，关闭整个ws连接，然后会触发xiaozhi 客户端停止设备音频流
        except Exception as e:
            self.connected = False
            code = getattr(e, "code", None)
            reason = getattr(e, "reason", None)
            if self.on_network_error and code is not None:
                self.on_network_error(f"连接已关闭: close_code={code}, reason={reason or ''}")
            if self.on_audio_channel_closed:
                await self.on_audio_channel_closed()

    async def send_audio(self, frames: list[bytes]):
        """发送音频数据"""
        # 1. 前置检查：音频通道未打开则直接返回，不执行发送逻辑
        if not self.is_audio_channel_opened():  # 使用已有的 is_connected 方法
            return

        try:
            # 2. 遍历音频帧列表，逐个异步发送
            for frame in frames:
                await self.websocket.send(frame)
        except Exception:
            # 3. 捕获所有异常并静默处理（仅 pass）
            pass

    async def send_text(self, message: str):
        """发送文本消息"""
        if self.websocket:
            try:
                await self.websocket.send(message)
            except Exception as e:
                raise e
    def is_audio_channel_opened(self) -> bool:
        """检查音频通道是否打开"""
        return self.websocket is not None and self.connected

    _is_heartbeat_running = False

    # 入口方法，xiaozhi脚本开启后最后会通过本方法：1.启动心跳 2.开启ws连接
    async def open_audio_channel(self):
        if not self._is_heartbeat_running:
            self._is_heartbeat_running = True
            asyncio.create_task(self.heartbeat())
        await self.connect()

    async def _handle_server_hello(self, data: dict):
        """处理服务器的 hello 消息

        解析服务器返回的 hello 消息，设置相关参数并通知音频通道已打开

        Args:
            data: 服务器返回的 hello 消息数据
        """
        try:
            # 有些服务端不会带 transport 字段；仅当字段存在且不匹配时才拒绝
            transport = data.get("transport")
            if transport and transport != "websocket":
                return

            self._log_info(f"⬅️ 收到服务器 hello：{data}")

            session_id = data.get("session_id") or data.get("sessionId")
            if session_id:
                self.session_id = session_id

            # TODO 使用默认的 24k 采样率
            # xiaozhi-esp32-server 返回的参数是 16k 采样率，但实际用的是 24k 采样率

            # 获取音频参数
            # audio_params = data.get("audio_params")
            # if audio_params:
            #     # 获取服务器的采样率
            #     sample_rate = audio_params.get("sample_rate")
            #     if sample_rate:
            #         self.server_sample_rate = sample_rate
            #     frame_duration = audio_params.get("frame_duration")
            #     if frame_duration:
            #         self.server_frame_duration = frame_duration
            #     self.server_frame_size = int(
            #         self.server_sample_rate * (self.server_frame_duration / 1000)
            #     )

            # 设置 hello 接收事件
            self.hello_received.set()

            # 通知音频通道已打开
            if self.on_audio_channel_opened:
                await self.on_audio_channel_opened()

        except Exception as e:
            if self.on_network_error:
                self.on_network_error(f"处理服务器响应失败: {str(e)}")

    async def close_audio_channel(self):
        """关闭音频通道"""
        if self.websocket:
            try:
                await self.websocket.close()
                self.websocket = None
                self.connected = False
                if self.on_audio_channel_closed:
                    await self.on_audio_channel_closed()
            except Exception:
                pass
    
    # 无限循环心跳，一秒跳一次
    async def heartbeat(self):
        while True:
            # 当 ws 已连接 并且 设备状态是IDLE，发送心跳
            if self.websocket and get_xiaozhi().device_state == DeviceState.IDLE:
                try:
                    await self.send_text(
                        json.dumps({"session_id": "", "type": "ping"}) #发送心跳信息
                    )
                except Exception:
                    # 发送心跳失败，重新连接
                    await self.open_audio_channel()
            await asyncio.sleep(1) # 暂停1秒（避免无限循环占用CPU）
