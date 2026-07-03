import pytest
from pydantic import ValidationError

from app.models import ChatPage, ChatScope, ChatThread
from app.schemas import ChangeProposalDraft, ChatThreadCreate, ProposalOperation
from app.services.chat import (
    ChatValidationError,
    validate_base_version,
    validate_operation_scope,
    validate_shot_merge_operations,
)


def test_chat_thread_scope_schema_rejects_mismatched_targets():
    with pytest.raises(ValidationError):
        ChatThreadCreate(page="assets", scope="page", target_type="asset", target_id="asset-1")
    with pytest.raises(ValidationError):
        ChatThreadCreate(page="assets", scope="object", target_type="shot", target_id="shot-1")


def test_structured_proposal_rejects_empty_or_untargeted_changes():
    with pytest.raises(ValidationError):
        ChangeProposalDraft(summary="Nothing", operations=[])
    with pytest.raises(ValidationError):
        ProposalOperation(action="update", resource="asset", values={"name": "Hero"})


def test_object_thread_rejects_operations_for_other_objects():
    thread = ChatThread(
        project_id="project-1",
        page=ChatPage.assets,
        scope=ChatScope.object,
        target_type="asset",
        target_id="asset-1",
    )
    valid = ProposalOperation(
        action="update", resource="asset", target_id="asset-1", values={"name": "Hero"}
    )
    validate_operation_scope(thread, valid)

    invalid = ProposalOperation(
        action="update", resource="asset", target_id="asset-2", values={"name": "Villain"}
    )
    with pytest.raises(ChatValidationError, match="outside"):
        validate_operation_scope(thread, invalid)


def test_base_version_conflicts_are_rejected():
    validate_base_version({"updated_at": "v1"}, {"updated_at": "v1"})
    with pytest.raises(ChatValidationError, match="conflicts"):
        validate_base_version({"updated_at": "v1"}, {"updated_at": "v2"})


def test_shot_merge_requires_all_dialogue_and_combined_duration():
    snapshots = {
        "shot:keep": {
            "scene_id": "scene-1",
            "dialogue": "A: First line",
            "duration_seconds": 4.0,
        },
        "shot:remove": {
            "scene_id": "scene-1",
            "dialogue": "B: Second line",
            "duration_seconds": 5.0,
        },
    }
    delete = ProposalOperation(action="delete", resource="shot", target_id="remove")
    incomplete = ProposalOperation(
        action="update",
        resource="shot",
        target_id="keep",
        values={"dialogue": "B: Second line", "duration_seconds": 9.0},
    )
    with pytest.raises(ChatValidationError, match="every original dialogue"):
        validate_shot_merge_operations([incomplete, delete], snapshots)

    complete = incomplete.model_copy(
        update={
            "values": {
                "dialogue": "A: First line\nB: Second line",
                "duration_seconds": 9.0,
            }
        }
    )
    validate_shot_merge_operations([complete, delete], snapshots)

    too_short = complete.model_copy(
        update={"values": {**complete.values, "duration_seconds": 8.0}}
    )
    with pytest.raises(ChatValidationError, match="cannot be shorter"):
        validate_shot_merge_operations([too_short, delete], snapshots)
