from solo_auto.gates.export import all_completion_gates, export_gate
from solo_auto.gates.feedback import feedback_gate
from solo_auto.gates.prompt import prompt_gate


def test_gates_share_eleven_step_truth(fixture_factory) -> None:
    root = fixture_factory("exported")
    assert prompt_gate(root).ok is True
    assert feedback_gate(root).ok is True
    assert export_gate(root).ok is True
    assert all_completion_gates(root).complete is True
