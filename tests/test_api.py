def test_health(client):
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_project_and_script_workflow(client):
    project_response = client.post(
        "/api/v1/projects",
        json={"name": "测试短片", "visual_style": "黑白铅笔故事板"},
    )
    assert project_response.status_code == 201
    project = project_response.json()

    script_response = client.post(
        f"/api/v1/projects/{project['id']}/scripts",
        json={"title": "车站", "content": "夜。女孩独自站在空旷的月台。"},
    )
    assert script_response.status_code == 201
    script = script_response.json()
    assert script["version"] == 1
    assert script["is_approved"] is False

    approve_response = client.post(f"/api/v1/scripts/{script['id']}/approve")
    assert approve_response.status_code == 200
    assert approve_response.json()["is_approved"] is True


def test_missing_project_returns_404(client):
    response = client.get("/api/v1/projects/not-found")
    assert response.status_code == 404
