from app.models import ChatScope, ChatThread
from app.schemas import ProposalOperation


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
