from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures" / "tasks"
FAKE_SKILL = Path(__file__).parent / "fixtures" / "skill"
os.environ.setdefault("SOLO_AUTO_SKILL_ROOT", str(FAKE_SKILL))
os.environ.setdefault("SOLO_AUTO_DISABLE_KEYCHAIN", "1")
os.environ.setdefault("SOLO_AUTO_DISPATCH__ENGINE", "v2")
os.environ.setdefault("SOLO_AUTO_HOME", str(Path(tempfile.gettempdir()) / f"solo-auto-pytest-import-{os.getpid()}"))


@pytest.fixture
def fixture_factory(tmp_path: Path):
    def materialize(name: str, destination: Path | None = None) -> Path:
        source = FIXTURES / name
        target = destination or (tmp_path / name)
        shutil.copytree(source, target)
        for path in target.rglob("*"):
            if not path.is_file() or path.suffix in {".mp4", ".xlsx"}:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            path.write_text(text.replace("${FIXTURE_ROOT}", str(target)), encoding="utf-8")
        state_path = target / "monitor" / "state.json"
        if state_path.is_file():
            state = json.loads(state_path.read_text(encoding="utf-8"))
            prompt_path = Path(str(state.get("promptPath") or ""))
            if prompt_path.is_file():
                state["promptSha256"] = hashlib.sha256(prompt_path.read_bytes()).hexdigest()
            feedback_path = target / "delivery" / "feedback.md"
            feedback_sha = hashlib.sha256(feedback_path.read_bytes()).hexdigest() if feedback_path.is_file() else ""
            if isinstance(state.get("feedbackCheck"), dict):
                state["feedbackCheck"]["sha256"] = feedback_sha
            state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            ack_path = target / "monitor" / "review-ack.json"
            if ack_path.is_file():
                ack = json.loads(ack_path.read_text(encoding="utf-8"))
                ack["feedbackSha256"] = feedback_sha
                ack_path.write_text(json.dumps(ack, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return target

    return materialize
