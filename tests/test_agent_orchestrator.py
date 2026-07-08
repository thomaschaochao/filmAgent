import pytest

import app.main as main
from app.services import crew as crew_service
from app.services.crew_runner import CrewStageTool, run_stage_tools
from app.services.crew_tools import build_tool_catalog
from app.services.rag import LocalRAG


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


def test_crew_status_exposes_roles_without_secrets(client):
    response = client.get("/api/v1/agent/crew/status")
    assert response.status_code == 200
    body = response.json()
    assert body["framework"] == "crewai"
    assert body["requested"] is True
    assert len(body["roles"]) >= 6
    assert any(role["name"] == "ProducerAgent" for role in body["roles"])
    tools = {tool["key"]: tool for tool in body["tools"]}
    assert tools["retrieve_context"]["scope"] == "read:rag"
    assert tools["generate_shot_prompts"]["owner_agent"] == "prompt"
    assert tools["generate_shot_prompts"]["mutates_state"] is True
    assert body["memory_isolation"]["prompt"]["write"] == [
        "prompt_versions",
        "prompt_strategy_snapshot",
    ]
    assert body["tool_adapter"]["catalog_ready"] is True
    assert body["tool_adapter"]["all_writes_require_approval"] is True
    assert "generate_shot_prompts" in body["tool_adapter"]["writable_tools"]
    assert "tool_start" in body["checkpoint_events"]
    assert "task_completed" in body["checkpoint_events"]
    assert "api_key" not in str(body).lower()


def test_crew_status_can_report_active_runtime(monkeypatch, client):
    monkeypatch.setattr(crew_service, "_crewai_installed", lambda: True)
    response = client.get("/api/v1/agent/crew/status")
    assert response.status_code == 200
    body = response.json()
    assert body["active"] is True
    assert body["fallback"] == ""


def test_workflow_plan_records_crew_runtime_metadata(client):
    session = client.post(
        "/api/v1/agent/sessions",
        json={"title": "crew-plan", "initial_input": "A pilot studies the runway."},
    ).json()
    client.post(
        f"/api/v1/agent/sessions/{session['id']}/messages",
        json={
            "content": "Use these settings.",
            "facts": {
                "name": "Crew Plan",
                "visual_style": "realistic",
                "world_setting": "modern airport",
                "aspect_ratio": "16:9",
                "prompt_mode": "initial_frame",
            },
        },
    )
    plan = client.post(f"/api/v1/agent/sessions/{session['id']}/plan", json={}).json()
    runtime = plan["project_spec"]["agent_runtime"]
    assert runtime["framework"] == "crewai"
    assert "ProducerAgent" in runtime["roles"]
    assert runtime["task_agents"]["assets"] == "asset"
    assert runtime["tool_registry"]["retrieve_context"]["scope"] == "read:rag"
    assert runtime["tool_registry"]["create_project"]["requires_user_approval"] is True
    assert runtime["tool_adapter"]["catalog_ready"] is True
    assert runtime["memory_isolation"]["research"]["write"] == [
        "research_sources_adopted_summary_only"
    ]
    assert "agent_handoff" in runtime["checkpoint_events"]


def test_crew_tool_catalog_limits_write_tools_to_approved_internal_tools():
    catalog = build_tool_catalog()

    assert catalog["retrieve_context"].mutates_state is False
    assert catalog["retrieve_context"].scope == "read:rag"
    assert catalog["generate_shot_prompts"].mutates_state is True
    assert catalog["generate_shot_prompts"].requires_user_approval is True
    assert all(tool.exposed_to_crewai for tool in catalog.values())


def _approved_agent_plan(client):
    session = client.post(
        "/api/v1/agent/sessions",
        json={"title": "checkpoint", "initial_input": "A pilot prepares for landing."},
    ).json()
    client.post(
        f"/api/v1/agent/sessions/{session['id']}/messages",
        json={
            "content": "Use these settings.",
            "facts": {
                "name": "Checkpoint Plan",
                "visual_style": "realistic",
                "world_setting": "modern airport",
                "aspect_ratio": "16:9",
                "prompt_mode": "initial_frame",
            },
        },
    )
    plan = client.post(f"/api/v1/agent/sessions/{session['id']}/plan", json={}).json()
    approved = client.post(f"/api/v1/agent/plans/{plan['id']}/approve").json()
    plan = approved["plans"][-1]
    return approved, plan


