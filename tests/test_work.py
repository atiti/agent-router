import pytest

from agentroute.work_cli import WORK_TYPE, identity, owners


def test_canonical_work_identity():
    assert identity("https://GitHub.com/atiti/agent-router/issues/1/") == (
        "https://github.com/atiti/agent-router/issues/1"
    )
    for value in (
        "similar prompt",
        "http://example.com/1",
        "https://u:p@example.com/1",
        "https://example.com/1?secret=x",
        "local:wrong",
    ):
        with pytest.raises(ValueError):
            identity(value)


def test_owner_lookup_paginates_native_store():
    class Client:
        def request(self, method, params):
            assert method == "thread/attachmentOwner/list"
            assert params["attachmentType"] == WORK_TYPE
            assert params["identityKey"] == "https://example.com/issue/1"
            if params["cursor"] is None:
                return {"data": [{"threadId": "one", "archived": False}], "nextCursor": "next"}
            return {"data": [{"threadId": "two", "archived": True}], "nextCursor": None}

    assert owners(Client(), "https://example.com/issue/1") == [
        {"threadId": "one", "archived": False},
        {"threadId": "two", "archived": True},
    ]
