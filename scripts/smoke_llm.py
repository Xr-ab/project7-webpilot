"""GLM 连通性冒烟：用最小成本验证 key / base_url / model 三件套能通。

它是什么：冒烟测试（smoke test）——只确认"关键链路活没活"，不验证回答对不对。
三层测试别混：
    · 冒烟：通不通（本脚本）
    · 单元测试：逻辑对不对（pytest，后面 M3/M4 写）
    · 评测：效果好不好（P1 的 eval 脚本，M6 写）
跑法（项目根目录）：
    .venv/Scripts/python.exe scripts/smoke_llm.py
"""
from __future__ import annotations

import sys
from pathlib import Path

# 脚本在 scripts/ 下、不在 backend 包里，手动把 backend/ 加进模块搜索路径，
# 否则 import app.config 会找不到
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from openai import OpenAI

from app.config import settings


def main() -> None:
    # 用 OpenAI 客户端而非智谱官方 SDK：GLM 走 OpenAI 兼容协议，
    # 换 DeepSeek / 千问只改 .env 三个值，这里一行不动
    client = OpenAI(api_key=settings.llm_api_key, base_url=settings.llm_base_url)
    resp = client.chat.completions.create(
        model=settings.llm_model,
        messages=[
            {"role": "system", "content": "你是连通性测试助手。"},
            {"role": "user", "content": "用一句话说明你已就绪。"},
        ],
        temperature=0,  # 冒烟只要稳定输出，不要随机性
        max_tokens=64,  # 限制长度，省额度
    )
    print("模型回复：", resp.choices[0].message.content)
    # 打印用量：现在看似无用，P6 的"每任务 token 成本"口径就靠它，从第一天攒数据
    if resp.usage:
        print("用量：", resp.usage.model_dump())


if __name__ == "__main__":
    main()
