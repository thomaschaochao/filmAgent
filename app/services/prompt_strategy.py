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


def build_director_overhead_prompt(shot: Shot, strategy: PromptStrategy) -> dict | None:
    if not strategy.needs_director_overhead:
        return None
    asset_references = [
        f"@{asset.name}"
        for asset in shot.scene.project.assets
        if asset.name.casefold()
        in " ".join((shot.subject or "", shot.action or "", shot.dialogue or "")).casefold()
    ] if shot.scene and shot.scene.project else []
    subject = " ".join(asset_references) if asset_references else shot.subject
    prompt = (
        "Director overhead blocking reference diagram, top-down floor plan view, "
        "not a cinematic camera frame. "
        f"Subject(s): {subject}. "
        f"Environment: {shot.environment or 'current scene space'}. "
        f"Action beats to map: {shot.action or 'specified shot action'}. "
        f"Camera movement reference: {shot.camera_motion or 'static camera'}. "
        "Show exact character positions, start and end marks, movement arrows, spacing, "
        "scene boundaries, entrances/exits, major props, camera position and camera path. "
        "Use a clean production planning style with simple labels, readable arrows, "
        "consistent scale, no dramatic perspective, no character portrait rendering. "
        "The diagram is only for staging continuity and must keep all action inside the "
        "physical environment boundaries."
    )
    return {
        "type": "director_overhead_reference",
        "positive_prompt": prompt,
        "negative_prompt": (
            "cinematic perspective, portrait, close-up, dramatic lighting, text clutter, "
            "unclear arrows, inconsistent scale, impossible body positions, "
            "action outside boundaries"
        ),
        "asset_references": asset_references,
        "purpose": "blocking_continuity",
        "reasons": strategy.reasons,
    }


LARGE_ACTION_TERMS = {
    "chase",
    "fight",
    "run",
    "running",
    "escape",
    "crash",
    "explosion",
    "shoot",
    "attack",
    "landing",
    "takeoff",
    "fall",
    "jump",
    "\u8ffd",
    "\u6253\u6597",
    "\u5954\u8dd1",
    "\u9003",
    "\u5760",
    "\u7206\u70b8",
    "\u5c04\u51fb",
    "\u653b\u51fb",
    "\u964d\u843d",
    "\u8d77\u98de",
    "\u6454\u5012",
    "\u8df3",
}

ACTION_CHANGE_TERMS = {
    "walk",
    "walking",
    "stand",
    "stands",
    "stands up",
    "rise",
    "rises",
    "sit down",
    "sits down",
    "turns",
    "crosses",
    "moves",
    "enters",
    "exits",
    "reaches",
    "\u8d70",
    "\u884c\u8d70",
    "\u7ad9\u8d77",
    "\u8d77\u8eab",
    "\u5750\u4e0b",
    "\u8f6c\u8eab",
    "\u7a7f\u8fc7",
    "\u8d70\u8fdb",
    "\u79bb\u5f00",
    "\u4f38\u624b",
}

SCENE_CHANGE_TERMS = {
    "from ",
    " to ",
    "into",
    "out of",
    "through",
    "between",
    "corridor",
    "door",
    "outside",
    "inside",
    "\u4ece",
    "\u5230",
    "\u8fdb\u5165",
    "\u79bb\u5f00",
    "\u7a7f\u8fc7",
    "\u95e8",
    "\u8d70\u5eca",
    "\u5ba4\u5185",
    "\u5ba4\u5916",
    "\u573a\u666f\u53d8\u5316",
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

GROUP_SUBJECT_TERMS = {
    "\u591a\u4eba",
    "\u4eba\u7fa4",
    "\u7fa4\u6f14",
    "crowd",
    "group",
    "three",
    "four",
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
    action_text = shot.action or ""
    subject_text = shot.subject or ""
    environment_text = shot.environment or ""
    continuity_text = shot.continuity or ""
    camera_text = shot.camera_motion or ""
    has_action_change = _contains_any(action_text, ACTION_CHANGE_TERMS)
    has_scene_change = _contains_any(
        " ".join((action_text, environment_text, continuity_text)), SCENE_CHANGE_TERMS
    )
    has_large_action = _contains_any(action_text, LARGE_ACTION_TERMS)
    has_moving_camera = _contains_any(camera_text, COMPLEX_CAMERA_TERMS)
    has_multiple_subjects = _contains_any(subject_text, MULTI_SUBJECT_TERMS)
    has_group_subjects = _contains_any(subject_text, GROUP_SUBJECT_TERMS)
    has_dialogue = bool((shot.dialogue or "").strip())

    reasons: list[str] = []
    if has_large_action:
        reasons.append("large_action")
    if has_scene_change:
        reasons.append("scene_change")
    if has_action_change:
        reasons.append("action_state_change")
    if has_moving_camera:
        reasons.append("moving_camera")
    if has_group_subjects:
        reasons.append("group_subjects")
    elif has_multiple_subjects:
        reasons.append("multiple_subjects")
    if has_dialogue:
        reasons.append("dialogue")

    requires_storyboard = has_scene_change or has_action_change or has_large_action
    complex_blocking = has_group_subjects and (has_action_change or has_scene_change)
    complex_two_person_change = has_multiple_subjects and has_action_change and has_scene_change
    complex_action_scene = has_large_action or complex_blocking or (
        has_moving_camera and (has_scene_change or has_large_action)
    )
    if complex_action_scene or complex_two_person_change:
        complexity = "complex"
    elif requires_storyboard:
        complexity = "moderate"
    else:
        complexity = "simple"

    recommended_mode: PromptMode = "storyboard" if requires_storyboard else "initial_frame"
    recommended_frame_count = (
        storyboard_frame_count_for_duration(duration) if recommended_mode == "storyboard" else None
    )
    needs_director_overhead = complexity == "complex" and (
        has_group_subjects
        or has_large_action
        or has_moving_camera
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
        reasons=reasons or ["static_dialogue_or_single_action"],
        continuity_constraints=continuity_constraints,
    )
