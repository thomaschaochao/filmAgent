def test_health(client):
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": "1.0.0"}


def test_frontend_entrypoint(client):
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "FilmPilot" in response.text
    assert "/static/app.js?v=20260709-deepseek-research-1" in response.text
    assert "/static/styles.css?v=20260709-deepseek-research-1" in response.text
    assert "data-close-dialog" in response.text
    assert "data-action=\"open-master-agent\"" in response.text
    assert "总控智能体对话" in response.text

    static_response = client.get("/static/styles.css")
    assert static_response.status_code == 200
    assert "--ink" in static_response.text

    app_response = client.get("/static/app.js")
    assert app_response.status_code == 200
    assert "data-copy-asset-prompt" in app_response.text
    assert "generate-all-asset-prompts" in app_response.text
    assert "data-preview-asset-prompt" in app_response.text
    assert "formatPromptPreview" in app_response.text
    assert "delete-selected-assets" in app_response.text
    assert "select-all-assets" in app_response.text
    assert "data-delete-shot" in app_response.text
    assert "renderMetrics" in app_response.text
    assert "data-prompt-default-mode" in app_response.text
    assert "data-shot-prompt-mode" in app_response.text
    assert "data-copy-frame" in app_response.text
    assert "data-copy-director-overhead" in app_response.text
    assert "director_overhead" in app_response.text
    assert "openChat" in app_response.text
    assert "data-chat-apply" in app_response.text
    assert "chatTargetButton ? null" in app_response.text
    assert 'query.set("scope", targetId ? "object" : "page")' in app_response.text
    assert "dialogueTextarea" in app_response.text
    assert 'id="chat-drawer"' in response.text
    assert 'id="chat-global-thread"' in response.text
    assert 'data-action="open-metrics"' in response.text
    assert 'data-step="metrics"' not in response.text
    assert "运行监控与 Agent 评估" in app_response.text
    assert "agent-run-dialog" in response.text
    assert "openAgentRunDetail" in app_response.text
    assert 'data-action="crew-preflight"' in app_response.text
    assert "runCrewPreflight" in app_response.text


def test_project_and_script_workflow(client):
    project_response = client.post(
        "/api/v1/projects",
        json={
            "name": "测试短片",
            "visual_style": "黑白铅笔故事板",
            "world_setting": "故事发生在 1980 年代美国西海岸",
        },
    )
    assert project_response.status_code == 201
    project = project_response.json()
    assert project["world_setting"] == "故事发生在 1980 年代美国西海岸"

    update_response = client.patch(
        f"/api/v1/projects/{project['id']}",
        json={"world_setting": "故事发生在现代美国阿拉斯加"},
    )
    assert update_response.status_code == 200
    project = update_response.json()
    assert project["world_setting"] == "故事发生在现代美国阿拉斯加"

    script_response = client.post(
        f"/api/v1/projects/{project['id']}/scripts",
        json={"title": "车站", "content": "夜。女孩独自站在空旷的月台。"},
    )
    assert script_response.status_code == 201
    script = script_response.json()
    assert script["version"] == 1
    assert script["is_approved"] is False

    scripts_response = client.get(f"/api/v1/projects/{project['id']}/scripts")
    assert scripts_response.status_code == 200
    assert scripts_response.json()[0]["id"] == script["id"]

    script_detail_response = client.get(f"/api/v1/scripts/{script['id']}")
    assert script_detail_response.status_code == 200
    assert script_detail_response.json()["title"] == script["title"]

    approve_response = client.post(f"/api/v1/scripts/{script['id']}/approve")
    assert approve_response.status_code == 200
    assert approve_response.json()["is_approved"] is True


def test_missing_project_returns_404(client):
    response = client.get("/api/v1/projects/not-found")
    assert response.status_code == 404


