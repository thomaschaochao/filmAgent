def test_agent_can_plan_and_create_project_before_one_exists(client):
    created = client.post(
        "/api/v1/agent/sessions",
        json={"title": "返航", "initial_input": "飞行员林深在暴雨中返航。"},
    )
    assert created.status_code == 201
    session = created.json()
    assert session["project_id"] is None
    assert session["status"] == "clarifying"

    response = client.post(
        f"/api/v1/agent/sessions/{session['id']}/messages",
        json={
            "content": "使用这些设定生成计划。",
            "facts": {
                "name": "暴雨返航",
                "visual_style": "电影感写实",
                "world_setting": "当代美国西海岸机场，暴雨夜",
                "aspect_ratio": "16:9",
                "prompt_mode": "storyboard",
            },
        },
    )
    assert response.status_code == 200
    assert response.json()["status"] == "plan_ready"

    planned = client.post(
        f"/api/v1/agent/sessions/{session['id']}/plan",
        json={"project_spec": {}, "assumptions": []},
    )
    assert planned.status_code == 201
    plan = planned.json()
    assert plan["project_spec"]["name"] == "暴雨返航"
    assert len(plan["tasks"]) == 5

    approved = client.post(f"/api/v1/agent/plans/{plan['id']}/approve")
    assert approved.status_code == 200
    result = approved.json()
    assert result["project_id"] is not None
    assert result["current_stage"] == "assets"

    projects = client.get("/api/v1/projects").json()
    assert projects[0]["name"] == "暴雨返航"
    scripts = client.get(f"/api/v1/projects/{result['project_id']}/scripts").json()
    index = client.get(f"/api/v1/scripts/{scripts[0]['id']}/index")
    assert index.status_code == 200
    assert index.json()["chunk_count"] >= 1
    assert index.json()["embedding_status"] in {"keyword_ready", "ready"}


def test_agent_plan_approval_is_idempotent(client):
    session = client.post(
        "/api/v1/agent/sessions",
        json={"title": "测试", "initial_input": "一个足够明确的短片故事。"},
    ).json()
    client.post(
        f"/api/v1/agent/sessions/{session['id']}/messages",
        json={
            "content": "确认设定",
            "facts": {
                "name": "测试项目",
                "visual_style": "写实",
                "world_setting": "2026年上海",
                "aspect_ratio": "16:9",
                "prompt_mode": "initial_frame",
            },
        },
    )
    plan = client.post(
        f"/api/v1/agent/sessions/{session['id']}/plan", json={}
    ).json()
    first = client.post(f"/api/v1/agent/plans/{plan['id']}/approve")
    second = client.post(f"/api/v1/agent/plans/{plan['id']}/approve")
    assert first.status_code == second.status_code == 200
    assert first.json()["project_id"] == second.json()["project_id"]
    assert len(client.get("/api/v1/projects").json()) == 1


def test_retrieval_status_does_not_expose_local_paths(client):
    response = client.get("/api/v1/agent/retrieval/status")
    assert response.status_code == 200
    body = response.json()
    assert body["model"] == "BAAI/bge-m3"
    assert "path" not in body
