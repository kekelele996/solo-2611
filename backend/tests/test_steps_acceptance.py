from __future__ import annotations

import json
from pathlib import Path

from solo_auto.pipeline.steps import _sha256, derive_job_status, evaluate_steps
from solo_auto.store.state import iso_now, load_state


def _rewrite_state(root: Path, callback) -> None:
    path = root / "monitor" / "state.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    callback(data)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def test_a1_missing_state_is_blocked(tmp_path: Path) -> None:
    result = evaluate_steps(tmp_path)
    assert not result.complete
    assert result.steps[0].state == "blocked"
    assert "state.json" in result.missing[0]


def test_a2_unknown_schema_is_read_only(fixture_factory) -> None:
    root = fixture_factory("exported")
    _rewrite_state(root, lambda data: data.update(schema="solo-2611/2"))
    result = evaluate_steps(root)
    assert not result.complete
    assert "未知 schema" in result.missing[0]


def test_a3_and_a4_running_process_mapping(fixture_factory) -> None:
    root = fixture_factory("running-stale")
    envelope = load_state(root)
    assert envelope.state is not None
    assert derive_job_status(envelope.state, container_live=False) == "blocked"
    assert derive_job_status(envelope.state, container_live=True) == "running"


def test_a5_valid_session_id(fixture_factory) -> None:
    result = evaluate_steps(fixture_factory("exported"))
    assert result.steps[3].ok is True
    assert result.steps[4].ok is True


def test_a6_duplicate_session_is_blocked(fixture_factory) -> None:
    root = fixture_factory("exported")
    duplicate = {root.name: 2}
    session_id = load_state(root).state.session.sessionId  # type: ignore[union-attr]
    duplicate = {session_id: 2}
    result = evaluate_steps(root, duplicate_sessions=duplicate)
    assert result.steps[4].ok is False
    assert "重复" in result.steps[4].detail


def test_a7_short_initial_sha_fails(fixture_factory) -> None:
    root = fixture_factory("exported")
    _rewrite_state(root, lambda data: data.update(initialSha="abcdef0"))
    result = evaluate_steps(root)
    assert result.steps[0].ok is False


def test_a8_unchanged_artifact_fails(fixture_factory) -> None:
    root = fixture_factory("exported")
    initial = load_state(root).state.initialSha  # type: ignore[union-attr]
    _rewrite_state(root, lambda data: data["artifact"].update(sha=initial))
    result = evaluate_steps(root)
    assert result.steps[5].ok is False


def test_a9_recording_over_90_seconds_fails(fixture_factory) -> None:
    root = fixture_factory("exported")
    _rewrite_state(root, lambda data: data["recording"].update(durationSeconds=91))
    result = evaluate_steps(root)
    assert result.steps[8].ok is False
    assert "90" in result.steps[8].detail


def test_a10_wrong_resolution_fails(fixture_factory) -> None:
    root = fixture_factory("exported")
    _rewrite_state(root, lambda data: data["recording"].update(height=718))
    result = evaluate_steps(root)
    assert result.steps[8].ok is False
    assert "1280×720" in result.steps[8].detail


def test_a11_missing_feedback_check_fails(fixture_factory) -> None:
    root = fixture_factory("exported")
    (root / "monitor" / "feedback-check.json").unlink()
    _rewrite_state(root, lambda data: data.pop("feedbackCheck", None))
    result = evaluate_steps(root)
    assert result.steps[9].ok is False


def test_a12_feedback_without_ack_fails(fixture_factory) -> None:
    root = fixture_factory("exported")
    (root / "monitor" / "review-ack.json").unlink()
    _rewrite_state(root, lambda data: data.pop("export", None))
    result = evaluate_steps(root)
    assert result.steps[9].ok is False
    assert "人工复核" in result.steps[9].detail


def test_a13_complete_export_passes(fixture_factory) -> None:
    result = evaluate_steps(fixture_factory("exported"))
    assert result.complete is True
    assert result.steps[10].ok is True
    assert result.steps[0].as_dict()["number"] == 1


def test_web_recording_requires_chrome_cleanup(fixture_factory) -> None:
    root = fixture_factory("exported")
    _rewrite_state(root, lambda data: data["recording"].update(recordPlanMode="web", captureKind="window-id"))
    result = evaluate_steps(root)
    assert result.steps[8].ok is False
    assert "Web" in result.steps[8].detail


def test_manual_import_is_accepted_but_reported(fixture_factory) -> None:
    root = fixture_factory("exported")
    _rewrite_state(
        root,
        lambda data: data["recording"].update(captureKind="manual-import", captureBackend="manual-import"),
    )
    for name in ("window-capture", "cursor-guard"):
        path = root / "recording" / f"运行录屏-{name}.json"
        payload = {"status": "manual-import", "pointerStrategy": "none"}
        path.write_text(json.dumps(payload), encoding="utf-8")
    result = evaluate_steps(root)
    assert result.steps[8].ok is True
    assert "manual-import" in result.steps[8].detail


