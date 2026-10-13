"""bili-danmaku-recorder 核心录制包。

模块划分：
- config:   设置与录制目标的持久化（config/settings.json、config/targets.json）
- events:   进程内异步事件总线（录制事件 -> Web UI / 日志）
- filters:  文本脱敏、uid 匿名化、纯表情识别
- protocol: B 站直播 WS 协议常量、封包、wbi 签名
- session:  单个直播间的 HTTP 会话（开播状态 / danmu token / host 列表）
- models:   RoomRuntime 房间运行时状态
- storage:  jsonl 落盘（弹幕记录 / 房管处置弱标签）
- room_task:单房间录制协程（连接、重连、节点轮换、懒落盘）
- manager:  多房间任务管理器（动态增删、聚合状态、空闲自动停止）
- cli:      无界面命令行录制模式
"""

__version__ = "1.2.0"
