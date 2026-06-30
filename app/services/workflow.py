import json

from pydantic import ValidationError

from app.models import Project, Shot
from app.schemas import PromptDraft, StoryboardDraft
from app.services.deepseek import DeepSeekClient, DeepSeekError


def generate_script(
    client: DeepSeekClient,
    *,
    brief: str,
    title: str,
    instructions: str,
    language: str,
) -> str:
    system = (
        "你是一名专业影视编剧。根据用户梗概生成可用于分镜拆解的完整剧本。"
        "使用清晰的场景标题、动作、人物和对白，不要输出解释。"
    )
    user = f"标题：{title}\n语言：{language}\n故事梗概：{brief}\n额外要求：{instructions or '无'}"
    return client.chat_text(system, user)


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
        f"语言：{project.language}\n剧本：\n{script}"
    )
    try:
        return StoryboardDraft.model_validate(client.chat_json(system, user))
    except ValidationError as exc:
        raise DeepSeekError("DeepSeek storyboard output failed schema validation.") from exc


def generate_prompt(client: DeepSeekClient, project: Project, shot: Shot) -> PromptDraft:
    schema_example = PromptDraft.model_json_schema()
    system = (
        "你是一名故事板图片提示词设计师。根据结构化镜头生成单帧故事板提示词。"
        "必须输出 json 对象，不要输出 Markdown。positive_prompt 应明确人物、动作、环境、"
        "构图、机位、光线、情绪、风格和画幅；不要描述无法在单帧中表现的时间过程。"
        f"输出 JSON Schema：{json.dumps(schema_example, ensure_ascii=False)}"
    )
    user = json.dumps(
        {
            "visual_style": project.visual_style,
            "aspect_ratio": project.aspect_ratio,
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
        return PromptDraft.model_validate(client.chat_json(system, user))
    except ValidationError as exc:
        raise DeepSeekError("DeepSeek prompt output failed schema validation.") from exc