def test_chat_proposal_apply_and_revert(client):
    project = client.post("/api/v1/projects", json={"name": "Chat Test"}).json()
    asset = client.post(
        f"/api/v1/projects/{project['id']}/assets",
        json={"asset_type": "character", "name": "Hero", "description": "Old"},
    ).json()
    thread_response = client.post(
        f"/api/v1/projects/{project['id']}/chat/threads",
        json={
            "page": "assets",
            "scope": "object",
            "target_type": "asset",
            "target_id": asset["id"],
            "title": "Edit Hero",
        },
    )
    assert thread_response.status_code == 201
    thread = thread_response.json()

    message_response = client.post(
        f"/api/v1/chat/threads/{thread['id']}/messages",
        json={
            "content": "Make the description more specific",
            "proposal": {
                "summary": "Update Hero description",
                "operations": [
                    {
                        "action": "update",
                        "resource": "asset",
                        "target_id": asset["id"],
                        "values": {"description": "Specific"},
                    }
                ],
            },
        },
    )
    assert message_response.status_code == 200
    detail = message_response.json()
    assert len(detail["messages"]) == 2
    proposal = detail["proposals"][0]
    assert proposal["status"] == "draft"

    apply_response = client.post(f"/api/v1/chat/proposals/{proposal['id']}/apply")
    assert apply_response.status_code == 200
    assert apply_response.json()["status"] == "applied"
    assert (
        client.get(f"/api/v1/projects/{project['id']}/assets").json()[0]["description"]
        == "Specific"
    )

    revert_response = client.post(f"/api/v1/chat/proposals/{proposal['id']}/revert")
    assert revert_response.status_code == 200
    assert revert_response.json()["status"] == "reverted"
    assert client.get(f"/api/v1/projects/{project['id']}/assets").json()[0]["description"] == "Old"


def test_object_chat_rejects_cross_object_proposal(client):
    project = client.post("/api/v1/projects", json={"name": "Scoped Chat"}).json()
    first = client.post(
        f"/api/v1/projects/{project['id']}/assets",
        json={"asset_type": "character", "name": "First"},
    ).json()
    second = client.post(
        f"/api/v1/projects/{project['id']}/assets",
        json={"asset_type": "character", "name": "Second"},
    ).json()
    thread = client.post(
        f"/api/v1/projects/{project['id']}/chat/threads",
        json={
            "page": "assets",
            "scope": "object",
            "target_type": "asset",
            "target_id": first["id"],
        },
    ).json()
    response = client.post(
        f"/api/v1/chat/threads/{thread['id']}/messages",
        json={
            "content": "Edit another asset",
            "proposal": {
                "summary": "Out of scope",
                "operations": [
                    {
                        "action": "update",
                        "resource": "asset",
                        "target_id": second["id"],
                        "values": {"description": "No"},
                    }
                ],
            },
        },
    )
    assert response.status_code == 422


def test_page_chat_can_create_and_revert_asset(client):
    project = client.post("/api/v1/projects", json={"name": "Create Asset Chat"}).json()
    thread = client.post(
        f"/api/v1/projects/{project['id']}/chat/threads",
        json={"page": "assets", "scope": "page"},
    ).json()
    detail = client.post(
        f"/api/v1/chat/threads/{thread['id']}/messages",
        json={
            "content": "Add a prop",
            "proposal": {
                "summary": "Add ticket",
                "operations": [
                    {
                        "action": "create",
                        "resource": "asset",
                        "values": {
                            "asset_type": "prop",
                            "name": "Ticket",
                            "description": "Paper ticket",
                        },
                    }
                ],
            },
        },
    ).json()
    proposal_id = detail["proposals"][0]["id"]
    assert client.post(f"/api/v1/chat/proposals/{proposal_id}/apply").status_code == 200
    assert [
        item["name"] for item in client.get(f"/api/v1/projects/{project['id']}/assets").json()
    ] == ["Ticket"]
    assert client.post(f"/api/v1/chat/proposals/{proposal_id}/revert").status_code == 200
    assert client.get(f"/api/v1/projects/{project['id']}/assets").json() == []


def test_global_asset_chat_receives_all_editable_assets(client, monkeypatch):
    from app.schemas import ChatAssistantDraft

    project = client.post("/api/v1/projects", json={"name": "Global Asset Chat"}).json()
    assets = []
    for name, description in [("Hero", "Lead"), ("Car", "Red coupe")]:
        assets.append(
            client.post(
                f"/api/v1/projects/{project['id']}/assets",
                json={
                    "asset_type": "character" if name == "Hero" else "prop",
                    "name": name,
                    "description": description,
                },
            ).json()
        )
    thread = client.post(
        f"/api/v1/projects/{project['id']}/chat/threads",
        json={"page": "assets", "scope": "page"},
    ).json()
    client.post(
        f"/api/v1/projects/{project['id']}/chat/threads",
        json={
            "page": "assets",
            "scope": "object",
            "target_type": "asset",
            "target_id": assets[0]["id"],
        },
    )
    page_threads = client.get(
        f"/api/v1/projects/{project['id']}/chat/threads?page=assets&scope=page"
    ).json()
    assert [item["id"] for item in page_threads] == [thread["id"]]
    captured = {}

    def fake_generate(*args, **kwargs):
        captured.update(kwargs["context"])
        return ChatAssistantDraft(reply="Ready for global edits")

    monkeypatch.setattr("app.main.generate_chat_draft", fake_generate)
    response = client.post(
        f"/api/v1/chat/threads/{thread['id']}/messages",
        json={"content": "Make all assets consistent"},
    )
    assert response.status_code == 200
    assert captured["scope"] == "page"
    assert "multiple listed items" in captured["scope_rules"]
    assert {item["name"] for item in captured["items"]} == {"Hero", "Car"}
    assert {item["description"] for item in captured["items"]} == {"Lead", "Red coupe"}


