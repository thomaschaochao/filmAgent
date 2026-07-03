import json

from pydantic import ValidationError

from app.models import ChatScope, ChatThread
from app.schemas import ChatAssistantDraft, ProposalOperation
from app.services.deepseek import DeepSeekClient, DeepSeekError


class ChatValidationError(ValueError):
    pass


def validate_operation_scope(thread: ChatThread, operation: ProposalOperation) -> None:
    if thread.scope != ChatScope.object:
        return
    if operation.resource != thread.target_type or operation.target_id != thread.target_id:
        raise ChatValidationError("operation is outside the chat thread scope")


def validate_base_version(expected: dict, current: dict) -> None:
    if expected != current:
        raise ChatValidationError("proposal base version conflicts with current data")


def generate_chat_draft(
    client: DeepSeekClient,
    *,
    context: dict,
    history: list[dict],
    instruction: str,
) -> ChatAssistantDraft:
    system = """You are FilmAgent's editing assistant. Return one JSON object only.
The object must contain `reply` and optional `proposal`. A proposal contains a short
`summary` and non-empty `operations`. Operations may only use create, update, delete,
reorder, or create_version with resources script, asset, shot, or prompt. Never invent
target IDs and never operate outside the supplied scope. If no safe edit is possible,
omit the proposal and explain why in reply."""
    user = json.dumps(
        {
            "context": context,
            "recent_messages": history[-10:],
            "instruction": instruction,
        },
        ensure_ascii=False,
    )
    try:
        return ChatAssistantDraft.model_validate(client.chat_json(system, user))
    except ValidationError as exc:
        raise DeepSeekError("DeepSeek returned an invalid chat proposal.") from exc
