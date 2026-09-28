from __future__ import annotations

import logging
import os
from pathlib import Path

from flask import Flask, jsonify, request

from backend.assistant import run_assistant
from backend.clock import SystemClock
from backend.console import bp as console_bp
from backend.deps import get_llm, get_routine_store, plan_compose_llm_for, plan_llm_for
from backend.llm import LiveLLM
from backend.pipeline import handle_message
from backend.storage import SQLiteStorage
from backend.trace import Tracer


LOGGER = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[1]
DATA_PATH = ROOT / "data" / "records.sqlite3"
TRACE_PATH = ROOT / "backend" / "logs" / "trace.jsonl"


def create_app(llm=None, storage=None, clock=None, routine_store=None, trace_path=None) -> Flask:
    """依赖全部可注入：测试不需要真实模型、真实文件、真实时间。"""
    app = Flask(__name__)
    app.config["LLM"] = llm
    app.config["STORAGE"] = storage or SQLiteStorage(DATA_PATH)
    app.config["CLOCK"] = clock or SystemClock()
    # 以下仅调试台使用；/api/chat 行为不变
    # 懒创建：只有用到调试台时才碰真实数据库文件
    app.config["ROUTINE_STORE"] = routine_store
    app.config["DATA_PATH"] = DATA_PATH
    # 统一助手的训练计划引擎：2026-09-25 主人手工对比后切为 v2；v7 仅作评测对照（PLAN_ENGINE=v7 可回退）
    app.config["PLAN_ENGINE"] = os.getenv("PLAN_ENGINE", "v2")
    app.config["TRACE_PATH"] = trace_path or TRACE_PATH
    app.register_blueprint(console_bp)

    @app.post("/api/chat")
    def chat():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or not isinstance(payload.get("text"), str):
            return jsonify({"ok": False, "error_code": "bad_request"}), 400
        user_id = payload.get("user_id", "demo-user")
        request_id = payload.get("request_id")
        if not isinstance(user_id, str) or not user_id.strip():
            return jsonify({"ok": False, "error_code": "bad_request"}), 400
        if request_id is not None and (
            not isinstance(request_id, str) or not request_id.strip()
        ):
            return jsonify({"ok": False, "error_code": "bad_request"}), 400

        client = app.config["LLM"]
        if client is None:
            # 懒创建但只建一次：避免每个请求都新起一个连接池
            client = LiveLLM()
            app.config["LLM"] = client
        try:
            result = handle_message(
                payload["text"],
                client,
                client,
                app.config["STORAGE"],
                Tracer(TRACE_PATH),
                query_llm=client,
                clock=app.config["CLOCK"],
                user_id=user_id.strip(),
                request_id=request_id.strip() if request_id else None,
            )
        except Exception:  # noqa: BLE001
            # 兜底：绝不把堆栈吐给调用方
            LOGGER.exception("unhandled error in /api/chat")
            return jsonify({"ok": False, "error_code": "internal_error"}), 500

        return jsonify(result), 200 if result.get("ok") else 400

    @app.post("/api/assistant")
    def assistant():
        """统一入口（V9）：不接收 request_id，见 docs/prd-v9-unified-assistant.md 3.1。"""
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or not isinstance(payload.get("text"), str) \
                or not payload["text"].strip():
            return jsonify({"ok": False, "error_code": "bad_request"}), 400
        user_id = payload.get("user_id", "demo-user")
        if not isinstance(user_id, str) or not user_id.strip():
            return jsonify({"ok": False, "error_code": "bad_request"}), 400
        try:
            result = run_assistant(
                payload["text"].strip(), get_llm(), plan_llm_for(app.config["PLAN_ENGINE"]),
                app.config["STORAGE"], get_routine_store(), Tracer(app.config["TRACE_PATH"]), app.config["CLOCK"],
                user_id=user_id.strip(), plan_engine=app.config["PLAN_ENGINE"],
                plan_compose_llm=plan_compose_llm_for(app.config["PLAN_ENGINE"]),
            )
        except ValueError as exc:
            return jsonify({"ok": False, "error_code": "config_error", "message": str(exc)}), 500
        except Exception:  # noqa: BLE001
            LOGGER.exception("unhandled error in /api/assistant")
            return jsonify({"ok": False, "error_code": "internal_error"}), 500
        body = {"ok": result["ok"], "trace_id": result["trace_id"],
                "reply": result["text"], "actions": result["actions"]}
        if not result["ok"]:
            body["error_code"] = result["error_code"]
        return jsonify(body), 200

    @app.get("/health")
    def health():
        return jsonify({"ok": True}), 200

    return app


if __name__ == "__main__":
    create_app().run(port=int(os.getenv("PORT", "5001")), debug=False)
