import json
import re
import unicodedata
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from app.models import Asset, Project, Shot
from app.schemas import AssetExtractionDraft, PromptDraft, StoryboardDraft
from app.services.deepseek import DeepSeekClient, DeepSeekError

DraftT = TypeVar("DraftT", bound=BaseModel)


def _validated_json(
    client: DeepSeekClient,
    system: str,
    user: str,
    model: type[DraftT],
    operation: str,
) -> DraftT:
    last_error: ValidationError | None = None
    current_user = user
    total_attempts = 0
    for _schema_attempt in range(1, 3):
        try:
            payload = client.chat_json(system, current_user)
            total_attempts += max(getattr(client, "attempt_count", 1), 1)
            result = model.model_validate(payload)
            if hasattr(client, "last_call"):
                client.last_call["attempt_count"] = total_attempts
            client.attempt_count = total_attempts
            return result
        except ValidationError as exc:
            last_error = exc
            current_user = (
                f"{user}\n\n上一次输出未通过 JSON Schema。只修复结构和缺失字段后重新输出。"
                f"校验错误：{json.dumps(exc.errors(include_url=False), ensure_ascii=False)}"
            )
    raise DeepSeekError(
        f"DeepSeek {operation} output failed schema validation after repair.",
        error_type="schema_validation",
    ) from last_error


def _comparable_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"[^\w\u3400-\u9fff]+", "", value)


def validate_storyboard(
    draft: StoryboardDraft,
    script: str,
    *,
    reference_coverage_threshold: float = 0.8,
    reference_match_threshold: float = 0.7,
) -> list[dict]:
    """Run deterministic checks before generated storyboard data is persisted."""
    checks: list[dict] = []
    expected_scenes = list(range(1, len(draft.scenes) + 1))
    actual_scenes = [scene.sequence for scene in draft.scenes]
    checks.append(
        {
            "key": "scene_sequence",
            "label": "场次连续编号",
            "passed": actual_scenes == expected_scenes,
            "value": int(actual_scenes == expected_scenes),
            "threshold": 1,
            "detail": f"实际编号 {actual_scenes}，期望 {expected_scenes}",
        }
    )

    invalid_shot_scenes: list[int] = []
    shots = []
    for scene in draft.scenes:
        shots.extend(scene.shots)
        actual = [shot.sequence for shot in scene.shots]
        if actual != list(range(1, len(scene.shots) + 1)):
            invalid_shot_scenes.append(scene.sequence)
    checks.append(
        {
            "key": "shot_sequence",
            "label": "镜头连续编号",
            "passed": not invalid_shot_scenes,
            "value": len(draft.scenes) - len(invalid_shot_scenes),
            "threshold": len(draft.scenes),
            "detail": "全部场次镜头编号连续"
            if not invalid_shot_scenes
            else f"异常场次：{invalid_shot_scenes}",
        }
    )

    referenced = [shot for shot in shots if shot.script_reference.strip()]
    coverage = len(referenced) / len(shots) if shots else 0.0
    checks.append(
        {
            "key": "script_reference_coverage",
            "label": "剧本引用覆盖率",
            "passed": coverage >= reference_coverage_threshold,
            "value": round(coverage, 4),
            "threshold": reference_coverage_threshold,
            "detail": f"{len(referenced)}/{len(shots)} 个镜头含剧本引用",
        }
    )

    normalized_script = _comparable_text(script)
    matched = sum(
        1
        for shot in referenced
        if _comparable_text(shot.script_reference)
        and _comparable_text(shot.script_reference) in normalized_script
    )
    match_rate = matched / len(referenced) if referenced else 0.0
    checks.append(
        {
            "key": "script_reference_match",
            "label": "剧本引用匹配率",
            "passed": match_rate >= reference_match_threshold,
            "value": round(match_rate, 4),
            "threshold": reference_match_threshold,
            "detail": f"{matched}/{len(referenced)} 条引用可在原剧本中找到",
        }
    )

    seen: set[tuple[str, ...]] = set()
    duplicate_count = 0
    for shot in shots:
        fingerprint = tuple(
            _comparable_text(value)
            for value in (
                shot.script_reference,
                shot.subject,
                shot.action,
                shot.environment,
            )
        )
        if fingerprint in seen:
            duplicate_count += 1
        seen.add(fingerprint)
    checks.append(
        {
            "key": "duplicate_shots",
            "label": "重复镜头检查",
            "passed": duplicate_count == 0,
            "value": duplicate_count,
            "threshold": 0,
            "detail": f"发现 {duplicate_count} 个完全重复镜头",
        }
    )
    return checks


