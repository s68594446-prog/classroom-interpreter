from classroom_interpreter import agents


def test_all_ten_runtime_agent_interfaces_are_exported() -> None:
    names = {
        "AudioListenerAgent",
        "SegmentAgent",
        "TranscriptionAgent",
        "ContextAgent",
        "TranslationAgent",
        "SessionOrchestratorAgent",
        "SummaryAgent",
        "CleanupAgent",
        "RecoveryAgent",
        "HealthMonitorAgent",
    }
    assert names <= set(agents.__all__)

