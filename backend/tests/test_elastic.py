from __future__ import annotations

import json
from pathlib import Path

from solo_auto.dispatch.elastic import ElasticController, parse_retry_events
from solo_auto.settings import DispatchSettings


def _controller(tmp_path: Path, **elastic: object) -> ElasticController:
    settings = DispatchSettings(
        containers=4,
        elastic={"enabled": True, "ceiling": 6, **elastic},
    )
    return ElasticController(tmp_path / "elastic.json", settings)


def test_disabled_elastic_keeps_the_floor(tmp_path: Path) -> None:
    controller = ElasticController(
        tmp_path / "elastic.json",
        DispatchSettings(containers=4, elastic={"enabled": False, "ceiling": 6}),
    )
    assert controller.effective == 4
    assert controller.adjust(now=1_000.0, full=True, waiting=True) is None


def test_full_quiet_demand_steps_up_one_container_at_a_time(tmp_path: Path) -> None:
    controller = _controller(tmp_path, step_up_seconds=180, cooldown_seconds=180)
    first = controller.adjust(now=1_000.0, full=True, waiting=True)
    assert first is not None and (first.action, first.current) == ("up", 5)
    held = controller.adjust(now=1_100.0, full=True, waiting=True)
    assert held is None
    second = controller.adjust(now=1_180.0, full=True, waiting=True)
    assert second is not None and (second.action, second.current) == ("up", 6)
    assert controller.adjust(now=1_500.0, full=True, waiting=True) is None


def test_429_wait_share_steps_down_and_never_goes_below_floor(tmp_path: Path) -> None:
    controller = _controller(tmp_path)
    controller.state.limit = 6
    controller.observe([(1, 60.0)], now=2_000.0)
    down = controller.adjust(now=2_000.0, full=True, waiting=True)
    assert down is not None and (down.action, down.current) == ("down", 5)
    controller.observe([(1, 60.0)], now=2_200.0)
    down = controller.adjust(now=2_200.0, full=True, waiting=True)
    assert down is not None and (down.action, down.current) == ("down", 4)
    controller.observe([(1, 60.0)], now=3_000.0)
    floor = controller.adjust(now=3_000.0, full=True, waiting=True)
    assert floor is not None and floor.action == "rate_limited_at_floor"
    assert controller.effective == 4


def test_short_retry_waits_do_not_reduce_capacity(tmp_path: Path) -> None:
    controller = _controller(tmp_path)
    controller.state.limit = 5
    controller.state.last_change_at = 3_000.0
    controller.observe([(1, 0.4), (1, 0.5)], now=3_000.0)
    assert controller.adjust(now=3_000.0, full=True, waiting=True) is None
    assert controller.effective == 5


def test_a_severe_single_request_steps_down_immediately(tmp_path: Path) -> None:
    controller = _controller(tmp_path, severe_attempt=6)
    controller.state.limit = 5
    controller.note_severe_rate_limit(now=4_000.0)
    down = controller.adjust(now=4_000.0, full=True, waiting=True)
    assert down is not None and (down.action, down.current) == ("down", 4)


def test_retry_event_parser_only_accepts_structured_429s() -> None:
    payload = b"\n".join(
        [
            b'{"type":"system","subtype":"api_retry","error_status":429,"attempt":2,"retry_delay_ms":750}',
            b'{"type":"system","subtype":"api_retry","error_status":500,"attempt":2,"retry_delay_ms":900}',
            b'{"type":"assistant","message":{"content":"retry_delay_ms=9999"}}',
        ]
    )
    assert parse_retry_events(payload) == [(2, 0.75)]


def test_scan_reads_only_new_candidate_bytes(tmp_path: Path) -> None:
    controller = _controller(tmp_path)
    trace = tmp_path / "candidate.jsonl"
    trace.write_text("", encoding="utf-8")
    assert controller.scan([trace], now=5_000.0) == []
    with trace.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "type": "system",
            "subtype": "api_retry",
            "error_status": 429,
            "attempt": 3,
            "retry_delay_ms": 1250,
        }) + "\n")
    assert controller.scan([trace], now=5_001.0) == [(3, 1.25)]


def test_state_persists_and_reset_uses_the_configured_floor(tmp_path: Path) -> None:
    controller = _controller(tmp_path)
    controller.state.limit = 6
    controller._save()
    loaded = ElasticController(tmp_path / "elastic.json", controller.settings)
    assert loaded.effective == 6
    loaded.reset(from_floor=True, now=6_000.0)
    assert loaded.effective == 4
    assert json.loads((tmp_path / "elastic.json").read_text(encoding="utf-8"))["limit"] == 4
