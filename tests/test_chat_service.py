import json

import pytest

from app.services.chat import generate_chat_draft
from app.services.deepseek import DeepSeekError


class FakeChatClient:
    def __init__(self, response: dict):
        self.response = response
        self.system_prompt = ""
        self.user_prompt = ""

    def chat_json(self, system_prompt: str, user_prompt: str) -> dict:
        self.system_prompt = system_prompt
        self.user_prompt = user_prompt
        return self.response


def test_generate_chat_draft_uses_scoped_context_and_validates_output():
    client = FakeChatClient(
        {
            "reply": "I can update this asset.",
            "proposal": {
                "summary": "Update description",
                "operations": [
                    {
                        "action": "update",
                        "resource": "asset",
                        "target_id": "asset-1",
                        "values": {"description": "New"},
                    }
                ],
            },
        }
    )
    draft = generate_chat_draft(
        client,
        context={"scope": "object", "target_id": "asset-1"},
        history=[{"role": "user", "content": "Earlier"}],
        instruction="Change it",
    )
    assert draft.proposal.operations[0].target_id == "asset-1"
    sent = json.loads(client.user_prompt)
    assert sent["context"]["target_id"] == "asset-1"
    assert sent["instruction"] == "Change it"


def test_generate_chat_draft_rejects_invalid_model_shape():
    client = FakeChatClient({"reply": "", "proposal": {"summary": "empty", "operations": []}})
    with pytest.raises(DeepSeekError, match="invalid chat proposal"):
        generate_chat_draft(
            client,
            context={"scope": "page"},
            history=[],
            instruction="Change it",
        )