def test_chat_script_edit_creates_version_and_conflicts_do_not_overwrite(client):
    project = client.post("/api/v1/projects", json={"name": "Version Chat"}).json()
    script = client.post(
        f"/api/v1/projects/{project['id']}/scripts",
        json={"title": "V1", "content": "Original"},
    ).json()
    script_thread = client.post(
        f"/api/v1/projects/{project['id']}/chat/threads",
        json={
            "page": "script",
            "scope": "object",
            "target_type": "script",
            "target_id": script["id"],
        },
    ).json()
    detail = client.post(
        f"/api/v1/chat/threads/{script_thread['id']}/messages",
        json={
            "content": "Rewrite",
            "proposal": {
                "summary": "Create V2",
                "operations": [
                    {
                        "action": "create_version",
                        "resource": "script",
                        "target_id": script["id"],
                        "values": {"title": "V2", "content": "Rewritten"},
                    }
                ],
            },
        },
    ).json()
    proposal_id = detail["proposals"][0]["id"]
    assert client.post(f"/api/v1/chat/proposals/{proposal_id}/apply").status_code == 200
    scripts = client.get(f"/api/v1/projects/{project['id']}/scripts").json()
    assert [item["version"] for item in scripts] == [2, 1]
    assert client.post(f"/api/v1/chat/proposals/{proposal_id}/revert").status_code == 200

    asset = client.post(
        f"/api/v1/projects/{project['id']}/assets",
        json={"asset_type": "character", "name": "Hero", "description": "Old"},
    ).json()
    asset_thread = client.post(
        f"/api/v1/projects/{project['id']}/chat/threads",
        json={
            "page": "assets",
            "scope": "object",
            "target_type": "asset",
            "target_id": asset["id"],
        },
    ).json()
    detail = client.post(
        f"/api/v1/chat/threads/{asset_thread['id']}/messages",
        json={
            "content": "Edit",
            "proposal": {
                "summary": "Change description",
                "operations": [
                    {
                        "action": "update",
                        "resource": "asset",
                        "target_id": asset["id"],
                        "values": {"description": "AI edit"},
                    }
                ],
            },
        },
    ).json()
    client.patch(f"/api/v1/assets/{asset['id']}", json={"description": "Manual edit"})
    conflict = client.post(f"/api/v1/chat/proposals/{detail['proposals'][0]['id']}/apply")
    assert conflict.status_code == 409
    current = client.get(f"/api/v1/projects/{project['id']}/assets").json()[0]
    assert current["description"] == "Manual edit"


def test_asset_crud_and_reference_image_upload(client):
    project = client.post("/api/v1/projects", json={"name": "Asset Test"}).json()
    create_response = client.post(
        f"/api/v1/projects/{project['id']}/assets",
        json={
            "asset_type": "character",
            "name": "林深",
            "description": "年轻的飞行员",
        },
    )
    assert create_response.status_code == 201
    asset = create_response.json()

    duplicate_response = client.post(
        f"/api/v1/projects/{project['id']}/assets",
        json={
            "asset_type": "character",
            "name": "@林深（青年）",
            "description": "同一人物的另一种写法",
        },
    )
    assert duplicate_response.status_code == 409

    update_response = client.patch(
        f"/api/v1/assets/{asset['id']}",
        json={"prompt": "1980s American airline pilot uniform"},
    )
    assert update_response.status_code == 200
    assert update_response.json()["prompt"].startswith("1980s")

    upload_response = client.put(
        f"/api/v1/assets/{asset['id']}/image",
        content=b"fake-png-content",
        headers={"content-type": "image/png"},
    )
    assert upload_response.status_code == 200
    assert upload_response.json()["image_url"].startswith("/storage/projects/")

    list_response = client.get(f"/api/v1/projects/{project['id']}/assets")
    assert list_response.status_code == 200
    assert list_response.json()[0]["name"] == "林深"

    delete_response = client.delete(f"/api/v1/assets/{asset['id']}")
    assert delete_response.status_code == 204