def generate_script(
    client: DeepSeekClient,
    *,
    brief: str,
    title: str,
    instructions: str,
    language: str,
    world_setting: str = "",
) -> str:
    system = (
        "你是一名专业影视编剧。根据用户梗概生成可用于分镜拆解的完整剧本。"
        "使用清晰的场景标题、动作、人物和对白，不要输出解释。"
    )
    user = f"标题：{title}\n语言：{language}\n故事梗概：{brief}\n额外要求：{instructions or '无'}"
    if world_setting:
        user += f"\n世界与地域设定：{world_setting}"
    content = client.chat_text(system, user).strip()
    minimum = getattr(getattr(client, "settings", None), "script_min_characters", 50)
    if len(content) < minimum or "```" in content:
        raise DeepSeekError(
            f"DeepSeek script output failed validation (minimum {minimum} characters).",
            error_type="output_validation",
        )
    return content


def generate_storyboard(client: DeepSeekClient, project: Project, script: str) -> StoryboardDraft:
    schema_example = StoryboardDraft.model_json_schema()
    system = (
        "你是一名电影分镜导演。把剧本拆分为场次和可独立绘制的镜头。"
        "必须输出 json 对象，不要输出 Markdown。每个镜头都要保持人物、服装、道具、空间连续性。"
        "镜头 sequence 在每个场次内从 1 连续递增，场次 sequence 从 1 连续递增。"
        f"输出 JSON Schema：{json.dumps(schema_example, ensure_ascii=False)}"
    )
    user = (
        f"项目风格：{project.visual_style}\n画幅：{project.aspect_ratio}\n"
        f"语言：{project.language}\n世界与地域设定：{project.world_setting or '未指定'}\n"
        f"剧本：\n{script}"
    )
    return _validated_json(client, system, user, StoryboardDraft, "storyboard")


def extract_assets(
    client: DeepSeekClient,
    project: Project,
    script: str,
) -> AssetExtractionDraft:
    schema_example = AssetExtractionDraft.model_json_schema()
    system = (
        "你是影视视觉资产统筹。请从剧本中提取需要保持视觉一致的人物、场景和关键道具。"
        "asset_type 只能是 character、location 或 prop。人物使用剧本中的正式姓名；"
        "同一人物只能输出一次，不得因年龄阶段、服装、职业状态或不同称呼拆成多个资产；"
        "场景和道具使用简短、唯一、可被镜头文本直接引用的名称。description 必须包含可视特征，"
        "并服从项目的时代、国家、地域和文化设定。只输出 JSON，不要输出 Markdown。"
        f"输出 JSON Schema：{json.dumps(schema_example, ensure_ascii=False)}"
    )
    user = (
        f"项目视觉风格：{project.visual_style}\n"
        f"世界与地域设定：{project.world_setting or '未指定'}\n"
        f"剧本：\n{script}"
    )
    return _validated_json(client, system, user, AssetExtractionDraft, "asset extraction")


