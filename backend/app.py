from __future__ import annotations

import logging
import os
from pathlib import Path

from flask import Flask, jsonify, request

from backend.clock import SystemClock
from backend.llm import LiveLLM
from backend.pipeline import handle_message
from backend.storage import SQLiteStorage
from backend.trace import Tracer


LOGGER = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[1]
DATA_PATH = ROOT / "data" / "records.sqlite3"
TRACE_PATH = ROOT / "backend" / "logs" / "trace.jsonl"


def create_app(llm=None, storage=None, clock=None) -> Flask:
    """依赖全部可注入：测试不需要真实模型、真实文件、真实时间。"""
    app = Flask(__name__)
    app.config["LLM"] = llm
    app.config["STORAGE"] = storage or SQLiteStorage(DATA_PATH)
    app.config["CLOCK"] = clock or SystemClock()

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

    @app.get("/health")
    def health():
        return jsonify({"ok": True}), 200

    return app


if __name__ == "__main__":
    create_app().run(port=int(os.getenv("PORT", "5001")), debug=False)
