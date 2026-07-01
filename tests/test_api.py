def test_health(client):
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_frontend_entrypoint(client):
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "FilmAgent" in response.text
    assert "/static/app.js?v=20260701-8" in response.text
    assert "/static/styles.css?v=20260701-2" in response.text
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
    assert upload_response.json()["image_url"].startswith("/storage/assets/")

    list_response = client.get(f"/api/v1/projects/{project['id']}/assets")
    assert list_response.status_code == 200
    assert list_response.json()[0]["name"] == "林深"

    delete_response = client.delete(f"/api/v1/assets/{asset['id']}")
    assert delete_response.status_code == 204
