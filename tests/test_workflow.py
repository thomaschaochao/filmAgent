import pytest

from app.models import Asset, Project, Scene, Shot
from app.schemas import AssetExtractionDraft, SceneDraft, ShotDraft, StoryboardDraft
from app.services.deepseek import DeepSeekError
from app.services.prompt_strategy import (
    build_director_overhead_prompt,
    classify_shot_prompt_strategy,
)
from app.services.workflow import (
    add_local_script_references,
    extract_assets,
    generate_asset_prompt,
    generate_prompt,
    generate_script,
    generate_storyboard,
    improve_storyboard_locally,
    storyboard_frame_count_for_duration,
    validate_storyboard,
)


class RecordingClient:
    def __init__(self) -> None:
        self.user_prompt = ""
        self.system_prompt = ""

    def chat_text(self, system_prompt: str, user_prompt: str) -> str:
        self.system_prompt = system_prompt
        self.user_prompt = user_prompt
        return (
            "A complete generated screenplay with scenes, visible action, characters, and dialogue."
        )

    def chat_json(self, system_prompt: str, user_prompt: str) -> dict:
        self.system_prompt = system_prompt
        self.user_prompt = user_prompt
        return {
            "positive_prompt": "cinematic airport scene",
            "negative_prompt": "",
            "model_target": "generic",
            "subject_position": "主体位于场景指定位置，面向关键操作对象",
            "action_constraints": "仅执行镜头中明确描述的动作，不增加位移",
            "spatial_constraints": "身体和动作保持在场景物理边界内",
            "camera_strategy": "机位靠近主体，以中近景清晰呈现关键动作",
            "components": {},
        }


class StoryboardClient(RecordingClient):
    def __init__(self, frame_count: int) -> None:
        super().__init__()
        self.frame_count = frame_count

    def chat_json(self, system_prompt: str, user_prompt: str) -> dict:
        self.system_prompt = system_prompt
        self.user_prompt = user_prompt
        return {
            "positive_prompt": "cinematic continuous action",
            "negative_prompt": "blur",
            "model_target": "generic",
            "subject_position": "主体保持在指定位置并面向动作目标",
            "action_constraints": "只推进给定动作，不添加额外行为",
            "spatial_constraints": "所有动作保持在环境物理边界内",
            "camera_strategy": "关键动作帧使用靠近主体的近景",
            "components": {},
            "frames": [
                {
                    "index": index,
                    "phase": (
                        "start" if index == 1 else "end" if index == self.frame_count else "middle"
                    ),
                    "description": f"visible action state {index}",
                }
                for index in range(1, self.frame_count + 1)
            ],
        }


class GeneratedStoryboardClient:
    def __init__(self, shots: list[dict]) -> None:
        self.shots = shots
        self.system_prompt = ""

    def chat_json(self, system_prompt: str, user_prompt: str) -> dict:
        self.system_prompt = system_prompt
        return {
            "scenes": [
                {
                    "sequence": 1,
                    "heading": "INT. ROOM",
                    "shots": self.shots,
                }
            ]
        }


def generated_shot(sequence: int, **overrides) -> dict:
    values = {
        "sequence": sequence,
        "script_reference": "A speaks",
        "subject": "A",
        "action": "speaks",
        "environment": "room",
        "shot_size": "medium",
        "camera_angle": "eye level",
        "camera_motion": "static",
        "duration_seconds": 4.0,
        "dialogue": "A: First line",
    }
    values.update(overrides)
    return values


def test_storyboard_requires_explicit_varied_durations_and_preserves_dialogue_rules():
    client = GeneratedStoryboardClient(
        [
            generated_shot(1, duration_seconds=2.0),
            generated_shot(
                2,
                duration_seconds=12.0,
                dialogue="A: First line\nB: Second line",
            ),
        ]
    )
    draft = generate_storyboard(
        client,
        Project(name="Film", visual_style="cinematic", aspect_ratio="16:9"),
        "A speaks",
    )
    assert [shot.duration_seconds for shot in draft.scenes[0].shots] == [2.0, 12.0]
    assert draft.scenes[0].shots[1].dialogue == "A: First line\nB: Second line"
    assert "不得把所有镜头统一设为4秒" in client.system_prompt
    assert "逐字保留" in client.system_prompt
    assert "8至20秒长镜头" in client.system_prompt


