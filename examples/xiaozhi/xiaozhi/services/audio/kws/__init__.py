import asyncio
import os
import threading
import time

from config import APP_CONFIG
from xiaozhi.event import EventManager
from xiaozhi.ref import get_speaker, get_xiaoai, get_xiaozhi, set_kws
from xiaozhi.services.audio.kws.sherpa import SherpaOnnx
from xiaozhi.services.audio.stream import MyAudio
from xiaozhi.services.protocols.typing import AudioConfig, DeviceState
from xiaozhi.utils.base import get_env


class _KWS:
    def __init__(self):
        set_kws(self)
    # 打开麦克风音频流，然后在一个后台线程里持续跑 _detection_loop，实时监听音频并做关键词检测（KWS）。
    def start(self):
        if not get_env("CLI"):
            return

        self.audio = MyAudio.create() #创建音频对象
        self.stream = self.audio.open(  #生成 麦克风输入流 对象
            format=AudioConfig.FORMAT,
            channels=1,
            rate=16000,
            input=True,
            frames_per_buffer=AudioConfig.FRAME_SIZE,
            start=True,
        )

        # 启动 KWS 服务
        self.paused = False
        self.thread = threading.Thread(target=self._detection_loop, daemon=True) #当daemon为True时，父线程在运行完毕后，子线程无论是否正在运行，都会伴随主线程一起退出。
        self.thread.start()

    def get_file_path(self, file_name: str):
        current_dir = os.path.dirname(os.path.abspath(__file__))
        return os.path.join(current_dir, "../../../models", file_name)

    def pause(self):
        self.paused = True

    def resume(self):
        self.paused = False

    # 无限循环检测唤醒词
    def _detection_loop(self):
        SherpaOnnx.start() # 语音处理引擎（用于唤醒词检测/KWS）
        self.stream.start_stream() #正式开启 麦克风输入流
        while True:
            # 读取缓冲区音频数据
            frames = self.stream.read()

            # 在说话和监听状态时，短暂休眠KWS后继续循环
            if (
                not frames
                or self.paused
                or get_xiaozhi().device_state
                in [
                    DeviceState.LISTENING,
                    DeviceState.SPEAKING,
                ]
            ):
                time.sleep(0.01)
                continue
            
             # 核心逻辑：将音频帧传入 SherpaOnnx 引擎，检测是否有唤醒词
            result = SherpaOnnx.kws(frames)
            if result:
                print(f"🔥 触发唤醒: {result}")
                self.on_message(result) # 调用唤醒方法

    # 调用 EventManager ， 进入 on_wakeup STEP
    def on_message(self, text: str):
        asyncio.run_coroutine_threadsafe(
            EventManager.wakeup(text, "kws"),
            get_xiaoai().async_loop,
        )


KWS = _KWS()
