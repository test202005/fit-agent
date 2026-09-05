import json

import pytest

from backend import prompt_registry


def test_all_registered_prompts_are_loadable():
    manifest = json.loads(prompt_registry.MANIFEST_PATH.read_text(encoding="utf-8"))
    assert set(manifest) == {"intent_router", "extractor", "query_planner", "agent_system"}
    for name, entry in manifest.items():
        asset = prompt_registry.load_prompt_asset(name)
        assert asset.name == name
        assert asset.version == entry["version"]
        assert asset.path.name == entry["file"]
        assert asset.eval_runner == entry["eval_runner"]
        assert asset.content
        assert asset.prompt_hash.startswith("sha256:")


def test_unknown_prompt_is_rejected():
    with pytest.raises(ValueError, match="unknown prompt"):
        prompt_registry.load_prompt_asset("missing")


def test_manifest_cannot_escape_prompts_directory(tmp_path, monkeypatch):
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({"unsafe": {"version": "v1", "file": "../outside.txt", "eval_runner": "x"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(prompt_registry, "PROMPTS_DIR", tmp_path / "prompts")
    monkeypatch.setattr(prompt_registry, "MANIFEST_PATH", manifest)
    with pytest.raises(ValueError, match="invalid prompt file"):
        prompt_registry.load_prompt_asset("unsafe")