def test_delete_shot_reorders_remaining_shots(client, monkeypatch):
    from app.schemas import (
        PromptDraft,
        SceneDraft,
        ShotDraft,
        StoryboardDraft,
        StoryboardFrameDraft,
    )

    project = client.post("/api/v1/projects", json={"name": "Shot Test"}).json()
    script = client.post(
        f"/api/v1/projects/{project['id']}/scripts",
        json={"title": "Test", "content": "Two shots"},
    ).json()
    client.post(f"/api/v1/scripts/{script['id']}/approve")

    storyboard = StoryboardDraft(
        scenes=[
            SceneDraft(
                sequence=1,
                heading="Scene 1",
                shots=[
                    ShotDraft(
                        sequence=1,
                        script_reference="Two shots",
                        subject="A",
                        action="walks",
                        environment="station",
                        shot_size="wide",
                        camera_angle="eye level",
                        camera_motion="static",
                    ),
                    ShotDraft(
                        sequence=2,
                        script_reference="Two shots",
                        subject="B",
                        action="waits",
                        environment="station",
                        shot_size="medium",
                        camera_angle="eye level",
                        camera_motion="static",
                    ),
                    ShotDraft(
                        sequence=3,
                        script_reference="Two shots",
                        subject="C",
                        action="looks up",
                        environment="station",
                        shot_size="close-up",
                        camera_angle="low angle",
                        camera_motion="static",
                    ),
                    ShotDraft(
                        sequence=4,
                        script_reference="Two shots",
                        subject="D",
                        action="leaves",
                        environment="station",
                        shot_size="wide",
                        camera_angle="eye level",
                        camera_motion="pan",
                    ),
                ],
            )
        ]
    )
    monkeypatch.setattr("app.main.generate_storyboard", lambda *args: storyboard)
    scenes = client.post(f"/api/v1/scripts/{script['id']}/shots/generate").json()

    monkeypatch.setattr(
        "app.main.generate_prompt",
        lambda *args, **kwargs: PromptDraft(
            positive_prompt="existing cinematic storyboard prompt",
            subject_position="at mark",
            action_constraints="only specified action",
            spatial_constraints="inside environment",
            camera_strategy="close on key action",
        ),
    )
    prompt_response = client.post(f"/api/v1/shots/{scenes[0]['shots'][0]['id']}/prompts/generate")
    assert prompt_response.status_code == 200
    prompt_metadata = prompt_response.json()["prompt_metadata"]
    assert prompt_metadata["mode"] == "initial_frame"
    assert prompt_metadata["strategy"]["recommended_mode"] in {"initial_frame", "storyboard"}
    assert "keep_spatial_boundaries_respected" in prompt_metadata["strategy"][
        "continuity_constraints"
    ]

    frames = [
        StoryboardFrameDraft(
            index=index,
            phase="start" if index == 1 else "end" if index == 4 else "middle",
            description=f"frame {index}",
        )
        for index in range(1, 5)
    ]
    monkeypatch.setattr(
        "app.main.generate_prompt",
        lambda *args, **kwargs: PromptDraft(
            positive_prompt="complete cinematic storyboard prompt",
            subject_position="at mark",
            action_constraints="only specified action",
            spatial_constraints="inside environment",
            camera_strategy="close on key action",
            frames=frames,
        ),
    )
    storyboard_response = client.post(
        f"/api/v1/shots/{scenes[0]['shots'][0]['id']}/prompts/generate",
        json={"mode": "storyboard", "frame_count": 4},
    )
    assert storyboard_response.status_code == 200
    metadata = storyboard_response.json()["prompt_metadata"]
    assert metadata["mode"] == "storyboard"
    assert metadata["frame_count"] == 4
    assert metadata["layout"] == "2x2"
    assert len(metadata["frames"]) == 4

    invalid_frames = client.post(
        f"/api/v1/shots/{scenes[0]['shots'][0]['id']}/prompts/generate",
        json={"mode": "storyboard", "frame_count": 5},
    )
    assert invalid_frames.status_code == 422

    duration_update = client.patch(
        f"/api/v1/shots/{scenes[0]['shots'][0]['id']}",
        json={"duration_seconds": 7.5},
    )
    assert duration_update.status_code == 200
    assert duration_update.json()["duration_seconds"] == 7.5
    auto_frames = [
        StoryboardFrameDraft(
            index=index,
            phase="start" if index == 1 else "end" if index == 9 else "middle",
            description=f"auto frame {index}",
        )
        for index in range(1, 10)
    ]
    monkeypatch.setattr(
        "app.main.generate_prompt",
        lambda *args, **kwargs: PromptDraft(
            positive_prompt="automatic storyboard prompt",
            subject_position="at mark",
            action_constraints="only specified action",
            spatial_constraints="inside environment",
            camera_strategy="close on key action",
            frames=auto_frames,
        ),
    )
    automatic_response = client.post(
        f"/api/v1/shots/{scenes[0]['shots'][0]['id']}/prompts/generate",
        json={"mode": "storyboard"},
    )
    assert automatic_response.status_code == 200
    automatic_metadata = automatic_response.json()["prompt_metadata"]
    assert automatic_metadata["frame_count"] == 9
    assert automatic_metadata["frame_count_source"] == "duration_auto"
    assert automatic_metadata["shot_duration_seconds"] == 7.5
    assert automatic_metadata["strategy"]["recommended_frame_count"] == 9

    delete_response = client.delete(f"/api/v1/shots/{scenes[0]['shots'][0]['id']}")
    assert delete_response.status_code == 204
    remaining = client.get(f"/api/v1/projects/{project['id']}/scenes").json()
    assert len(remaining[0]["shots"]) == 3
    assert [shot["sequence"] for shot in remaining[0]["shots"]] == [1, 2, 3]

    second_generation = client.post(f"/api/v1/scripts/{script['id']}/shots/generate")
    assert second_generation.status_code == 200
    metrics = client.get(f"/api/v1/projects/{project['id']}/agent-metrics").json()
    assert metrics["total_runs"] == 5
    assert metrics["passed_runs"] == 5
    assert metrics["regeneration_count"] == 3
    assert metrics["pass_rate"] == 100.0
    snapshots = client.get(f"/api/v1/projects/{project['id']}/storyboard-snapshots").json()
    assert [snapshot["version"] for snapshot in snapshots] == [2, 1]
    restore = client.post(f"/api/v1/storyboard-snapshots/{snapshots[-1]['id']}/restore")
    assert restore.status_code == 200
    assert len(restore.json()[0]["shots"]) == 4


