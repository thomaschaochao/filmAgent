from app.models import Asset, Project, Shot
from app.schemas import SceneDraft, ShotDraft, StoryboardDraft
from app.services.workflow import (
    generate_asset_prompt,
    generate_prompt,
    generate_script,
    validate_storyboard,
)


class RecordingClient:
    def __init__(self) -> None:
        self.user_prompt = ""

    def chat_text(self, system_prompt: str, user_prompt: str) -> str:
        self.user_prompt = user_prompt
        return "generated script"

    def chat_json(self, system_prompt: str, user_prompt: str) -> dict:
        self.user_prompt = user_prompt
        return {
            "positive_prompt": "cinematic airport scene",
            "negative_prompt": "",
            "model_target": "generic",
            "components": {},
        }


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
        check["key"]: check
        for check in validate_storyboard(storyboard, "夜。女孩走进车站。")
    }
    assert checks["scene_sequence"]["passed"] is True
    assert checks["shot_sequence"]["passed"] is True
    assert checks["script_reference_coverage"]["passed"] is True
    assert checks["script_reference_match"]["passed"] is True
    assert checks["duplicate_shots"]["passed"] is False
    assert checks["duplicate_shots"]["value"] == 1
