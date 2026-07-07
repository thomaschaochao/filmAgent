from __future__ import annotations

from dataclasses import asdict, dataclass

from app.models import Shot
from app.schemas import PromptMode, StoryboardFrameCount
from app.services.workflow import storyboard_frame_count_for_duration


@dataclass(frozen=True)
class PromptStrategy:
    complexity: str
    recommended_mode: PromptMode
    recommended_frame_count: StoryboardFrameCount | None
    needs_director_overhead: bool
    reasons: list[str]
    continuity_constraints: list[str]

    def model_dump(self) -> dict:
        return asdict(self)


COMPLEX_ACTION_TERMS = {
    "chase",
    "fight",
    "run",
    "running",
    "escape",
    "crash",
    "landing",
    "takeoff",
    "turns",
    "crosses",
    "moves",
    "enters",
    "exits",
    "\u8ffd",
    "\u6253\u6597",
    "\u5954\u8dd1",
    "\u9003",
    "\u5760",
    "\u964d\u843d",
    "\u8d77\u98de",
    "\u8f6c\u8eab",
    "\u7a7f\u8fc7",
    "\u8d70\u8fdb",
    "\u79bb\u5f00",
}

COMPLEX_CAMERA_TERMS = {
    "tracking",
    "dolly",
    "crane",
    "handheld",
    "pan",
    "tilt",
    "orbit",
    "\u8ddf\u62cd",
    "\u63a8\u8f68",
    "\u6447\u81c2",
    "\u624b\u6301",
    "\u6447",
    "\u73af\u7ed5",
}

MULTI_SUBJECT_TERMS = {
    " and ",
    ",",
    "&",
    "\u4e0e",
    "\u548c",
    "\u4e24\u4eba",
    "\u591a\u4eba",
    "\u4eba\u7fa4",
}

ENCLOSED_SPACE_TERMS = {
    "cockpit",
    "car",
    "elevator",
    "cabin",
    "room",
    "\u9a7e\u9a76\u8231",
    "\u8f66\u5185",
    "\u7535\u68af",
    "\u673a\u8231",
    "\u623f\u95f4",
}


def _contains_any(text: str, terms: set[str]) -> bool:
    lowered = text.casefold()
    return any(term in lowered for term in terms)


def classify_shot_prompt_strategy(shot: Shot) -> PromptStrategy:
    duration = float(shot.duration_seconds or 4.0)
    combined = " ".join(
        value or ""
        for value in (
            shot.subject,
            shot.action,
            shot.environment,
            shot.camera_motion,
            shot.continuity,
            shot.dialogue,
        )
    )
    reasons: list[str] = []
    score = 0

    if duration > 6:
        score += 3
        reasons.append("long_duration")
    elif duration > 3:
        score += 1
        reasons.append("medium_duration")

    if _contains_any(shot.action or "", COMPLEX_ACTION_TERMS):
        score += 2
        reasons.append("complex_action")
    if _contains_any(shot.camera_motion or "", COMPLEX_CAMERA_TERMS):
        score += 1
        reasons.append("moving_camera")
    if _contains_any(shot.subject or "", MULTI_SUBJECT_TERMS):
        score += 1
        reasons.append("multiple_subjects")
    if (shot.dialogue or "").strip() and score <= 1:
        reasons.append("simple_dialogue")

    complexity = "complex" if score >= 3 else "moderate" if score == 2 else "simple"
    recommended_mode: PromptMode = "storyboard" if complexity == "complex" else "initial_frame"
    recommended_frame_count = (
        storyboard_frame_count_for_duration(duration) if recommended_mode == "storyboard" else None
    )
    needs_director_overhead = complexity == "complex" and (
        "multiple_subjects" in reasons
        or "moving_camera" in reasons
        or _contains_any(combined, ENCLOSED_SPACE_TERMS)
    )
    continuity_constraints = [
        "keep_subject_position_explicit",
        "keep_action_within_script_scope",
        "keep_camera_close_for_key_actions",
        "keep_spatial_boundaries_respected",
    ]
    if needs_director_overhead:
        continuity_constraints.append("prepare_director_overhead_blocking")

    return PromptStrategy(
        complexity=complexity,
        recommended_mode=recommended_mode,
        recommended_frame_count=recommended_frame_count,
        needs_director_overhead=needs_director_overhead,
        reasons=reasons or ["static_single_action"],
        continuity_constraints=continuity_constraints,
    )
