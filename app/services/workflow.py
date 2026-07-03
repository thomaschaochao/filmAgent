import json
import re
import unicodedata
from collections.abc import Callable
from difflib import SequenceMatcher
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from app.models import Asset, Project, Shot
from app.schemas import (
    AssetExtractionDraft,
    PromptDraft,
    PromptMode,
    StoryboardDraft,
    StoryboardFrameCount,
)
from app.services.deepseek import DeepSeekClient, DeepSeekError

DraftT = TypeVar("DraftT", bound=BaseModel)


def _schema_validation_results(error: ValidationError, operation: str) -> list[dict]:
    results = []
    for issue in error.errors(include_url=False):
        location = ".".join(str(part) for part in issue.get("loc", ())) or "$"
        results.append(
            {
                "key": "schema_validation",
                "label": f"{operation}字段校验",
                "passed": False,
                "value": 0,
                "threshold": 1,
                "detail": f"字段 {location}：{issue.get('msg', '格式不正确')}",
            }
        )
    return results


def _validated_json(
    client: DeepSeekClient,
    system: str,
    user: str,
    model: type[DraftT],
    operation: str,
    payload_transform: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> DraftT:
    last_error: ValidationError | None = None
    current_user = user
    total_attempts = 0
    for _schema_attempt in range(1, 3):
        try:
            payload = client.chat_json(system, current_user)
            if payload_transform is not None:
                payload = payload_transform(payload)
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
        validation_results=_schema_validation_results(last_error, operation),
    ) from last_error


def _comparable_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"[^\w\u3400-\u9fff]+", "", value)


def _script_reference_candidates(script: str) -> list[str]:
    candidates: list[str] = []
    for line in script.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = re.split(r"(?<=[。！？!?；;])", line)
        candidates.extend(part.strip() for part in parts if _comparable_text(part))
    return candidates


def _best_script_reference(candidates: list[str], shot_text: str, fallback_index: int) -> str:
    normalized_shot = _comparable_text(shot_text)
    if not candidates:
        return ""

    def score(candidate: str) -> float:
        normalized_candidate = _comparable_text(candidate)
        if not normalized_candidate or not normalized_shot:
            return 0.0
        containment = 1.0 if normalized_candidate in normalized_shot else 0.0
        shared = len(set(normalized_candidate) & set(normalized_shot)) / len(
            set(normalized_candidate)
        )
        similarity = SequenceMatcher(None, normalized_candidate, normalized_shot).ratio()
        return containment * 2 + shared + similarity

    scored = [(score(candidate), index, candidate) for index, candidate in enumerate(candidates)]
    best_score, _, best_candidate = max(scored)
    if best_score > 0:
        return best_candidate
    return candidates[min(fallback_index, len(candidates) - 1)]


def add_local_script_references(payload: dict[str, Any], script: str) -> dict[str, Any]:
    """Fill missing or invented storyboard references with exact excerpts from the script."""
    candidates = _script_reference_candidates(script)
    normalized_script = _comparable_text(script)
    shot_index = 0
    for scene in payload.get("scenes") or []:
        scene_text = " ".join(
            str(scene.get(key) or "") for key in ("heading", "summary", "location", "time_of_day")
        )
        for shot in scene.get("shots") or []:
            reference = str(shot.get("script_reference") or "").strip()
            normalized_reference = _comparable_text(reference)
            if normalized_reference and normalized_reference in normalized_script:
                shot_index += 1
                continue
            shot_text = " ".join(
                [
                    scene_text,
                    *(
                        str(shot.get(key) or "")
                        for key in (
                            "subject",
                            "action",
                            "environment",
                            "dialogue",
                            "narrative_purpose",
                            "continuity",
                        )
                    ),
                ]
            )
            shot["script_reference"] = _best_script_reference(candidates, shot_text, shot_index)
            shot_index += 1
    return payload