def test_complex_shot_prompt_metadata_includes_director_overhead(client, monkeypatch):
    from app.schemas import PromptDraft, SceneDraft, ShotDraft, StoryboardDraft

    project = client.post(
        "/api/v1/projects",
        json={"name": "Blocking Test", "visual_style": "realistic"},
    ).json()
    script = client.post(
        f"/api/v1/projects/{project['id']}/scripts",
        json={"title": "Gate", "content": "A group crosses the airport gate."},
    ).json()
    client.post(f"/api/v1/scripts/{script['id']}/approve")
    storyboard = StoryboardDraft(
        scenes=[
            SceneDraft(
                sequence=1,
                heading="Airport gate",
                shots=[
                    ShotDraft(
                        sequence=1,
                        script_reference="A group crosses the airport gate.",
                        subject="group of passengers and Pilot",
                        action=(
                            "the group moves from the gate through the corridor "
                            "as Pilot crosses behind them"
                        ),
                        environment="airport gate to corridor",
                        shot_size="wide",
                        camera_angle="high angle",
                        camera_motion="tracking pan",
                        duration_seconds=5.5,
                    )
                ],
            )
        ]
    )
    monkeypatch.setattr("app.main.generate_storyboard", lambda *args: storyboard)
    scenes = client.post(f"/api/v1/scripts/{script['id']}/shots/generate").json()
    monkeypatch.setattr(
        "app.main.generate_prompt",
        lambda *args, **kwargs: PromptDraft(
            positive_prompt="complex blocking storyboard prompt",
            subject_position="group at the gate",
            action_constraints="only specified crossing action",
            spatial_constraints="inside airport gate and corridor",
            camera_strategy="wide tracking view",
        ),
    )

    response = client.post(f"/api/v1/shots/{scenes[0]['shots'][0]['id']}/prompts/generate")

    assert response.status_code == 200
    metadata = response.json()["prompt_metadata"]
    assert metadata["strategy"]["needs_director_overhead"] is True
    assert metadata["director_overhead"]["type"] == "director_overhead_reference"
    assert "top-down floor plan" in metadata["director_overhead"]["positive_prompt"]
    assert "movement arrows" in metadata["director_overhead"]["positive_prompt"]


