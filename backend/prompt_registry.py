from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path


PROMPTS_DIR = Path(__file__).parent / "prompts"
MANIFEST_PATH = PROMPTS_DIR / "manifest.json"


@dataclass(frozen=True)
class PromptAsset:
    name: str
    version: str
    path: Path
    content: str
    prompt_hash: str
    eval_runner: str


def _manifest() -> dict[str, dict[str, str]]:
    data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("prompt manifest must be an object")
    return data


def prompt_path(name: str) -> Path:
    entry = _manifest().get(name)
    if not isinstance(entry, dict) or not isinstance(entry.get("file"), str):
        raise ValueError(f"unknown prompt {name!r}")
    path = PROMPTS_DIR / entry["file"]
    if path.parent != PROMPTS_DIR or not path.is_file():
        raise ValueError(f"invalid prompt file for {name!r}")
    return path


def load_prompt_asset(name: str) -> PromptAsset:
    entry = _manifest().get(name)
    if not isinstance(entry, dict):
        raise ValueError(f"unknown prompt {name!r}")
    version = entry.get("version")
    eval_runner = entry.get("eval_runner")
    if not isinstance(version, str) or not version:
        raise ValueError(f"missing version for prompt {name!r}")
    if not isinstance(eval_runner, str) or not eval_runner:
        raise ValueError(f"missing eval runner for prompt {name!r}")
    path = prompt_path(name)
    content = path.read_text(encoding="utf-8")
    prompt_hash = "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()
    return PromptAsset(name, version, path, content, prompt_hash, eval_runner)
