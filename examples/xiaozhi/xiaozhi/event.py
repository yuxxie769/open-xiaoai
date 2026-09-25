import asyncio

from config import APP_CONFIG
from xiaozhi.ref import (
    get_audio_codec,
    get_kws,
    get_speaker,
    get_vad,
    get_xiaoai,
    get_xiaozhi,
    set_speech_frames,
)
from xiaozhi.services.protocols.typing import AbortReason, DeviceState, ListeningMode
from xiaozhi.utils.base import get_env

# 记录当前对话到哪一步了
class Step:
    idle = "idle"
    on_interrupt = "on_interrupt"
    on_wakeup = "on_wakeup"
    on_tts_start = "on_tts_start"
    on_tts_end = "on_tts_end"
    on_speech = "on_speech"
    on_silence = "on_silence"


class __EventManager:
    def __init__(self):
        self.session_id = 0
        self.current_step = Step.idle #当前STEP
        self.next_step_future = None  #当前FUTURE

    # 用途： 更新EventManager的current_step，并且通知 wait_next_step（）STEP已经更新，继续它下面的代码
    def update_step(self, step: Step, step_data=None):
        if not get_env("CLI"):
            return

        self.current_step = step
        if self.next_step_future: # 如果存在future 实例，说明 EventManager 的 Future 任务还在等通知
            get_xiaoai().async_loop.call_soon_threadsafe(
                self.next_step_future.set_result, (step, step_data) #结束next_step_future中的Future 任务，通知wait_next_step（）
            )
            self.next_step_future = None

    # 输入：空 或者 超时时长timeout， 输出：（当前step，回传step_data）
    # 目的： 用于等待其他 STEP 的更新，被通知/超时/session过期之前便不会执行它后面的代码
    async def wait_next_step(self, timeout=None):
        current_session = self.session_id #记录会话seesion

        ## 创建一个一个Future 对象，eventManager绑定它，可以认为它是一个空的异步任务，没任何内容也不会执行，只有set_result（）通知它才会结束并返回结果
        self.next_step_future = get_xiaoai().async_loop.create_future() 

        # 内部计时函数，“等 timeout 秒，然后确认超时返回step ”timeout”
        async def _timeout(timeout):
            idx = 0
            while idx < timeout:
                idx += 1
                await asyncio.sleep(1)
            return ("timeout", None)

        futures = [self.next_step_future] #生成一个Future list， 把当前 Future放进去，方便其他异步任务也放进来。目前里面还没有任何结果

        # 如果输入变量有超时时长timeout，在Future list加入一个异步_timeout计时任务
        if timeout:
            futures.append(get_xiaoai().async_loop.create_task(_timeout(timeout)))
        
        ### 开始停下来等待 STEP 更新 ###
        #等待任意list中的任何异步任务完成并返回结果，它可能是：update_step()被call set_result执行 并导致 Future完成、_timeout超时、session被打断
        done, _ = await asyncio.wait(
            futures, # 要等待的任务列表（Future对象）
            return_when=asyncio.FIRST_COMPLETED, # 触发返回的条件，这里为“第一个任务完成就返回”
        )
        # 如果前面任务返回后 eventManager 的session id已经和方法开头记录的当前seesion id不匹配，
        # 说明当前 session 已经结束，直接进入interrupted step
        if current_session != self.session_id:
            return ("interrupted", None)
        # 返回最先完成的异步任务
        return list(done)[0].result()

    def on_interrupt(self):
        """用户打断（小爱同学）"""
        self.session_id = self.session_id + 1
        self.update_step(Step.on_interrupt)
        self.start_session()

    def on_wakeup(self):
        """用户唤醒（你好小智）"""
        self.session_id = self.session_id + 1
        self.update_step(Step.on_wakeup)
        self.start_session() 

    # 告诉状态机“结束 TTS 播放阶段了，进入一下对对话了”
    def on_tts_end(self, session_id):
        """TTS结束"""
        # 如果刚刚被打断了或已经处理过 end，就不再处理
        if self.current_step in [Step.on_interrupt, Step.on_tts_end]:
            # 当前 session 已经被打断了，不再处理
            return
        self.session_id = self.session_id + 1 # 更新session id
        self.update_step(Step.on_tts_end)  #更新on_tts_end状态
        self.start_session() #进入下一轮 session，等待用户说话 或者 直接退出唤醒
    
    # 告诉状态机“现在进入 TTS 播放阶段了”（并且如果有人在 wait_next_step() 等事件，会被唤醒）。
    def on_tts_start(self, session_id):
        """TTS结束"""
        self.update_step(Step.on_tts_start)

    def on_speech(self, speech_buffer: bytes):
        """检测到声音（开始说话"""
        self.update_step(Step.on_speech, speech_buffer)

    def on_silence(self):
        """检测到静音（说话结束）"""
        self.update_step(Step.on_silence)

    #异步执行__start_session方法
    def start_session(self):
        asyncio.run_coroutine_threadsafe( #线程安全的函数，专门用于「跨线程调用协程」。需要执行一个异步协程函数时，必须用这个方法。
            self.__start_session(), get_xiaoai().async_loop #把__start_session这个异步协程函数，扔到async_loop这个异步任务list里面等待执行
        )

    # 主要用于当 【音箱刚唤醒】 或者 【一段TTS播放结束后】 的阶段，用户说话回复/用户停止回复/小爱打断等情况的处理
    # 分为三个阶段 【会话结束阶段一】，【用户说话阶段 】，【会话结束阶段二】
    async def __start_session(self):
        if not get_env("CLI"):
            return

        # 获得依赖对象
        vad = get_vad()
        codec = get_audio_codec() # 录音流
        speaker = get_speaker() # SpeakerManager类，就是音箱本身
        xiaozhi = get_xiaozhi() # xiaozhi，智能音箱应用程序主类，这里用于管理device_state

        ### 会话结束阶段一 ###
        ####################

        # 先取消之前的 VAD 检测和音频输入输出流
        xiaozhi.set_device_state(DeviceState.IDLE) # 设备进入IDLE状态
        await xiaozhi.protocol.send_abort_speaking(AbortReason.ABORT) # 向服务端发送停止信号

        # 小爱同学唤醒时，直接打断，不进入录音会话。
        if self.current_step == Step.on_interrupt:
            return

        # on_tts_end 状态下，等待 TTS 余音结束，防止干扰下一步
        if self.current_step in [Step.on_tts_end]:
            vad.resume("silence") #开始监听静音事件
            step, _ = await self.wait_next_step() # 停在这里，等待用户其他行为。比如等到on_silence方法执行，得到静音STEP就可以继续执行后续操作了
            # 如果等到的不是静音STEP，说明流程被别的事件打断了，直接退出
            if step != Step.on_silence:
                return

        
        ### 用户说话阶段 ###
        ####################

        vad.resume("speech") # 监测用户说话
        # 停，等待 step 更新，获取VAD 检测到的那点“开头音频”speech_buffer
        step, speech_buffer = await self.wait_next_step(
            timeout=APP_CONFIG["wakeup"]["timeout"] # 输入配置文件里面的静音等待时长
        )

        # 如果是超时STEP，那就是用户没说话，退出唤醒，回到 IDLE 状态
        if step == "timeout":
            # 如果没人说话，则设备回到 IDLE 状态
            xiaozhi.set_device_state(DeviceState.IDLE)
            print("👋 已退出唤醒")
            after_wakeup = APP_CONFIG["wakeup"]["after_wakeup"]
            await after_wakeup(speaker) #执行配置文件中的解释唤醒后处理方法，一般是播放结束语音
            return
        # 如果没讲话也没进入超时，可能发生了其他情况，直接退出
        if step != Step.on_speech:
            return

        ## 如果用户说话了，当前是on_speech STEP, 进入录音，也就是所谓的连续对话开始了
        # 开始说话
        set_speech_frames(speech_buffer) # 把刚刚 VAD 检测到的那点“开头音频”先存起来（避免漏掉第一句话开头），codec音箱录音流会自动拼接进第一句话
        codec.input_stream.start_stream()  # 音箱开启麦克风录音，虽然后面set_device_state为LISTENING后，xiaozhi主循环也会自动开启
        await xiaozhi.protocol.send_start_listening(ListeningMode.MANUAL) #json通知服务端接下来要发音频了
        xiaozhi.set_device_state(DeviceState.LISTENING) # 设备状态设置为LISTENING状态

        # 重新监测静音，等待说话结束
        vad.resume("silence")
        step, _ = await self.wait_next_step() # 停，继续等待说话结束，更新静音STEP
        if step != Step.on_silence:
            return

        ### 会话结束阶段一 ###
        ####################

        # 停止说话了，停止录音，设备回到IDLE状态
        await xiaozhi.protocol.send_stop_listening() #json通知服务端接下来停止发送音频了
        xiaozhi.set_device_state(DeviceState.IDLE) # 设备状态设置回IDLE，xiaozhi主循环会停止音箱录音

    # 唤醒音箱
    async def wakeup(self, text, source):
        # 获得before_wakeup（）方法
        before_wakeup = APP_CONFIG["wakeup"]["before_wakeup"]
        get_kws().pause()  # 暂停 KWS 检测
        wakeup = await before_wakeup(get_speaker(), text, source) #唤醒前处理，一般是播放唤醒提示语
        get_kws().resume()  # 恢复 KWS 检测
        if wakeup:
            self.on_wakeup() # 设置 唤醒 STEP


EventManager = __EventManager()
