def test_health(client):
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_frontend_entrypoint(client):
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "FilmAgent" in response.text
    assert "/static/app.js?v=20260702-1" in response.text
    assert "/static/styles.css?v=20260702-1" in response.text
    assert "data-close-dialog" in response.text

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
    assert "Agent 评估" in response.text
    assert "renderMetrics" in app_response.text


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
    from app.schemas import PromptDraft, SceneDraft, ShotDraft, StoryboardDraft

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
        lambda *args: PromptDraft(positive_prompt="existing prompt"),
    )
    prompt_response = client.post(
        f"/api/v1/shots/{scenes[0]['shots'][0]['id']}/prompts/generate"
    )
    assert prompt_response.status_code == 200

    delete_response = client.delete(f"/api/v1/shots/{scenes[0]['shots'][0]['id']}")
    assert delete_response.status_code == 204
    remaining = client.get(f"/api/v1/projects/{project['id']}/scenes").json()
    assert len(remaining[0]["shots"]) == 3
    assert [shot["sequence"] for shot in remaining[0]["shots"]] == [1, 2, 3]

    second_generation = client.post(f"/api/v1/scripts/{script['id']}/shots/generate")
    assert second_generation.status_code == 200
    metrics = client.get(f"/api/v1/projects/{project['id']}/agent-metrics").json()
    assert metrics["total_runs"] == 2
    assert metrics["passed_runs"] == 2
    assert metrics["regeneration_count"] == 1
    assert metrics["pass_rate"] == 100.0


def test_invalid_storyboard_is_rejected_and_measured(client, monkeypatch):
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
    assert response.status_code == 422
    assert "场次连续编号" in response.json()["detail"]
    assert client.get(f"/api/v1/projects/{project['id']}/scenes").json() == []

    metrics = client.get(f"/api/v1/projects/{project['id']}/agent-metrics").json()
    assert metrics["total_runs"] == 1
    assert metrics["validation_failed_count"] == 1
    assert metrics["pass_rate"] == 0.0
    failed_keys = {
        item["key"] for item in metrics["recent_runs"][0]["validation_results"]
        if not item["passed"]
    }
    assert {"scene_sequence", "script_reference_match"} <= failed_keys