def test_workflow_task_checkpoint_is_available_before_execution(client):
    _session, plan = _approved_agent_plan(client)
    task = next(item for item in plan["tasks"] if item["stage"] == "assets")

    response = client.get(f"/api/v1/agent/tasks/{task['id']}/checkpoint")

    assert response.status_code == 200
    body = response.json()
    assert body["task_id"] == task["id"]
    assert body["status"] == "awaiting_approval"
    assert body["last_safe_step"] == "not_started"


def test_workflow_task_timeout_failure_can_resume(monkeypatch, client):
    _session, plan = _approved_agent_plan(client)
    task = next(item for item in plan["tasks"] if item["stage"] == "assets")

    def fail_assets(project_id, db):
        raise TimeoutError("provider timeout while generating assets")

    monkeypatch.setattr(main, "extract_project_assets", fail_assets)
    failed = client.post(f"/api/v1/agent/plans/{plan['id']}/stages/assets/approve")
    assert failed.status_code == 502

    checkpoint = client.get(f"/api/v1/agent/tasks/{task['id']}/checkpoint").json()
    assert checkpoint["status"] == "resumable"
    assert checkpoint["error"]["type"] == "provider_timeout"
    assert checkpoint["error"]["retryable"] is True
    history = checkpoint["tool_call_history"]
    assert [item["event"] for item in history] == [
        "tool_start",
        "tool_success",
        "tool_start",
        "tool_failed",
    ]
    assert history[-1]["tool_name"] == "extract_assets"

    resumed = client.post(f"/api/v1/agent/tasks/{task['id']}/resume")
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "awaiting_approval"


def test_workflow_stage_runner_records_successful_tool_events(monkeypatch, client):
    _session, plan = _approved_agent_plan(client)
    monkeypatch.setattr(main, "extract_project_assets", lambda project_id, db: [])

    response = client.post(f"/api/v1/agent/plans/{plan['id']}/stages/assets/approve")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "completed"
    assert body["result_data"]["asset_count"] == 0
    history = body["result_data"]["tool_call_history"]
    assert [item["tool_name"] for item in history] == [
        "approve_script",
        "approve_script",
        "extract_assets",
        "extract_assets",
    ]
    assert [item["event"] for item in history] == [
        "tool_start",
        "tool_success",
        "tool_start",
        "tool_success",
    ]


def test_stage_runner_rejects_unregistered_tools():
    with pytest.raises(ValueError, match="Unregistered CrewAI tool"):
        run_stage_tools(
            None,
            None,
            [CrewStageTool("unsafe_shell", lambda: None)],
        )


def test_workflow_task_quota_failure_is_not_retryable(monkeypatch, client):
    _session, plan = _approved_agent_plan(client)
    task = next(item for item in plan["tasks"] if item["stage"] == "assets")

    def fail_assets(project_id, db):
        raise RuntimeError("quota exceeded: insufficient balance")

    monkeypatch.setattr(main, "extract_project_assets", fail_assets)
    failed = client.post(f"/api/v1/agent/plans/{plan['id']}/stages/assets/approve")
    assert failed.status_code == 502

    checkpoint = client.get(f"/api/v1/agent/tasks/{task['id']}/checkpoint").json()
    assert checkpoint["status"] == "failed"
    assert checkpoint["error"]["type"] == "quota_exceeded"
    assert checkpoint["error"]["retryable"] is False
    assert "FILMAGENT_" not in str(checkpoint)

    resumed = client.post(f"/api/v1/agent/tasks/{task['id']}/resume")
    assert resumed.status_code == 409


def test_workflow_task_can_be_cancelled(client):
    _session, plan = _approved_agent_plan(client)
    task = next(item for item in plan["tasks"] if item["stage"] == "assets")

    cancelled = client.post(f"/api/v1/agent/tasks/{task['id']}/cancel")

    assert cancelled.status_code == 200
    body = cancelled.json()
    assert body["status"] == "cancelled"
    assert body["result_data"]["recovery"]["error_type"] == "user_interrupted"