def test_storyboard_business_errors_are_repaired_and_measured(client, monkeypatch):
    from app.schemas import SceneDraft, ShotDraft, StoryboardDraft

    project = client.post("/api/v1/projects", json={"name": "Validation Test"}).json()
    script = client.post(
        f"/api/v1/projects/{project['id']}/scripts",
        json={"title": "Test", "content": "女孩走进车站。"},
    ).json()
    client.post(f"/api/v1/scripts/{script['id']}/approve")
    invalid = StoryboardDraft(
        scenes=[
            SceneDraft(
                sequence=2,
                heading="Wrong sequence",
                shots=[
                    ShotDraft(
                        sequence=1,
                        script_reference="不存在的台词",
                        subject="女孩",
                        action="走路",
                        environment="车站",
                        shot_size="wide",
                        camera_angle="eye level",
                        camera_motion="static",
                    )
                ],
            )
        ]
    )
    monkeypatch.setattr("app.main.generate_storyboard", lambda *args: invalid)

    response = client.post(f"/api/v1/scripts/{script['id']}/shots/generate")
    assert response.status_code == 200
    assert response.json()[0]["sequence"] == 1
    assert response.json()[0]["shots"][0]["script_reference"] == script["content"]

    metrics = client.get(f"/api/v1/projects/{project['id']}/agent-metrics").json()
    assert metrics["total_runs"] == 1
    assert metrics["validation_failed_count"] == 0
    assert metrics["pass_rate"] == 100.0
    failed_keys = {
        item["key"]
        for item in metrics["recent_runs"][0]["validation_results"]
        if not item["passed"]
    }
    assert failed_keys == set()
    detail = client.get(f"/api/v1/agent-runs/{metrics['recent_runs'][0]['id']}").json()
    assert detail["error_message"] is None


def test_script_and_asset_operations_are_recorded(client, monkeypatch):
    from app.schemas import AssetDraft, AssetExtractionDraft

    project = client.post("/api/v1/projects", json={"name": "Metrics"}).json()

    class AuditedClient:
        def __init__(self):
            self.attempt_count = 1
            self.last_call = {
                "provider": "deepseek",
                "model": "test-model",
                "system_prompt": "system instructions",
                "user_prompt": "user screenplay request",
                "raw_response": "raw generated screenplay",
                "input_tokens": 10,
                "output_tokens": 20,
            }

    monkeypatch.setattr("app.main.DeepSeekClient", AuditedClient)
    monkeypatch.setattr(
        "app.main.generate_script",
        lambda *args, **kwargs: (
            "A complete screenplay with scenes, action, dialogue, and a visible ending."
        ),
    )
    script_response = client.post(
        f"/api/v1/projects/{project['id']}/scripts/generate",
        json={"brief": "A pilot returns home", "title": "Return"},
    )
    assert script_response.status_code == 200
    script = script_response.json()
    client.post(f"/api/v1/scripts/{script['id']}/approve")
    monkeypatch.setattr(
        "app.main.extract_assets",
        lambda *args: AssetExtractionDraft(
            assets=[
                AssetDraft(asset_type="character", name="Pilot", description="middle-aged pilot")
            ]
        ),
    )
    assert client.post(f"/api/v1/projects/{project['id']}/assets/extract").status_code == 200
    asset = client.get(f"/api/v1/projects/{project['id']}/assets").json()[0]
    monkeypatch.setattr(
        "app.main.generate_asset_prompt",
        lambda *args: "Detailed character turnaround prompt with stable visible features.",
    )
    assert client.post(f"/api/v1/assets/{asset['id']}/prompt/generate").status_code == 200

    metrics = client.get(f"/api/v1/projects/{project['id']}/agent-metrics").json()
    operations = {run["operation"] for run in metrics["recent_runs"]}
    assert {
        "script_generation",
        "asset_extraction",
        "asset_prompt_generation",
    } <= operations
    script_run = next(
        run for run in metrics["recent_runs"] if run["operation"] == "script_generation"
    )
    detail = client.get(f"/api/v1/agent-runs/{script_run['id']}").json()
    assert detail["system_prompt"] == "system instructions"
    assert detail["user_prompt"] == "user screenplay request"
    assert detail["raw_response"] == "raw generated screenplay"
    assert detail["input_tokens"] == 10