def test_storyboard_rejects_implicit_four_second_fallback():
    shot = generated_shot(1)
    shot.pop("duration_seconds")
    client = GeneratedStoryboardClient([shot])
    with pytest.raises(DeepSeekError, match="omitted duration_seconds"):
        generate_storyboard(
            client,
            Project(name="Film", visual_style="cinematic", aspect_ratio="16:9"),
            "A speaks",
        )


def test_world_setting_is_passed_to_script_generation():
    client = RecordingClient()
    generate_script(
        client,
        brief="A pilot prepares for a final flight.",
        title="Final Flight",
        instructions="",
        language="zh-CN",
        world_setting="1980s United States, with period-accurate aviation details",
    )
    assert "1980s United States" in client.user_prompt


def test_world_setting_is_passed_to_prompt_generation():
    client = RecordingClient()
    project = Project(
        name="Final Flight",
        visual_style="cinematic realism",
        aspect_ratio="16:9",
        world_setting="1980s United States, with period-accurate aviation details",
    )
    shot = Shot(
        sequence=1,
        subject="pilot",
        action="walks toward the aircraft",
        environment="airport apron",
        shot_size="wide shot",
        camera_angle="eye level",
        camera_motion="static",
    )
    generate_prompt(client, project, shot)
    assert "1980s United States" in client.user_prompt
    assert "首帧提示词" in client.system_prompt
    assert "起始姿态" in client.system_prompt


@pytest.mark.parametrize(("frame_count", "layout"), [(4, "2×2"), (6, "2×3"), (9, "3×3")])
def test_storyboard_prompt_has_exact_frames_layout_and_continuity(frame_count, layout):
    client = StoryboardClient(frame_count)
    project = Project(
        name="Flight",
        visual_style="cinematic realism",
        aspect_ratio="16:9",
        world_setting="modern United States",
    )
    project.assets.append(
        Asset(asset_type="character", name="林深", description="pilot", prompt="fixed pilot")
    )
    shot = Shot(
        sequence=1,
        subject="林深",
        action="walks from the terminal to the aircraft and stops",
        environment="airport apron",
        shot_size="wide shot",
        camera_angle="eye level",
        camera_motion="tracking",
    )

    result = generate_prompt(client, project, shot, mode="storyboard", frame_count=frame_count)

    assert len(result.frames) == frame_count
    assert [frame.index for frame in result.frames] == list(range(1, frame_count + 1))
    assert result.frames[0].phase == "start"
    assert result.frames[-1].phase == "end"
    assert layout in result.positive_prompt
    assert f"共 {frame_count} 格" in result.positive_prompt
    assert "@林深" in result.positive_prompt
    assert "角色漂移" in result.negative_prompt
    assert "cinematic realism" in client.user_prompt
    assert "modern United States" in client.user_prompt


def test_storyboard_prompt_rejects_missing_or_misordered_frames():
    client = StoryboardClient(4)
    client.frame_count = 3
    project = Project(name="Invalid", visual_style="realistic", aspect_ratio="16:9")
    shot = Shot(
        sequence=1,
        subject="person",
        action="walks",
        environment="street",
        shot_size="wide",
        camera_angle="eye level",
        camera_motion="static",
    )

    with pytest.raises(DeepSeekError, match="必须返回 4 个"):
        generate_prompt(client, project, shot, mode="storyboard", frame_count=4)


@pytest.mark.parametrize(
    ("duration", "expected"),
    [(0.5, 4), (3, 4), (3.5, 6), (6, 6), (6.5, 9), (30, 9)],
)
def test_storyboard_frame_count_adapts_to_shot_duration(duration, expected):
    assert storyboard_frame_count_for_duration(duration) == expected