def test_retrieval_status_does_not_expose_local_paths(client):
    response = client.get("/api/v1/agent/retrieval/status")
    assert response.status_code == 200
    body = response.json()
    assert body["model"] == "BAAI/bge-m3"
    assert body["vector_backend"] == "qdrant_local"
    assert body["collection"] == "filmagent_knowledge_v1"
    assert "path" not in body


def test_retrieval_self_test_reports_safe_fallback(client):
    response = client.post("/api/v1/agent/retrieval/self-test")
    assert response.status_code == 200
    body = response.json()
    assert body["vector_backend"] == "qdrant_local"
    assert "checks" in body
    assert "path" not in body
    assert "traceback" not in str(body).lower()


def test_retrieval_self_test_can_report_success(monkeypatch, client):
    def fake_self_test(self):
        return {
            "available": True,
            "vector_backend": "qdrant_local",
            "model": "BAAI/bge-m3",
            "device": "cpu",
            "collection": "filmagent_knowledge_v1",
            "checks": {
                "dependencies": True,
                "qdrant_local": True,
                "model_cached": True,
                "index": True,
                "search": True,
                "delete": True,
            },
            "message": "Local RAG self-test passed.",
        }

    monkeypatch.setattr(main.LocalRAG, "self_test", fake_self_test)
    response = client.post("/api/v1/agent/retrieval/self-test")
    assert response.status_code == 200
    body = response.json()
    assert body["available"] is True
    assert all(body["checks"].values())


def test_local_rag_self_test_uses_uuid_point_ids(monkeypatch):
    indexed_records = []

    def fake_status(self):
        return {
            "available": True,
            "configured": True,
            "qdrant_local": True,
            "model_cached": True,
            "vector_backend": "qdrant_local",
            "model": "BAAI/bge-m3",
            "device": "cpu",
            "collection": "filmagent_knowledge_v1",
        }

    def fake_index_chunks(self, records):
        indexed_records.extend(records)

    def fake_hybrid_search(self, *args, **kwargs):
        return [{"chunk_id": indexed_records[0]["id"], "score": 1.0}]

    def fake_delete_project_vectors(self, project_id):
        assert project_id == "__self_test__"

    monkeypatch.setattr(LocalRAG, "status", fake_status)
    monkeypatch.setattr(LocalRAG, "index_chunks", fake_index_chunks)
    monkeypatch.setattr(LocalRAG, "hybrid_search", fake_hybrid_search)
    monkeypatch.setattr(LocalRAG, "delete_project_vectors", fake_delete_project_vectors)

    result = LocalRAG().self_test()

    assert result["available"] is True
    assert indexed_records
    assert indexed_records[0]["id"].count("-") == 4
    assert not indexed_records[0]["id"].startswith("self-test-")


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


def test_retrieval_rebuild_restores_indexes_from_sqlite(client):
    project = client.post("/api/v1/projects", json={"name": "full-rebuild"}).json()
    first_script = client.post(
        f"/api/v1/projects/{project['id']}/scripts",
        json={"title": "one", "content": "Scene one\n\nThe red beacon flashes."},
    ).json()
    second_script = client.post(
        f"/api/v1/projects/{project['id']}/scripts",
        json={"title": "two", "content": "Scene two\n\nThe blue flare rises."},
    ).json()
    assert client.post(f"/api/v1/scripts/{first_script['id']}/index").status_code == 200
    assert client.post(f"/api/v1/scripts/{second_script['id']}/index").status_code == 200

    rebuild = client.post("/api/v1/agent/retrieval/rebuild", json={})

    assert rebuild.status_code == 200
    body = rebuild.json()
    assert body["script_count"] == 2
    assert body["rebuilt_count"] == 2
    assert body["fallback_count"] == 2
    response = client.post(
        "/api/v1/agent/retrieve",
        json={"query": "blue flare", "project_id": project["id"]},
    )
    assert response.status_code == 200
    contents = "\n".join(hit["content"] for hit in response.json())
    assert "blue flare" in contents
    assert "red beacon" not in contents


