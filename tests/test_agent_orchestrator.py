import app.main as main


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
    assert body["vector_backend"] == "qdrant_local"
    assert body["collection"] == "filmagent_knowledge_v1"
    assert "path" not in body


def test_keyword_retrieval_is_available_without_embedding_dependencies(client):
    project = client.post("/api/v1/projects", json={"name": "检索项目"}).json()
    script = client.post(
        f"/api/v1/projects/{project['id']}/scripts",
        json={
            "title": "返航",
            "content": (
                "内景：驾驶舱 - 夜\n\n林深握紧操纵杆。\n\n"
                "外景：跑道 - 夜\n\n跑道灯在暴雨中延伸。"
            ),
        },
    ).json()
    assert client.post(f"/api/v1/scripts/{script['id']}/index").status_code == 200

    response = client.post(
        "/api/v1/agent/retrieve",
        json={"query": "驾驶舱 林深", "project_id": project["id"]},
    )
    assert response.status_code == 200
    hits = response.json()
    assert hits
    assert "林深" in hits[0]["content"]
    assert hits[0]["source"] in {"keyword", "hybrid"}


def test_project_retrieval_uses_current_script_index(client):
    project = client.post("/api/v1/projects", json={"name": "current-index"}).json()
    old_script = client.post(
        f"/api/v1/projects/{project['id']}/scripts",
        json={"title": "old", "content": "Old scene\n\nPilot Alpha keeps the red map."},
    ).json()
    new_script = client.post(
        f"/api/v1/projects/{project['id']}/scripts",
        json={"title": "new", "content": "New scene\n\nPilot Beta keeps the blue compass."},
    ).json()
    assert client.post(f"/api/v1/scripts/{old_script['id']}/index").status_code == 200
    assert client.post(f"/api/v1/scripts/{new_script['id']}/index").status_code == 200

    response = client.post(
        "/api/v1/agent/retrieve",
        json={"query": "red map blue compass", "project_id": project["id"]},
    )
    assert response.status_code == 200
    contents = "\n".join(hit["content"] for hit in response.json())
    assert "blue compass" in contents
    assert "red map" not in contents


def test_script_index_rebuild_is_idempotent(client):
    project = client.post("/api/v1/projects", json={"name": "rebuild-index"}).json()
    script = client.post(
        f"/api/v1/projects/{project['id']}/scripts",
        json={"title": "script", "content": "Scene one\n\nA lantern glows by the runway."},
    ).json()
    first = client.post(f"/api/v1/scripts/{script['id']}/index/rebuild")
    second = client.post(f"/api/v1/scripts/{script['id']}/index/rebuild")

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["chunk_count"] == first.json()["chunk_count"] == 1
    assert second.json()["is_current"] is True


def test_project_delete_removes_local_vectors(monkeypatch, client):
    deleted_projects = []

    def fake_status(self):
        return {"qdrant_local": True, "available": False}

    def fake_delete(self, project_id):
        deleted_projects.append(project_id)

    monkeypatch.setattr(main.LocalRAG, "status", fake_status)
    monkeypatch.setattr(main.LocalRAG, "delete_project_vectors", fake_delete)
    project = client.post("/api/v1/projects", json={"name": "delete-vectors"}).json()
    script = client.post(
        f"/api/v1/projects/{project['id']}/scripts",
        json={"title": "script", "content": "Scene\n\nThe hangar door closes."},
    ).json()
    assert client.post(f"/api/v1/scripts/{script['id']}/index").status_code == 200

    response = client.delete(f"/api/v1/projects/{project['id']}")

    assert response.status_code == 204
    assert deleted_projects == [project["id"]]
    assert client.get(f"/api/v1/projects/{project['id']}").status_code == 404
    assert client.get(f"/api/v1/scripts/{script['id']}/index").status_code == 404


def test_adopted_research_source_is_retrievable(monkeypatch, client):
    async def fake_search_web(query):
        return {
            "query": query,
            "summary": "Runway threshold lights are green and identify the runway start.",
            "sources": [{"title": "Runway lights", "url": "https://example.com/runway"}],
        }

    monkeypatch.setattr(main, "search_web", fake_search_web)
    session = client.post("/api/v1/agent/sessions", json={"title": "research"}).json()
    search = client.post(
        "/api/v1/agent/tools/search",
        json={"query": "runway threshold lights", "session_id": session["id"]},
    )
    assert search.status_code == 200
    source_id = search.json()["persisted_sources"][0]["id"]

    adopted = client.post(
        f"/api/v1/agent/research/{source_id}/adopt",
        json={"adoption_reason": "Use for airport visual accuracy."},
    )
    assert adopted.status_code == 200
    assert adopted.json()["adopted"] is True

    retrieved = client.post(
        "/api/v1/agent/retrieve",
        json={"query": "green runway start threshold"},
    )
    assert retrieved.status_code == 200
    hits = retrieved.json()
    assert hits
    assert hits[0]["source"] == "research"
    assert "threshold lights" in hits[0]["content"]
