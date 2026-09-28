"""按请求取依赖：优先用 create_app 注入的实现，缺省时懒创建真实实现。

/api/assistant 与调试台共用，测试注入替身即可，不碰真实模型与数据库文件。
"""

from __future__ import annotations

from typing import Any

from flask import current_app

from backend.llm import MAX_PLAN_TOKENS, LiveLLM
from backend.routine import SQLiteRoutineStore


# V10 计划 v2 的编排输出是分段 JSON，明显长于 V7 计划
MAX_PLAN_V2_TOKENS = 2000
MAX_TOKENS_BY_KEY = {"PLAN_LLM": MAX_PLAN_TOKENS, "PLAN_V2_LLM": MAX_PLAN_V2_TOKENS,
                     "PLAN_COMPOSE_LLM": MAX_PLAN_V2_TOKENS}
# 产品入口的计划编排温度（主人 2026-09-25 确认）：同一句话每次给出不同但都合格的计划；
# 红线与时长由代码守住。需求解析、选工具与评测仍为 0，保证理解稳定、结果可复现
PRODUCT_COMPOSE_TEMPERATURE = 0.7
TEMPERATURE_BY_KEY = {"PLAN_COMPOSE_LLM": PRODUCT_COMPOSE_TEMPERATURE}


def get_llm(key: str = "LLM") -> Any:
    """LLM 用于路由、抽取与工具循环；PLAN_LLM 给 V7 计划链路，PLAN_V2_LLM 给 V10 计划 v2 与纯大模型对照。"""
    client = current_app.config.get(key)
    if client is None:
        client = LiveLLM(max_tokens=MAX_TOKENS_BY_KEY.get(key), temperature=TEMPERATURE_BY_KEY.get(key))
        current_app.config[key] = client
    return client


def get_routine_store() -> Any:
    store = current_app.config.get("ROUTINE_STORE")
    if store is None:
        store = SQLiteRoutineStore(current_app.config["DATA_PATH"])
        current_app.config["ROUTINE_STORE"] = store
    return store


def plan_llm_for(engine: str) -> Any:
    return get_llm("PLAN_LLM" if engine == "v7" else "PLAN_V2_LLM")


def plan_compose_llm_for(engine: str) -> Any:
    """只有 v2 的编排环节用产品温度；其他引擎沿用 plan_llm_for。"""
    return get_llm("PLAN_COMPOSE_LLM") if engine == "v2" else None
