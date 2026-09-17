#!/usr/bin/env python3
"""Verify LiteLLM Responses API encrypted-content affinity without logging secrets."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any


def stream_response(endpoint: str, api_key: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{endpoint.rstrip('/')}/responses",
        data=json.dumps({**payload, "stream": True}).encode(),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    completed: dict[str, Any] | None = None
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            for raw_line in response:
                line = raw_line.decode("utf-8").strip()
                if not line.startswith("data: ") or line == "data: [DONE]":
                    continue
                event = json.loads(line[6:])
                if event.get("type") == "response.completed":
                    completed = event["response"]
                elif event.get("type") in {"error", "response.failed"}:
                    raise RuntimeError(json.dumps(event))
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {error.code}: {body}") from error
    if completed is None:
        raise RuntimeError("stream ended without response.completed")
    return completed


def item_ids(response: dict[str, Any]) -> list[str]:
    return [
        item_id
        for item in response.get("output", [])
        if isinstance(item, dict) and isinstance((item_id := item.get("id")), str)
    ]


def item_types(response: dict[str, Any]) -> list[str]:
    return [
        item_type
        for item in response.get("output", [])
        if isinstance(item, dict) and isinstance((item_type := item.get("type")), str)
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", required=True, help="LiteLLM base URL including /v1")
    parser.add_argument("--model", required=True)
    parser.add_argument("--effort", default="high")
    parser.add_argument("--require-affinity-item", action="store_true")
    parser.add_argument("--api-key-env", default="AGENTROUTE_AFFINITY_API_KEY")
    args = parser.parse_args()

    api_key = os.environ.get(args.api_key_env)
    if not api_key:
        parser.error(f"{args.api_key_env} is not set")

    common = {
        "model": args.model,
        "store": False,
        "include": ["reasoning.encrypted_content"],
        "reasoning": {"effort": args.effort},
        "tools": [
            {
                "type": "function",
                "name": "status_probe",
                "description": "Return a fixed test status.",
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
                "strict": True,
            }
        ],
    }
    first = stream_response(
        args.endpoint,
        api_key,
        {
            **common,
            "tool_choice": "required",
            "input": "Call status_probe once. After its result, reply with only SECOND.",
        },
    )
    first_ids = item_ids(first)
    has_affinity_item = any(item_id.startswith("encitem_") for item_id in first_ids)

    second_input = list(first.get("output", []))
    for item in first.get("output", []):
        if isinstance(item, dict) and item.get("type") == "function_call":
            second_input.append(
                {
                    "type": "function_call_output",
                    "call_id": item["call_id"],
                    "output": "status-ok",
                }
            )
    second = stream_response(
        args.endpoint,
        api_key,
        {**common, "input": second_input},
    )
    if not has_affinity_item:
        outcome = "FAIL" if args.require_affinity_item else "INCONCLUSIVE"
        print(
            f"{outcome} tool continuation passed, but the model returned no encrypted reasoning "
            f"item; types={item_types(first)}; ids={first_ids}"
        )
        return 1 if args.require_affinity_item else 0
    print(f"PASS encrypted-content affinity; first_ids={first_ids}; second_ids={item_ids(second)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
