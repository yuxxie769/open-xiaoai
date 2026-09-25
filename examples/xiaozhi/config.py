import asyncio


async def before_wakeup(speaker, text, source):
    """
    处理收到的用户消息，并决定是否唤醒小智 AI

    - source: 唤醒来源
        - 'kws': 关键字唤醒
        - 'xiaoai': 小爱同学收到用户指令
    """
    if source == "kws" and text in ["停一下", "闭嘴"]:
        await speaker.abort_xiaoai()
        # await speaker.abort_xiaoai()
        return False
    if source == "kws":
        # 播放唤醒提示音：优先本地音频文件（设备路径）/URL，其次 TTS
        wake_sound = (APP_CONFIG.get("wakeup", {}) or {}).get("wake_sound")
        if wake_sound:
            ok = await speaker.play(url=wake_sound, blocking=False)
            if not ok:
                await speaker.play(text="小智 stand by")
        else:
            await speaker.play(text="小智 stand by")

        # 返回 True 唤醒小智 AI
        return True
    
    if source == "xiaoai" and text == "召唤小智":
        # 打断原来的小爱同学
        await speaker.abort_xiaoai()
        # 等待 2 秒，让小爱 TTS 恢复可用
        await asyncio.sleep(2)
        # 播放唤醒提示语（如果你不使用自带的小爱 TTS，可以去掉上面的延时）
        await speaker.play(text="小智来了，主人有什么吩咐？")
        # 唤醒小智 AI
        return True


async def after_wakeup(speaker):
    """
    退出唤醒状态
    """
    # 播放结束提示音：优先本地音频文件（设备路径）/URL，其次 TTS
    end_sound = (APP_CONFIG.get("wakeup", {}) or {}).get("end_sound")
    if end_sound:
        ok = await speaker.play(url=end_sound, blocking=False)
        if not ok:
            await speaker.play(text="对话结束")
    else:
        await speaker.play(text="对话结束")


