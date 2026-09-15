import json

from agentroute.transcript import parse_agent_model_request, previous_assistant_task


def test_reads_latest_assistant_final_answer(tmp_path):
    transcript = tmp_path / "rollout.jsonl"
    events = [
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "phase": "final_answer",
                "content": [{"type": "output_text", "text": "First answer"}],
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "phase": "commentary",
                "content": [{"type": "output_text", "text": "Ignore commentary"}],
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "phase": "final_answer",
                "content": [{"type": "output_text", "text": "Implement the migration"}],
            },
        },
    ]
    transcript.write_text("\n".join(json.dumps(event) for event in events) + "\n")

    assert previous_assistant_task(str(transcript)) == "Implement the migration"


def test_missing_transcript_fails_open(tmp_path):
    assert previous_assistant_task(str(tmp_path / "missing.jsonl")) is None


def test_parses_strict_agent_model_request_at_end():
    request = parse_agent_model_request(
        "This needs deeper analysis.\n\n"
        "MODEL_REQUEST: SMART\n"
        "MODEL_REQUEST_REASON: The production failure spans several services."
    )

    assert request is not None
    assert request.tier == "smart"
    assert request.reason == "The production failure spans several services."


def test_does_not_parse_request_example_inside_code_fence():
    request = parse_agent_model_request(
        "Example:\n```text\nMODEL_REQUEST: SMART\nMODEL_REQUEST_REASON: example only\n```"
    )

    assert request is None