def improve_storyboard_locally(draft: StoryboardDraft, script: str) -> StoryboardDraft:
    """Repair deterministic storyboard issues before applying business validation."""
    payload = add_local_script_references(draft.model_dump(mode="python"), script)
    repaired_scenes: list[dict[str, Any]] = []
    seen_shots: set[tuple[str, ...]] = set()
    for scene in payload.get("scenes") or []:
        repaired_shots: list[dict[str, Any]] = []
        for shot in scene.get("shots") or []:
            fingerprint = tuple(
                _comparable_text(str(shot.get(key) or ""))
                for key in ("script_reference", "subject", "action", "environment")
            )
            if fingerprint in seen_shots:
                continue
            seen_shots.add(fingerprint)
            shot["sequence"] = len(repaired_shots) + 1
            repaired_shots.append(shot)
        if not repaired_shots:
            continue
        scene["sequence"] = len(repaired_scenes) + 1
        scene["shots"] = repaired_shots
        repaired_scenes.append(scene)
    payload["scenes"] = repaired_scenes
    return StoryboardDraft.model_validate(payload)


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
            validation_results=[
                {
                    "key": "script_text",
                    "label": "剧本文本校验",
                    "passed": False,
                    "value": len(content),
                    "threshold": minimum,
                    "detail": (
                        "返回中包含 Markdown 代码块"
                        if "```" in content
                        else f"返回仅 {len(content)} 字符，低于最低要求 {minimum}"
                    ),
                }
            ],
        )
    return content


def generate_storyboard(client: DeepSeekClient, project: Project, script: str) -> StoryboardDraft:
    schema_example = StoryboardDraft.model_json_schema()
    shot_schema = schema_example.get("$defs", {}).get("ShotDraft", {})
    required_fields = shot_schema.setdefault("required", [])
    if "duration_seconds" not in required_fields:
        required_fields.append("duration_seconds")
    system = (
        "你是一名电影分镜导演。把剧本拆分为场次和可独立绘制的镜头。"
        "必须输出 json 对象，不要输出 Markdown。每个镜头都要保持人物、服装、道具、空间连续性。"
        "dialogue 必须逐字保留该镜头覆盖的全部对白，保留说话人和换行；"
        "不得摘要、改写或只留下最后一句。"
        "每个镜头必须填写 duration_seconds，根据对白长度、动作复杂度和镜头运动估算0.5至60秒的时长；"
        "不得把所有镜头统一设为4秒。短反应镜头通常1至3秒，单一动作通常3至6秒；"
        "完整对白、连续表演、复杂调度、空间展示或情绪停顿应使用8至20秒长镜头，必要时可更长。"
        "对白时长按实际朗读时间估算，并额外保留表演反应与停顿；叙事允许时，每个主要场次至少考虑一个长镜头，"
        "但不要为了满足数量强行拉长镜头。不要把一句连续对白机械切成多个4秒镜头。"
        "镜头 sequence 在每个场次内从 1 连续递增，场次 sequence 从 1 连续递增。"
        f"输出 JSON Schema：{json.dumps(schema_example, ensure_ascii=False)}"
    )
    user = (
        f"项目风格：{project.visual_style}\n画幅：{project.aspect_ratio}\n"
        f"语言：{project.language}\n世界与地域设定：{project.world_setting or '未指定'}\n"
        f"剧本：\n{script}"
    )
    draft = _validated_json(
        client,
        system,
        user,
        StoryboardDraft,
        "storyboard",
        payload_transform=lambda payload: add_local_script_references(payload, script),
    )
    missing_duration = [
        (scene.sequence, shot.sequence)
        for scene in draft.scenes
        for shot in scene.shots
        if "duration_seconds" not in shot.model_fields_set
    ]
    if missing_duration:
        raise DeepSeekError(
            f"DeepSeek omitted duration_seconds for shots: {missing_duration}",
            error_type="output_validation",
            validation_results=[
                {
                    "key": "duration_seconds",
                    "label": "镜头时长校验",
                    "passed": False,
                    "value": len(missing_duration),
                    "threshold": 0,
                    "detail": f"缺少 duration_seconds 的镜头：{missing_duration}",
                }
            ],
        )
    return draft


