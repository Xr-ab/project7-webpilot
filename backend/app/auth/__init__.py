"""认证模块（P3-M4）：bcrypt 密码哈希 + JWT 签发校验 + FastAPI 依赖。

它对应的章程决策：M4 把 users 表从摆设变成真登录，任务按 user_id 隔离。
模块只有两个文件，刻意的：
- service.py 纯逻辑（哈希/校验/签发/解析），不认识 FastAPI，可以单独测
- deps.py 只有一个依赖函数，负责把 HTTP 请求翻译成"当前用户"
"""