def test_prompt_strategy_recommends_storyboard_and_director_overhead_for_complex_shot():
    shot = Shot(
        subject="Pilot and copilot",
        action="Pilot turns and reaches across the cockpit during landing",
        environment="narrow aircraft cockpit",
        camera_motion="tracking handheld pan",
        duration_seconds=8.0,
    )

    strategy = classify_shot_prompt_strategy(shot)

    assert strategy.complexity == "complex"
    assert strategy.recommended_mode == "storyboard"
    assert strategy.recommended_frame_count == 9
    assert strategy.needs_director_overhead is True
    assert "prepare_director_overhead_blocking" in strategy.continuity_constraints


def test_prompt_strategy_keeps_simple_dialogue_initial_frame_even_when_long():
    shot = Shot(
        subject="A and B",
        action="sit facing each other",
        environment="quiet room",
        camera_motion="static",
        dialogue="A: We wait.\nB: We wait.",
        duration_seconds=12.0,
    )

    strategy = classify_shot_prompt_strategy(shot)

    assert strategy.complexity == "simple"
    assert strategy.recommended_mode == "initial_frame"
    assert strategy.recommended_frame_count is None
    assert strategy.needs_director_overhead is False


def test_prompt_strategy_uses_storyboard_for_dialogue_position_change_without_overhead():
    shot = Shot(
        subject="A and B",
        action="A stands up during the conversation while B remains seated",
        environment="same apartment room",
        camera_motion="static",
        dialogue="A: I have to go.\nB: Stay.",
        duration_seconds=5.0,
    )

    strategy = classify_shot_prompt_strategy(shot)

    assert strategy.complexity == "moderate"
    assert strategy.recommended_mode == "storyboard"
    assert strategy.recommended_frame_count == 6
    assert strategy.needs_director_overhead is False
    assert "action_state_change" in strategy.reasons


def test_prompt_strategy_uses_overhead_for_group_blocking_changes():
    shot = Shot(
        subject="group of passengers and two guards",
        action="the group moves from the gate through the corridor as guards cross behind them",
        environment="airport gate to corridor",
        camera_motion="tracking pan",
        duration_seconds=5.5,
    )

    strategy = classify_shot_prompt_strategy(shot)

    assert strategy.complexity == "complex"
    assert strategy.recommended_mode == "storyboard"
    assert strategy.needs_director_overhead is True
    assert "group_subjects" in strategy.reasons
    assert "scene_change" in strategy.reasons


def test_director_overhead_prompt_is_only_built_for_complex_blocking():
    simple = Shot(
        subject="A and B",
        action="sit facing each other",
        environment="quiet room",
        camera_motion="static",
        duration_seconds=12.0,
    )
    simple_strategy = classify_shot_prompt_strategy(simple)
    assert build_director_overhead_prompt(simple, simple_strategy) is None

    project = Project(name="Airport", visual_style="realistic")
    project.assets.append(Asset(asset_type="character", name="Pilot", description="pilot"))
    scene = Scene(project=project, heading="Gate", sequence=1)
    complex_shot = Shot(
        scene=scene,
        subject="group of passengers and Pilot",
        action="the group moves from the gate through the corridor as Pilot crosses behind them",
        environment="airport gate to corridor",
        camera_motion="tracking pan",
        duration_seconds=5.5,
    )
    complex_strategy = classify_shot_prompt_strategy(complex_shot)

    overhead = build_director_overhead_prompt(complex_shot, complex_strategy)

    assert overhead is not None
    assert overhead["type"] == "director_overhead_reference"
    assert "@Pilot" in overhead["asset_references"]
    assert "top-down floor plan" in overhead["positive_prompt"]
    assert "movement arrows" in overhead["positive_prompt"]
    assert "scene boundaries" in overhead["positive_prompt"]