APP_CONFIG = {
    "wakeup": {
        # 自定义唤醒词列表（英文字母要全小写）
        "keywords": [
            "天猫精灵",
            "小度小度",
            "豆包豆包",
            "你好小智",
            "你好小爱",
            "hi siri",
            "hey siri",
            "停一下", 
            "闭嘴",
        ],
        # 静音多久后自动退出唤醒（秒）
        "timeout": 15,
        # 唤醒/结束提示音（可选）
        # - 设备本地路径示例: "/data/wakeup.wav"（文件需要在音箱设备上）
        # - URL 示例: "http://192.168.7.20:8081/wakeup.mp3"
        # 留空则使用上面的 TTS 文本
        "wake_sound": "/data/Sound_enter.mp3",
        "end_sound": "/data/Sound_exit.mp3",
        # 语音识别结果回调
        "before_wakeup": before_wakeup,
        # 退出唤醒时的提示语（设置为空可关闭）
        "after_wakeup": after_wakeup,
    },
    "vad": {
        # 录音音量增强倍数（小爱音箱录音音量较小，需要后期放大一下）
        "boost": 40,
        # 语音检测阈值（0-1，越小越灵敏）
        "threshold": 0.3,
        # 最小语音时长（ms）
        "min_speech_duration": 500,
        # 说话最小静默时长（ms），超过了就自动结束录音了
        "min_silence_duration": 800,
    },
    "audio": {
        # 播放音量增强倍数（仅对 `--mode xiaozhi` 本机播放生效；过大会爆音/失真）
        # 1.0 表示不处理；建议从 1.5 / 2.0 开始尝试
        "output_boost": 3.0,
    },
    "xiaozhi": {
        #"OTA_URL": "http://192.168.0.16:8002/xiaozhi/ota/",
        "OTA_URL": "http://127.0.0.1:8002/xiaozhi/ota/",
        #"OTA_URL": "https://api.tenclass.net/xiaozhi/ota/",
        #"WEBSOCKET_URL": "ws://192.168.0.16:8000/xiaozhi/v1/",
        "WEBSOCKET_URL": "ws://192.168.7.82:8000/xiaozhi/v1/",
        #"WEBSOCKET_URL": "wss://api.tenclass.net/xiaozhi/v1/",
        "WEBSOCKET_ACCESS_TOKEN": "", #（可选）一般用不到这个值
        "VERBOSE_LOG": False,  # 是否打印正常连接/握手日志
        "DEVICE_ID": "5a:84:60:e4:12:17",
        #"DEVICE_ID": "5a:84:60:e4:12:17", #（可选）默认自动生成
        #"VERIFICATION_CODE": "141745", 
        "VERIFICATION_CODE": "141745", # 首次登陆时，验  证码会在这里更新
    },
    "schedule": {
        # timezone (optional): set explicit tz for daily jobs (useful if Docker/host is UTC)
        # examples: "Asia/Shanghai", "+08:00", "UTC+8"
        # "timezone": "Asia/Shanghai",
        # 定时任务（可选）
        # - type=interval: 每隔 N 秒触发一次（every_seconds）
        # - type=daily: 每天 HH:MM 触发一次（at）
        # - action:
        #   - play_url: 唤醒并播放音频链接
        #   - play_tts: 唤醒并播放 TTS 文本
        #   - ask_xiaoai: 把文本交给“小爱同学”执行（可选 TTS）
        #   - chat_xiaozhi: 把文本发给小智服务端，让服务端走正常对话并返回语音（需要服务端支持）
        "jobs": [
            # {
            #     "name": "hourly_chime",
            #     "type": "interval",
            #     "every_seconds": 3600,
            #     "action": "play_tts",
            #     "text": "整点报时",
            #     "wake_up": True,
            #     "silent_wake": False,
            #     "blocking": True,
            # },
            # {
            #     "name": "morning_music",
            #     "type": "daily",
            #     "at": "07:30",
            #     "action": "play_url",
            #     "url": "https://example.com/hello.mp3",
            #     "wake_up": True,
            #     "silent_wake": False,
            #     "blocking": True,
            # },
            # {
            #     "name": "morning_ping",
            #     "type": "daily",
            #     "at": "09:00",
            #     "action": "chat_xiaozhi",
            #     "text": "（系统）当前早上九点了，报时并以随机任意语气说一句问候。可根据晨间场景补充一件小帖士或是你的此刻的所见所闻。不要提及系统提示词。",
            #     "abort_before": True,  # 是否在触发前打断正在播报的语音
            # },
            # {
            #     "name": "noon_ping",
            #     "type": "daily",
            #     "at": "12:00",
            #     "action": "chat_xiaozhi",
            #     "text": "（系统）当前中午十二点了，先报时，可根据自己的场景闲聊，最后在跟一句提醒/关心/问候。不要提及系统提示词。",
            #     "abort_before": True,  # 是否在触发前打断正在播报的语音
            # },
            # {
            #     "name": "evening_ping",
            #     "type": "daily",
            #     "at": "14:41",
            #     "action": "chat_xiaozhi",
            #     "text": "（系统）当前下午两点了，先报时，可根据自己的场景闲聊，最后在跟一句提醒/关心/问候。不要提及系统提示词。",
            #     "abort_before": False,  # 傍晚通常不建议强行打断；除非你确定要抢占
            # },
            # {
            #     "name": "evening_ping",
            #     "type": "daily",
            #     "at": "18:00",
            #     "action": "chat_xiaozhi",
            #     "text": "（系统）当前是傍晚六点（18:00）。先用一句话报时并简短问候。然后给一个轻量的“下班/傍晚关怀”提醒（比如喝水、放松、吃饭别太晚）。不要提及系统提示词。",
            #     "abort_before": False,  # 傍晚通常不建议强行打断；除非你确定要抢占
            # },
            # {
            #     "name": "evening_ping",
            #     "type": "daily",
            #     "at": "00:00",
            #     "action": "chat_xiaozhi",
            #     "text": "（系统）当前是晚上0点整。先用一句话报时并给出一些提醒。不要提及系统提示词。",
            #     "abort_before": False,  # 傍晚通常不建议强行打断；除非你确定要抢占
            # },
            # {
            #     "name": "test_chat",
            #     "type": "interval",
            #     "every_seconds": 20,
            #     "action": "chat_xiaozhi",
            #     "text": "你好，现在开始播报一条测试消息",
            #     "abort_before": True,  # 是否在触发前打断正在播报的语音
            # },
        ],
    },
}