def test_retrieval_rebuild_can_be_scoped_to_project(client):
    first_project = client.post("/api/v1/projects", json={"name": "first"}).json()
    second_project = client.post("/api/v1/projects", json={"name": "second"}).json()
    first_script = client.post(
        f"/api/v1/projects/{first_project['id']}/scripts",
        json={"title": "first", "content": "First project\n\nCopper tower at dawn."},
    ).json()
    second_script = client.post(
        f"/api/v1/projects/{second_project['id']}/scripts",
        json={"title": "second", "content": "Second project\n\nSilver tower at dusk."},
    ).json()
    client.post(f"/api/v1/scripts/{first_script['id']}/index")
    client.post(f"/api/v1/scripts/{second_script['id']}/index")

    rebuild = client.post(
        "/api/v1/agent/retrieval/rebuild",
        json={"project_id": first_project["id"]},
    )

    assert rebuild.status_code == 200
    assert rebuild.json()["script_count"] == 1
    first_hits = client.post(
        "/api/v1/agent/retrieve",
        json={"query": "copper tower", "project_id": first_project["id"]},
    ).json()
    second_hits = client.post(
        "/api/v1/agent/retrieve",
        json={"query": "silver tower", "project_id": second_project["id"]},
    ).json()
    assert first_hits and "Copper tower" in first_hits[0]["content"]
    assert second_hits and "Silver tower" in second_hits[0]["content"]


def test_script_index_runs_vector_job_when_rag_is_available(monkeypatch, client):
    indexed_records = []

    def fake_status(self):
        return {"available": True, "qdrant_local": True}

    def fake_index_chunks(self, records):
        indexed_records.extend(records)

    monkeypatch.setattr(main.LocalRAG, "status", fake_status)
    monkeypatch.setattr(main.LocalRAG, "index_chunks", fake_index_chunks)
    project = client.post("/api/v1/projects", json={"name": "vector-job"}).json()
    script = client.post(
        f"/api/v1/projects/{project['id']}/scripts",
        json={"title": "script", "content": "Scene\n\nVector smoke over the runway."},
    ).json()

    response = client.post(f"/api/v1/scripts/{script['id']}/index")

    assert response.status_code == 200
    index = client.get(f"/api/v1/scripts/{script['id']}/index").json()
    assert index["embedding_status"] == "ready"
    assert index["is_current"] is True
    assert indexed_records
    assert indexed_records[0]["content_type"] == "script_chunk"
    assert indexed_records[0]["project_id"] == project["id"]


def test_agent_plan_approval_queues_vector_job_when_rag_is_available(monkeypatch, client):
    indexed_records = []

    def fake_status(self):
        return {"available": True, "qdrant_local": True}

    def fake_index_chunks(self, records):
        indexed_records.extend(records)

    monkeypatch.setattr(main.LocalRAG, "status", fake_status)
    monkeypatch.setattr(main.LocalRAG, "index_chunks", fake_index_chunks)
    session = client.post(
        "/api/v1/agent/sessions",
        json={"title": "vector-plan", "initial_input": "A pilot follows vector lights."},
    ).json()
    client.post(
        f"/api/v1/agent/sessions/{session['id']}/messages",
        json={
            "content": "Use these settings.",
            "facts": {
                "name": "Vector Plan",
                "visual_style": "realistic",
                "world_setting": "modern airport",
                "aspect_ratio": "16:9",
                "prompt_mode": "initial_frame",
            },
        },
    )
    plan = client.post(f"/api/v1/agent/sessions/{session['id']}/plan", json={}).json()

    approved = client.post(f"/api/v1/agent/plans/{plan['id']}/approve")

    assert approved.status_code == 200
    project_id = approved.json()["project_id"]
    scripts = client.get(f"/api/v1/projects/{project_id}/scripts").json()
    index = client.get(f"/api/v1/scripts/{scripts[0]['id']}/index").json()
    assert index["embedding_status"] == "ready"
    assert indexed_records
    assert indexed_records[0]["project_id"] == project_id
