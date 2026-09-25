import asyncio
import json
import re
import threading
import time

from xiaozhi.event import EventManager
from xiaozhi.ref import get_speaker, get_xiaoai, set_xiaozhi
from xiaozhi.services.audio.kws import KWS
from xiaozhi.services.audio.vad import VAD
from xiaozhi.services.protocols.typing import (
    AbortReason,
    DeviceState,
    EventType,
    ListeningMode,
)
from xiaozhi.services.protocols.websocket_protocol import WebsocketProtocol
from xiaozhi.utils.base import get_env
from xiaozhi.utils.config import ConfigManager
from xiaozhi.xiaoai import XiaoAI


class XiaoZhi:
    """智能音箱应用程序主类"""

    _instance = None

    @classmethod
    def instance(cls):
        """获取单例实例"""
        if cls._instance is None:
            cls._instance = XiaoZhi()
        return cls._instance

    def __init__(self):
        """初始化应用程序"""
        # 确保单例模式
        if XiaoZhi._instance is not None:
            raise Exception("XiaoZhi是单例类，请使用instance()获取实例")
        XiaoZhi._instance = self

        # 获取配置管理器实例
        self.config = ConfigManager.instance()

        # 状态变量
        self.device_state = DeviceState.IDLE
        self.voice_detected = False
        self.current_text = ""
        self.current_emotion = "neutral"

        # 音频处理相关
        self.audio_codec = None

        # 事件循环和线程
        self.loop = asyncio.new_event_loop()
        self.loop_thread = None
        self.running = False

        # 任务队列和锁
        self.main_tasks = []
        self.mutex = threading.Lock()

        # 协议实例
        self.protocol = None

        # 回调函数
        self.on_state_changed_callbacks = []

        # 初始化事件对象
        self.events = {
            EventType.SCHEDULE_EVENT: threading.Event(),
            EventType.AUDIO_INPUT_READY_EVENT: threading.Event(),
        }

        # 创建显示界面
        self.display = None
        set_xiaozhi(self)

    def run(self):
        self.protocol = WebsocketProtocol()

        # 创建并启动事件循环线程
        self.loop_thread = threading.Thread(target=self._run_event_loop)
        self.loop_thread.daemon = True
        self.loop_thread.start()

        # 等待事件循环准备就绪
        time.sleep(0.1)

        # 初始化应用程序
        asyncio.run_coroutine_threadsafe(XiaoAI.init_xiaoai(), self.loop)
        asyncio.run_coroutine_threadsafe(self._initialize_xiaozhi(), self.loop)

        # 启动主循环线程
        main_loop_thread = threading.Thread(target=self._main_loop)
        main_loop_thread.daemon = True
        main_loop_thread.start()

        VAD.start()
        KWS.start()

        try:
            from xiaozhi.services.scheduler import Scheduler

            Scheduler.start()
        except Exception:
            pass

        # 启动 GUI
        self._initialize_display()
        self.display.start()

    def _run_event_loop(self):
        """运行事件循环的线程函数"""
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    async def _initialize_xiaozhi(self):
        """初始化应用程序组件"""

        # 初始化音频编解码器
        self._initialize_audio()

        # 设置协议回调
        self.protocol.on_network_error = self._on_network_error
        self.protocol.on_incoming_audio = self._on_incoming_audio
        self.protocol.on_incoming_json = self._on_incoming_json
        self.protocol.on_audio_channel_opened = self._on_audio_channel_opened
        self.protocol.on_audio_channel_closed = self._on_audio_channel_closed

        # 打开音频通道
        self.device_state = DeviceState.CONNECTING
        await self.protocol.open_audio_channel()

    def _initialize_audio(self):
        """初始化音频设备和编解码器"""
        try:
            from xiaozhi.services.audio.codec import AudioCodec

            self.audio_codec = AudioCodec()
        except Exception as e:
            self.alert("错误", f"初始化音频设备失败: {e}")

        #启动单独线程，轮询 input_stream 是否 active，如果active说明在录音，则触发AUDIO_INPUT_READY_EVENT，用于激活主循环中的方法进行音频处理并发送服务端
        threading.Thread(target=self._audio_input_event_trigger, daemon=True).start() 

    def _initialize_display(self):
        """初始化显示界面"""
        if get_env("CLI"):
            from xiaozhi.services.display import no_display

            self.display = no_display.NoDisplay()
        else:
            from xiaozhi.services.display import gui_display

            self.display = gui_display.GuiDisplay()

        # 设置回调函数
        self.display.set_callbacks(
            press_callback=self.start_listening,
            release_callback=self.stop_listening,
            status_callback=self._get_status_text,
            text_callback=self._get_current_text,
            emotion_callback=self._get_current_emotion,
            mode_callback=self._on_mode_changed,
            auto_callback=self.toggle_chat_state,
            abort_callback=lambda: self.abort_speaking(AbortReason.WAKE_WORD_DETECTED),
        )

    def _main_loop(self):
        """应用程序主循环"""
        # 1. 标记主循环为运行状态
        self.running = True

        # 2. 主循环核心：只要 running 为 True，就持续执行
        while self.running:
            # 3. 遍历所有已注册的事件，检查是否有事件被触发
            for event_type, event in self.events.items():
                # 4. 判断当前事件是否被设置（触发）
                if event.is_set():
                    # 5. 清除事件的触发状态（避免重复处理）
                    event.clear()

                    # 6. 根据事件类型执行对应的处理逻辑
                    # AUDIO_INPUT_READY_EVENT 代表“现在有音频输入流处于 active（可读）”
                    if event_type == EventType.AUDIO_INPUT_READY_EVENT:
                        # 处理音频输入就绪事件（比如读取、发送音频数据）
                        self._handle_input_audio()
                    elif event_type == EventType.SCHEDULE_EVENT:
                        # 处理定时任务事件（比如执行计划内的操作）
                        self._process_scheduled_tasks()

            time.sleep(0.01)

    def _process_scheduled_tasks(self):
        """处理调度任务"""
        with self.mutex:
            tasks = self.main_tasks.copy()
            self.main_tasks.clear()

        for task in tasks:
            try:
                task()
            except Exception:
                pass

    def schedule(self, callback):
        """调度任务到主循环"""
        with self.mutex:
            # 如果是中止语音的任务，检查是否已经存在相同类型的任务
            if "abort_speaking" in str(callback):
                # 如果已经有中止任务在队列中，就不再添加
                if any("abort_speaking" in str(task) for task in self.main_tasks):
                    return
            self.main_tasks.append(callback)
        self.events[EventType.SCHEDULE_EVENT].set()

    # 用于主循环处理音频并发送给客户端
    def _handle_input_audio(self):
        """处理音频输入"""
        # 如果设备状态不是LISTENING，不进行音频处理。
        # 这是业务状态闸门，即使AUDIO_INPUT_READY_EVENT是触发的，代表麦克风有输入【可能是背景音】，但是因为没到该监听流程故不能继续处理音频
        if self.device_state != DeviceState.LISTENING:
            return

        encoded_data = self.audio_codec.read_audio() # 拼接VAD缓存+当前录制的PCM音频帧，再编码成Opus输出为encoded_data
        # 如果拿到Opus音频帧，同时ws连接正常，那么就发送音频帧encoded_data到服务端
        if encoded_data and self.protocol and self.protocol.is_audio_channel_opened():
            asyncio.run_coroutine_threadsafe(
                self.protocol.send_audio(encoded_data), self.loop
            )

    def _on_network_error(self, message):
        """网络错误回调"""
        # CLI 模式下 display 不输出，必须打印到控制台，方便排查连接问题
        if message:
            print(f"⚠️ 网络错误：{message}")
            self.alert("网络错误", message)

        self.set_device_state(DeviceState.IDLE)
        if self.device_state != DeviceState.CONNECTING:
            self.set_device_state(DeviceState.IDLE)

            # 关闭现有连接
            if self.protocol:
                asyncio.run_coroutine_threadsafe(
                    self.protocol.close_audio_channel(), self.loop
                )

    def _on_incoming_audio(self, data):
        """接收音频数据回调"""
        if self.device_state == DeviceState.SPEAKING:
            self.audio_codec.write_audio(data)

    def _on_incoming_json(self, json_data):
        """接收JSON数据回调"""
        try:
            if not json_data:
                return

            # 解析JSON数据
            if isinstance(json_data, str):
                data = json.loads(json_data)
            else:
                data = json_data

            # 处理不同类型的消息
            msg_type = data.get("type", "")
            if msg_type == "tts":
                self._handle_tts_message(data)
            elif msg_type == "stt":
                self._handle_stt_message(data)
            elif msg_type == "llm":
                self._handle_llm_message(data)
            elif msg_type in ("command", "cmd"):
                self._handle_command_message(data)
            else:
                # 打印未知消息，便于排查服务端为何未返回 hello/为何断开
                print(f"📩 收到未处理的消息: {data}")
        except Exception:
            pass

    def _handle_command_message(self, data: dict):
        """
        Handle server-initiated commands.

        Expected payload examples:
        - {"type":"command","action":"chat_xiaozhi","text":"...","abort_before":true}
        - {"type":"command","action":"wake","silent":true}
        - {"type":"command","action":"play_tts","text":"...","wake_up":true,"silent_wake":true,"blocking":false}
        """
        action = (data.get("action") or "").strip()
        if not action:
            return

        if action == "chat_xiaozhi":
            text = data.get("text", "")
            if not text or not self.protocol:
                return

            if bool(data.get("abort_before", False)) and self.device_state == DeviceState.SPEAKING:
                asyncio.run_coroutine_threadsafe(
                    self.protocol.send_abort_speaking(AbortReason.ABORT),
                    self.loop,
                )

            session_id = getattr(self.protocol, "session_id", "") or ""
            payload = {
                "type": "listen",
                "state": "detect",
                "text": text,
                "source": "server_push",
                "session_id": session_id,
            }
            asyncio.run_coroutine_threadsafe(
                self.protocol.send_text(json.dumps(payload, ensure_ascii=False)),
                self.loop,
            )
            return

        speaker = get_speaker()
        if speaker is None:
            return

        xiaoai = get_xiaoai()
        speaker_loop = getattr(xiaoai, "async_loop", None) if xiaoai else None
        if speaker_loop is None:
            return

        async def _run_speaker_command():
            wake_up = bool(data.get("wake_up", True))
            silent_wake = bool(data.get("silent_wake", True))
            blocking = bool(data.get("blocking", False))

            if action == "wake":
                await speaker.wake_up(True, silent=bool(data.get("silent", True)))
                return

            if wake_up:
                await speaker.wake_up(True, silent=silent_wake)

            if action == "play_tts":
                text = data.get("text", "")
                if text:
                    await speaker.play(text=text, blocking=blocking)
                return

            if action == "play_url":
                url = data.get("url", "")
                if url:
                    await speaker.play(url=url, blocking=blocking)
                return

            if action == "ask_xiaoai":
                text = data.get("text", "")
                if text:
                    await speaker.ask_xiaoai(text, silent=silent_wake)
                return

        asyncio.run_coroutine_threadsafe(_run_speaker_command(), speaker_loop)

    def _handle_tts_message(self, data):
        """处理TTS消息"""
        state = data.get("state", "")
        if state == "start":
            EventManager.on_tts_start(data.get("session_id"))
            self.schedule(lambda: self._handle_tts_start()) #标记DeviceState为SPEAKING
        elif state == "stop":
            EventManager.on_tts_end(data.get("session_id"))
            self.schedule(lambda: self._handle_tts_stop())
        elif state == "sentence_start":
            text = data.get("text", "")
            if text:
                print(f"🤖 小智：{text}")

                verification_code = re.search(r"验证码.*?(\d+)", text) or re.search(
                    r"控制面板.*?(\d+)", text
                )
                if verification_code:
                    self.config.update_config_file(
                        "VERIFICATION_CODE", verification_code.group(1)
                    )

                self.schedule(lambda: self.set_chat_message("assistant", text))

    #转换DeviceState为SPEAKING
    def _handle_tts_start(self):
        """处理TTS开始事件"""
        if (
            self.device_state == DeviceState.IDLE
            or self.device_state == DeviceState.LISTENING
        ):
            self.set_device_state(DeviceState.SPEAKING)

    def _handle_tts_stop(self):
        """处理TTS停止事件"""
        pass

    def _handle_stt_message(self, data):
        """处理STT消息"""
        text = data.get("text", "")
        if text:
            print(f"💬 我说：{text}")
            self.schedule(lambda: self.set_chat_message("user", text))

    def _handle_llm_message(self, data):
        """处理LLM消息"""
        emotion = data.get("emotion", "")
        if emotion:
            self.schedule(lambda: self.set_emotion(emotion))

    async def _on_audio_channel_opened(self):
        """音频通道打开回调"""
        self.set_device_state(DeviceState.IDLE)

    # 检查音响麦克风是否有输入
    def _audio_input_event_trigger(self):
        """音频输入事件触发器"""
        # 1. 循环控制：只要程序主开关 running 为 True，就持续执行
        while self.running:
            # 2. 检查音频输入流是否活跃（是否有音频数据可读）
            if self.audio_codec.input_stream.is_active():
                # 3. 如有输入，触发“音频输入就绪”事件（给主循环发信号）
                self.events[EventType.AUDIO_INPUT_READY_EVENT].set()
            time.sleep(0.01)

    async def _on_audio_channel_closed(self):
        """音频通道关闭回调"""
        self.set_device_state(DeviceState.IDLE)
        self.audio_codec.stop_streams()

    def set_device_state(self, state):
        """设置设备状态"""
        self.device_state = state

        VAD.pause()  # 停用 VAD
        self.audio_codec.stop_streams()  # 停用输入输出流

        if state == DeviceState.IDLE:
            self.display.update_status("待命")
            self.display.update_emotion("😶")
        elif state == DeviceState.CONNECTING:
            self.display.update_status("连接中...")
        elif state == DeviceState.LISTENING:
            self.display.update_status("聆听中...")
            self.display.update_emotion("🙂")
            # 停止输出流
            if self.audio_codec.output_stream.is_active():
                self.audio_codec.output_stream.stop_stream()
            # 打开输入流
            if not self.audio_codec.input_stream.is_active():
                self.audio_codec.input_stream.start_stream()
        elif state == DeviceState.SPEAKING:
            self.display.update_status("说话中...")
            # 停止输入流
            if self.audio_codec.input_stream.is_active():
                self.audio_codec.input_stream.stop_stream()
            # 打开输出流
            if not self.audio_codec.output_stream.is_active():
                self.audio_codec.output_stream.start_stream()

        # 通知状态变化
        for callback in self.on_state_changed_callbacks:
            try:
                callback(state)
            except Exception:
                pass

    def _get_status_text(self):
        """获取当前状态文本"""
        states = {
            DeviceState.IDLE: "待命",
            DeviceState.CONNECTING: "连接中...",
            DeviceState.LISTENING: "聆听中...",
            DeviceState.SPEAKING: "说话中...",
        }
        return states.get(self.device_state, "未知")

    def _get_current_text(self):
        """获取当前显示文本"""
        return self.current_text

    def _get_current_emotion(self):
        """获取当前表情"""
        emotions = {
            "neutral": "😶",
            "happy": "🙂",
            "laughing": "😆",
            "funny": "😂",
            "sad": "😔",
            "angry": "😠",
            "crying": "😭",
            "loving": "😍",
            "embarrassed": "😳",
            "surprised": "😲",
            "shocked": "😱",
            "thinking": "🤔",
            "winking": "😉",
            "cool": "😎",
            "relaxed": "😌",
            "delicious": "🤤",
            "kissy": "😘",
            "confident": "😏",
            "sleepy": "😴",
            "silly": "😜",
            "confused": "🙄",
        }
        return emotions.get(self.current_emotion, "😶")

    def set_chat_message(self, role, message):
        """设置聊天消息"""
        self.current_text = message
        # 更新显示
        if self.display:
            self.display.update_text(message)

    def set_emotion(self, emotion):
        """设置表情"""
        self.current_emotion = emotion
        # 更新显示
        if self.display:
            self.display.update_emotion(self._get_current_emotion())

    def start_listening(self):
        """开始监听"""
        self.schedule(self._start_listening_impl)

    def _start_listening_impl(self):
        """开始监听的实现"""
        if not self.protocol:
            return

        self.set_device_state(DeviceState.IDLE)
        asyncio.run_coroutine_threadsafe(
            self.protocol.send_abort_speaking(AbortReason.ABORT),
            self.loop,
        )
        asyncio.run_coroutine_threadsafe(
            self.protocol.send_start_listening(ListeningMode.MANUAL), self.loop
        )
        self.set_device_state(DeviceState.LISTENING)

    def stop_listening(self):
        """停止监听"""
        self.schedule(self._stop_listening_impl)

    def _stop_listening_impl(self):
        """停止监听的实现"""
        asyncio.run_coroutine_threadsafe(self.protocol.send_stop_listening(), self.loop)
        self.set_device_state(DeviceState.IDLE)

    def abort_speaking(self, reason):
        """中止语音输出"""
        self.set_device_state(DeviceState.IDLE)
        asyncio.run_coroutine_threadsafe(
            self.protocol.send_abort_speaking(AbortReason.ABORT),
            self.loop,
        )

    def alert(self, title, message):
        """显示警告信息"""
        if self.display:
            self.display.update_text(f"{title}: {message}")

    def on_state_changed(self, callback):
        """注册状态变化回调"""
        self.on_state_changed_callbacks.append(callback)

    def shutdown(self):
        """关闭应用程序"""
        self.running = False

        try:
            from xiaozhi.services.scheduler import Scheduler

            Scheduler.stop()
        except Exception:
            pass

        # 关闭音频编解码器
        if self.audio_codec:
            self.audio_codec.close()

        # 关闭协议
        if self.protocol:
            asyncio.run_coroutine_threadsafe(
                self.protocol.close_audio_channel(), self.loop
            )

        # 停止事件循环
        if self.loop and self.loop.is_running():
            self.loop.call_soon_threadsafe(self.loop.stop)

        # 等待事件循环线程结束
        if self.loop_thread and self.loop_thread.is_alive():
            self.loop_thread.join(timeout=1.0)

    def toggle_chat_state(self):
        """切换聊天状态"""
        pass

    def _on_mode_changed(self, auto_mode):
        """处理对话模式变更"""
        pass