def generate_asset_prompt(client: DeepSeekClient, project: Project, asset: Asset) -> str:
    layout_requirements = {
        "character": (
            "角色设定三视图，使用同一角色、同一服装、同一比例，完整展示正面、标准侧面和背面，"
            "纯白无缝背景或无背景，不得出现环境、场景、家具、建筑或装饰元素，"
            "三幅视图等距排列，不使用透视夸张"
        ),
        "location": (
            "场景设定九宫格，3×3 等尺寸分镜板，在同一场景设定和时间条件下展示九个不同机位与角度，"
            "包括正面、左右侧、俯视、仰视、远景、中景和关键细节，保持建筑与空间结构一致"
        ),
        "prop": (
            "道具设定三视图，使用同一道具、同一比例，完整展示正面、标准侧面和背面，"
            "纯净中性背景，三幅视图等距排列，材质与结构清晰"
        ),
    }
    layout_requirement = layout_requirements[asset.asset_type]
    project_context = (
        f"项目视觉风格：{project.visual_style or '未指定'}。"
        f"世界与地域设定：{project.world_setting or '未指定'}"
    )
    system = (
        "你是影视视觉资产概念设计师。为单个资产编写可用于图片生成的固定设定提示词。"
        "只描述资产本身的稳定外观、材质、时代地域特征和辨识特征，不描述具体镜头动作，"
        "必须明确继承 visual_style，不得省略写实、动画、水墨等项目风格。"
        "人物资产必须是纯白或无背景的角色设定图，禁止添加任何环境和场景；"
        "必须严格遵守指定的版式与视角要求，不要输出解释或 Markdown。"
    )
    user = json.dumps(
        {
            "asset_reference": f"@{asset.name}",
            "asset_type": asset.asset_type,
            "name": asset.name,
            "description": asset.description,
            "world_setting": project.world_setting,
            "visual_style": project.visual_style,
            "background_requirement": (
                "纯白无缝背景或无背景，禁止环境和场景元素"
                if asset.asset_type == "character"
                else "按资产类型和版式要求处理"
            ),
            "required_layout": layout_requirement,
        },
        ensure_ascii=False,
    )
    generated = client.chat_text(system, user).strip()
    minimum = getattr(getattr(client, "settings", None), "asset_prompt_min_characters", 20)
    if len(generated) < minimum or "```" in generated:
        raise DeepSeekError(
            f"DeepSeek asset prompt failed validation (minimum {minimum} characters).",
            error_type="output_validation",
        )
    return f"{layout_requirement}。{project_context}。{generated}"


def generate_prompt(client: DeepSeekClient, project: Project, shot: Shot) -> PromptDraft:
    schema_example = PromptDraft.model_json_schema()
    system = (
        "你是一名故事板图片提示词设计师。根据结构化镜头生成单帧故事板提示词。"
        "必须输出 json 对象，不要输出 Markdown。positive_prompt 应明确人物、动作、环境、"
        "构图、机位、光线、情绪、风格和画幅；不要描述无法在单帧中表现的时间过程。"
        "如果 asset_catalog 中的资产出现在镜头中，必须原样使用其 @引用名称。"
        f"输出 JSON Schema：{json.dumps(schema_example, ensure_ascii=False)}"
    )
    user = json.dumps(
        {
            "visual_style": project.visual_style,
            "aspect_ratio": project.aspect_ratio,
            "world_setting": project.world_setting,
            "asset_catalog": [
                {
                    "reference": f"@{asset.name}",
                    "type": asset.asset_type,
                    "description": asset.description,
                    "asset_prompt": asset.prompt,
                    "has_reference_image": bool(asset.image_path),
                }
                for asset in project.assets
            ],
            "subject": shot.subject,
            "action": shot.action,
            "environment": shot.environment,
            "shot_size": shot.shot_size,
            "camera_angle": shot.camera_angle,
            "camera_motion": shot.camera_motion,
            "emotion": shot.emotion,
            "lighting": shot.lighting,
            "continuity": shot.continuity,
        },
        ensure_ascii=False,
    )
    try:
        draft = _validated_json(client, system, user, PromptDraft, "shot prompt")
        shot_text = " ".join(
            value or ""
            for value in [
                shot.subject,
                shot.action,
                shot.environment,
                shot.script_reference,
                shot.dialogue,
                shot.continuity,
            ]
        ).casefold()
        references = [
            f"@{asset.name}" for asset in project.assets if asset.name.casefold() in shot_text
        ]
        prefix = " ".join(ref for ref in references if ref not in draft.positive_prompt)
        positive_prompt = f"{prefix}, {draft.positive_prompt}" if prefix else draft.positive_prompt
        return draft.model_copy(
            update={
                "positive_prompt": positive_prompt,
                "asset_references": references,
            }
        )
    except ValidationError as exc:
        raise DeepSeekError(
            "DeepSeek prompt output failed validation.", error_type="output_validation"
        ) from exc