def storyboard_frame_count_for_duration(duration_seconds: float) -> StoryboardFrameCount:
    if duration_seconds <= 3:
        return 4
    if duration_seconds <= 6:
        return 6
    return 9


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
            validation_results=[
                {
                    "key": "asset_prompt_text",
                    "label": "资产提示词文本校验",
                    "passed": False,
                    "value": len(generated),
                    "threshold": minimum,
                    "detail": (
                        "返回中包含 Markdown 代码块"
                        if "```" in generated
                        else f"返回仅 {len(generated)} 字符，低于最低要求 {minimum}"
                    ),
                }
            ],
        )
    return f"{layout_requirement}。{project_context}。{generated}"


def generate_prompt(
    client: DeepSeekClient,
    project: Project,
    shot: Shot,
    *,
    mode: PromptMode = "initial_frame",
    frame_count: StoryboardFrameCount = 6,
) -> PromptDraft:
    schema_example = PromptDraft.model_json_schema()
    duration_seconds = shot.duration_seconds or 4.0
    layouts = {4: "2×2", 6: "2×3", 9: "3×3"}
    if mode == "storyboard":
        mode_instruction = (
            f"生成一张 {layouts[frame_count]} 网格、共 {frame_count} 格的连续帧故事板提示词。"
            "frames 必须严格按从左到右、从上到下排列，index 从 1 连续编号；"
            "第一帧 phase=start 且表现镜头起始状态，最后一帧 phase=end 且表现镜头结束状态，"
            "其余帧 phase=middle 并均匀推进人物动作与镜头运动。每帧必须有可见差异，"
            "同时保持人物身份、服装、道具、场景结构、光线、画风和空间轴线完全连续。"
            "positive_prompt 描述整版共用的视觉要求；frames 描述每一格的具体画面。"
        )
    else:
        mode_instruction = (
            "生成用于视频生成的首帧提示词。只表现镜头动作开始前或开始瞬间的单张画面；"
            "人物使用起始姿态，镜头运动使用初始机位，不得描述动作完成后的状态或连续时间过程。"
            "frames 必须返回空数组。"
        )
    system = (
        "你是一名影视镜头图片提示词设计师。"
        f"{mode_instruction}"
        "必须输出 json 对象，不要输出 Markdown。positive_prompt 应明确人物、动作、环境、"
        "构图、机位、光线、情绪、项目风格和画幅。"
        "subject_position 必须明确主体在场景内的具体位置、朝向、坐姿或站姿及其与关键物体的关系；"
        "action_constraints 必须把动作限制在 shot.action 明确要求的范围内，禁止添加新的动作、"
        "位移、转身、离座、奔跑或交互；spatial_constraints 必须描述场景的物理边界和可活动范围，"
        "人物身体、四肢、道具和动作不得穿透或超出驾驶舱、车厢、电梯、房间等封闭空间，"
        "动作幅度必须符合空间尺寸和人体工学；camera_strategy 必须说明如何让关键动作清晰可读，"
        "关键动作或手部操作应使用靠近人物的中近景、近景或局部特写，同时保持轴线、视线和空间关系。"
        "不得为了画面更戏剧化而扩大动作幅度或改变人物位置。"
        "如果 asset_catalog 中的资产出现在镜头中，必须原样使用其 @引用名称。"
        f"输出 JSON Schema：{json.dumps(schema_example, ensure_ascii=False)}"
    )
    user = json.dumps(
        {
            "visual_style": project.visual_style,
            "aspect_ratio": project.aspect_ratio,
            "world_setting": project.world_setting,
            "prompt_mode": mode,
            "storyboard_frame_count": frame_count if mode == "storyboard" else None,
            "storyboard_layout": layouts[frame_count] if mode == "storyboard" else None,
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
            "duration_seconds": duration_seconds,
            "hard_requirements": {
                "subject_position": "明确主体在 environment 内的位置、朝向、姿态和相对物体关系",
                "action_scope": "只允许 action 中明确描述的动作，不得自行增加动作或位移",
                "environment_boundary": (
                    "主体和动作必须完全位于 environment 的合理物理边界内；"
                    "封闭空间内禁止离座、穿透、伸出舱体或超出可活动范围，除非 action 明确要求"
                ),
                "key_action_camera": (
                    "关键动作必须在画面中清晰可见；机位靠近主体或动作部位，"
                    "使用中近景、近景或局部特写，并保持轴线和空间连续"
                ),
            },
            "emotion": shot.emotion,
            "lighting": shot.lighting,
            "continuity": shot.continuity,
        },
        ensure_ascii=False,
    )
    try:
        draft = _validated_json(client, system, user, PromptDraft, "shot prompt")
        if mode == "storyboard":
            expected_indices = list(range(1, frame_count + 1))
            actual_indices = [frame.index for frame in draft.frames]
            if len(draft.frames) != frame_count or actual_indices != expected_indices:
                raise ValueError(
                    f"故事板必须返回 {frame_count} 个按 1 至 {frame_count} 连续编号的画面"
                )
            phases = [frame.phase for frame in draft.frames]
            if phases[0] != "start" or phases[-1] != "end" or any(
                phase != "middle" for phase in phases[1:-1]
            ):
                raise ValueError("故事板首帧、过渡帧和尾帧的阶段标记不正确")
        else:
            draft = draft.model_copy(update={"frames": []})
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
        positive_prompt = (
            f"{prefix}, {draft.positive_prompt}" if prefix else draft.positive_prompt
        )
        hard_constraints = (
            f"主体位置：{draft.subject_position}；"
            f"动作边界：{draft.action_constraints}；"
            f"空间边界：{draft.spatial_constraints}；"
            f"关键动作机位：{draft.camera_strategy}。"
        )
        positive_prompt = f"{positive_prompt}。{hard_constraints}"
        negative_prompt = draft.negative_prompt
        if mode == "storyboard":
            frame_lines = "；".join(
                f"第{frame.index}格（{frame.phase}）：{frame.description}"
                for frame in draft.frames
            )
            positive_prompt = (
                f"镜头时长 {duration_seconds:g} 秒，{layouts[frame_count]} 连续帧故事板，"
                f"共 {frame_count} 格，"
                f"从左到右、从上到下阅读。{positive_prompt}。{frame_lines}。"
                "所有格保持角色、服装、道具、场景、光线、画风和空间轴线一致。"
            )
            storyboard_negatives = (
                "缺帧，多余格子，重复画面，错误顺序，不规则网格，角色漂移，服装变化，"
                "场景结构变化，轴线跳跃，人物位置漂移，额外动作，动作幅度过大，"
                "离开指定位置，超出场景边界，穿透舱体或家具，文字，字幕，编号，水印"
            )
            negative_prompt = (
                f"{draft.negative_prompt}, {storyboard_negatives}"
                if draft.negative_prompt
                else storyboard_negatives
            )
        return draft.model_copy(
            update={
                "positive_prompt": positive_prompt,
                "negative_prompt": negative_prompt,
                "asset_references": references,
            }
        )
    except (ValidationError, ValueError) as exc:
        raise DeepSeekError(
            f"DeepSeek prompt output failed validation: {exc}",
            error_type="output_validation",
            validation_results=[
                {
                    "key": "prompt_business_rules",
                    "label": "镜头提示词业务校验",
                    "passed": False,
                    "value": 0,
                    "threshold": 1,
                    "detail": str(exc),
                }
            ],
        ) from exc