def test_matching_asset_is_added_as_at_reference():
    client = RecordingClient()
    project = Project(name="Forest", visual_style="cinematic", aspect_ratio="16:9")
    project.assets.append(
        Asset(
            asset_type="character",
            name="林深",
            description="年轻的中国男性，深色风衣",
            prompt="固定角色设定",
        )
    )
    shot = Shot(
        sequence=1,
        subject="林深",
        action="走进车站",
        environment="旧车站",
        shot_size="medium shot",
        camera_angle="eye level",
        camera_motion="static",
    )
    result = generate_prompt(client, project, shot)
    assert result.positive_prompt.startswith("@林深")
    assert result.asset_references == ["@林深"]


def test_prompt_enforces_position_action_space_and_key_action_camera():
    client = RecordingClient()
    project = Project(name="Cockpit", visual_style="写实", aspect_ratio="16:9")
    shot = Shot(
        sequence=1,
        subject="飞行员",
        action="右手按下航电面板上的启动按钮",
        environment="狭窄的民航飞机驾驶舱，飞行员坐在左座",
        shot_size="medium shot",
        camera_angle="eye level",
        camera_motion="static",
    )

    result = generate_prompt(client, project, shot)

    assert "主体位置：" in result.positive_prompt
    assert "动作边界：" in result.positive_prompt
    assert "空间边界：" in result.positive_prompt
    assert "关键动作机位：" in result.positive_prompt
    assert "禁止添加新的动作" in client.system_prompt
    assert "不得穿透或超出驾驶舱" in client.system_prompt
    assert "中近景、近景或局部特写" in client.system_prompt


def test_asset_prompts_enforce_type_specific_layouts():
    project = Project(
        name="Assets",
        visual_style="cinematic neo-noir",
        aspect_ratio="16:9",
        world_setting="1980s United States",
    )
    expected = {
        "character": ("三视图", "正面", "侧面", "背面"),
        "location": ("九宫格", "3×3", "不同机位"),
        "prop": ("三视图", "正面", "侧面", "背面"),
    }
    for asset_type, required_terms in expected.items():
        client = RecordingClient()
        asset = Asset(asset_type=asset_type, name="测试资产", description="测试描述")
        prompt = generate_asset_prompt(client, project, asset)
        assert all(term in prompt for term in required_terms)
        assert all(term in client.user_prompt for term in required_terms)
        assert "cinematic neo-noir" in prompt
        assert "1980s United States" in prompt


def test_character_prompt_uses_blank_background_and_keeps_realistic_style():
    client = RecordingClient()
    project = Project(
        name="Realistic Film",
        visual_style="电影级写实风格",
        world_setting="现代美国",
    )
    asset = Asset(asset_type="character", name="飞行员", description="中年民航飞行员")

    prompt = generate_asset_prompt(client, project, asset)

    assert "纯白无缝背景或无背景" in prompt
    assert "不得出现环境、场景" in prompt
    assert "电影级写实风格" in prompt
    assert "纯白无缝背景或无背景" in client.user_prompt
    assert "电影级写实风格" in client.user_prompt


def test_storyboard_validation_reports_sequences_coverage_matches_and_duplicates():
    duplicate = ShotDraft(
        sequence=1,
        script_reference="女孩走进车站",
        subject="女孩",
        action="走进车站",
        environment="车站",
        shot_size="wide",
        camera_angle="eye level",
        camera_motion="static",
    )
    storyboard = StoryboardDraft(
        scenes=[
            SceneDraft(
                sequence=1,
                heading="车站",
                shots=[duplicate, duplicate.model_copy(update={"sequence": 2})],
            )
        ]
    )
    checks = {
        check["key"]: check for check in validate_storyboard(storyboard, "夜。女孩走进车站。")
    }
    assert checks["scene_sequence"]["passed"] is True
    assert checks["shot_sequence"]["passed"] is True
    assert checks["script_reference_coverage"]["passed"] is True
    assert checks["script_reference_match"]["passed"] is True
    assert checks["duplicate_shots"]["passed"] is False
    assert checks["duplicate_shots"]["value"] == 1