def test_recorded_requires_review_ack_for_job_mapping(fixture_factory) -> None:
    root = fixture_factory("exported")
    _rewrite_state(root, lambda data: data.update(status="recorded"))
    state = load_state(root).state
    assert state is not None
    assert derive_job_status(state, review_acked=False, recording_ok=True) == "recorded"
    assert derive_job_status(state, review_acked=True, recording_ok=True) == "reviewed"


def test_recording_failure_reasons_and_permalink_fallback(fixture_factory) -> None:
    root = fixture_factory("exported")
    (root / "recording" / "运行录屏.mp4").unlink()
    result = evaluate_steps(root)
    assert "不存在" in result.steps[8].detail

    video = root / "recording" / "运行录屏.mp4"
    video.write_bytes(b"video")
    _rewrite_state(
        root,
        lambda data: data["remote"].update(web="", fullName="fallback/repo"),
    )
    result = evaluate_steps(root)
    assert result.steps[6].detail.startswith("https://github.com/fallback/repo/commit/")

    _rewrite_state(root, lambda data: data["recording"].update(windowCaptureOk=False))
    assert "窗口采集" in evaluate_steps(root).steps[8].detail

    _rewrite_state(root, lambda data: data["recording"].update(windowCaptureOk=True))
    cursor_path = root / "recording" / "运行录屏-cursor-guard.json"
    cursor_path.write_text(json.dumps({"status": "bad", "pointerStrategy": "follow"}), encoding="utf-8")
    assert "capture" in evaluate_steps(root).steps[8].detail


def test_prompt_hash_drift_fails_step_three(fixture_factory) -> None:
    root = fixture_factory("exported")
    (root / "delivery" / "提示词.md").write_text("changed after validation", encoding="utf-8")
    result = evaluate_steps(root)
    assert result.steps[2].ok is False


def test_legacy_exported_task_remains_compatible_without_ack(fixture_factory) -> None:
    root = fixture_factory("exported")
    (root / "monitor" / "review-ack.json").unlink()
    result = evaluate_steps(root)
    assert result.steps[9].ok is True


def test_job_status_mapping_branches(fixture_factory) -> None:
    root = fixture_factory("exported")
    state = load_state(root).state
    assert state is not None
    state.status = "prepared"
    assert derive_job_status(state) == "authored"
    state.status = "prompt_ready"
    state.remote.initialPushed = ""
    assert derive_job_status(state) == "prompt_ok"
    state.remote.initialPushed = "set"
    assert derive_job_status(state) == "ready"
    state.status = "exported"
    assert derive_job_status(state) == "exported"
    state.status = "run_failed"
    state.attempts[0].errors = ["401 invalid token"]
    assert derive_job_status(state) == "blocked"
    state.attempts[0].errors = ["container exited"]
    assert derive_job_status(state) == "ready"
    state.status = "recorded"
    assert derive_job_status(state, recording_ok=False) == "ran"
    state.status = "unknown"
    assert derive_job_status(state) == "blocked"


def test_missing_artifacts_pointer_and_feedback_drift_branches(fixture_factory) -> None:
    root = fixture_factory("exported")
    _rewrite_state(
        root,
        lambda data: data.update(
            promptPath=str(root / "delivery"),
            artifact=None,
            remote={"fullName": ""},
            recording=None,
        ),
    )
    result = evaluate_steps(root)
    assert result.steps[2].ok is False
    assert result.steps[5].ok is False
    assert result.steps[6].ok is False
    assert result.steps[8].detail == "未录制"

    second = fixture_factory("exported", root.parent / "exported-pointer")
    cursor_path = second / "recording" / "运行录屏-cursor-guard.json"
    cursor_path.write_text(json.dumps({"status": "ok", "pointerStrategy": "follow"}), encoding="utf-8")
    assert "pointerStrategy" in evaluate_steps(second).steps[8].detail

    third = fixture_factory("exported", root.parent / "exported-feedback-drift")
    (third / "monitor" / "review-ack.json").unlink()
    (third / "delivery" / "feedback.md").write_text("changed after check", encoding="utf-8")
    assert "又被修改" in evaluate_steps(third).steps[9].detail


def test_remaining_job_status_and_iso_helpers(fixture_factory) -> None:
    root = fixture_factory("exported")
    state = load_state(root).state
    assert state is not None
    state.status = "ran"
    assert derive_job_status(state) == "ran"
    state.status = "snapshotted"
    assert derive_job_status(state) == "ran"
    assert iso_now().endswith(("+00:00", "+08:00"))
    assert _sha256(root / "delivery") == ""
