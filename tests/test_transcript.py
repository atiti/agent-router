import json

from agentroute.transcript import previous_assistant_task


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