def test_script_text_validation_rejects_empty_or_markdown_output():
    client = RecordingClient()
    client.chat_text = lambda *_: "```markdown\nshort\n```"
    with pytest.raises(DeepSeekError) as exc_info:
        generate_script(
            client,
            brief="A story",
            title="Title",
            instructions="",
            language="en-US",
        )
    assert exc_info.value.error_type == "output_validation"


def test_schema_validation_feedback_triggers_repair_attempt():
    class RepairingClient:
        def __init__(self):
            self.calls = []
            self.attempt_count = 0

        def chat_json(self, system_prompt, user_prompt):
            self.calls.append(user_prompt)
            if len(self.calls) == 1:
                return {"assets": [{"asset_type": "invalid", "name": "A"}]}
            return {
                "assets": [{"asset_type": "character", "name": "A", "description": "visible face"}]
            }

    client = RepairingClient()
    project = Project(name="Repair", visual_style="cinematic")
    result = extract_assets(client, project, "A enters the room.")
    assert isinstance(result, AssetExtractionDraft)
    assert len(client.calls) == 2
    assert "上一次输出未通过 JSON Schema" in client.calls[1]


def test_schema_failure_reports_exact_field_location():
    class InvalidClient:
        attempt_count = 0

        def chat_json(self, *_):
            return {"assets": [{"asset_type": "invalid", "name": "A"}]}

    with pytest.raises(DeepSeekError) as exc_info:
        extract_assets(
            InvalidClient(),
            Project(name="Invalid", visual_style="cinematic"),
            "A enters.",
        )
    details = [item["detail"] for item in exc_info.value.validation_results]
    assert any("assets.0.asset_type" in detail for detail in details)
    assert any("assets.0.description" in detail for detail in details)


def test_local_script_references_fill_missing_and_replace_invented_values():
    payload = {
        "scenes": [
            {
                "sequence": 1,
                "heading": "车站",
                "shots": [
                    {"subject": "林深", "action": "推开车站大门"},
                    {
                        "script_reference": "模型编造的原文",
                        "subject": "苏晚",
                        "dialogue": "苏晚：你终于来了。",
                    },
                ],
            }
        ]
    }
    script = "林深推开车站大门。\n苏晚：你终于来了。"

    repaired = add_local_script_references(payload, script)

    shots = repaired["scenes"][0]["shots"]
    assert shots[0]["script_reference"] == "林深推开车站大门。"
    assert shots[1]["script_reference"] == "苏晚：你终于来了。"


def test_generate_storyboard_accepts_missing_reference_after_local_repair():
    shot = generated_shot(1)
    shot.pop("script_reference")
    client = GeneratedStoryboardClient([shot])

    draft = generate_storyboard(
        client,
        Project(name="Film", visual_style="cinematic", aspect_ratio="16:9"),
        "A speaks",
    )

    assert draft.scenes[0].shots[0].script_reference == "A speaks"


def test_local_storyboard_improvement_removes_duplicates_and_renumbers():
    duplicate = ShotDraft(
        sequence=4,
        script_reference="invented",
        subject="林深",
        action="推门",
        environment="车站",
        shot_size="wide",
        camera_angle="eye level",
        camera_motion="static",
    )
    draft = StoryboardDraft(
        scenes=[
            SceneDraft(sequence=3, heading="车站", shots=[duplicate, duplicate]),
            SceneDraft(sequence=8, heading="重复场", shots=[duplicate]),
        ]
    )

    repaired = improve_storyboard_locally(draft, "林深推开车站大门。")

    assert len(repaired.scenes) == 1
    assert repaired.scenes[0].sequence == 1
    assert len(repaired.scenes[0].shots) == 1
    assert repaired.scenes[0].shots[0].sequence == 1
    assert repaired.scenes[0].shots[0].script_reference == "林深推开车站大门。"
    assert all(item["passed"] for item in validate_storyboard(repaired, "林深推开车站大门。"))
