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


def validate_shot_merge_operations(
    operations: list[ProposalOperation],
    snapshots: dict[str, dict],
) -> None:
    updates = [
        operation
        for operation in operations
        if operation.resource == "shot" and operation.action == "update"
    ]
    deletes = [
        operation
        for operation in operations
        if operation.resource == "shot" and operation.action == "delete"
    ]
    for update in updates:
        survivor = snapshots.get(f"shot:{update.target_id}")
        if survivor is None:
            continue
        merged_sources = [
            snapshots[f"shot:{operation.target_id}"]
            for operation in deletes
            if snapshots.get(f"shot:{operation.target_id}", {}).get("scene_id")
            == survivor.get("scene_id")
        ]
        if not merged_sources:
            continue
        combined_dialogue = update.values.get("dialogue", "")
        original_dialogues = [
            snapshot.get("dialogue", "").strip()
            for snapshot in [survivor, *merged_sources]
            if snapshot.get("dialogue", "").strip()
        ]
        missing = [line for line in original_dialogues if line not in combined_dialogue]
        if missing:
            raise ChatValidationError(
                "Merged shots must preserve every original dialogue line verbatim"
            )
        minimum_duration = sum(
            float(snapshot.get("duration_seconds") or 0)
            for snapshot in [survivor, *merged_sources]
        )
        combined_duration = update.values.get("duration_seconds")
        if not isinstance(combined_duration, (int, float)) or combined_duration < minimum_duration:
            raise ChatValidationError(
                "Merged shot duration cannot be shorter than the source shots combined"
            )


def generate_chat_draft(
    client: DeepSeekClient,
    *,
    context: dict,
    history: list[dict],
    instruction: str,
) -> ChatAssistantDraft:
    system = """You are FilmPilot's editing assistant. Return one JSON object only.
The object must contain `reply` and optional `proposal`. A proposal contains a short
`summary` and non-empty `operations`. Operations may only use create, update, delete,
reorder, or create_version with resources script, asset, shot, or prompt. Never invent
target IDs and never operate outside the supplied scope. If no safe edit is possible,
omit the proposal and explain why in reply. When combining shots, update the surviving
shot and delete the redundant shot in the same proposal. Preserve every original dialogue
line verbatim with speaker labels and newlines; never summarize dialogue. Recalculate the
combined duration from all dialogue, actions, pauses, and camera movement instead of using
a fixed 4 seconds. Long continuous performances or complex blocking should normally use
8-20 seconds when appropriate."""
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
        validation_results = []
        for issue in exc.errors(include_url=False):
            location = ".".join(str(part) for part in issue.get("loc", ())) or "$"
            validation_results.append(
                {
                    "key": "chat_schema_validation",
                    "label": "AI 修改提案字段校验",
                    "passed": False,
                    "value": 0,
                    "threshold": 1,
                    "detail": f"字段 {location}：{issue.get('msg', '格式不正确')}",
                }
            )
        raise DeepSeekError(
            "DeepSeek returned an invalid chat proposal.",
            error_type="schema_validation",
            validation_results=validation_results,
        ) from exc
