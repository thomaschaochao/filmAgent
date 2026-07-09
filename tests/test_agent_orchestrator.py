from types import SimpleNamespace

import pytest

import app.main as main
import app.services.orchestrator as orchestrator
from app.services import crew as crew_service
from app.services.crew_runner import CrewStageTool, run_stage_tools
from app.services.crew_runtime import instantiate_crewai_runtime
from app.services.crew_tool_executor import CrewToolExecutionError, execute_crewai_tool
from app.services.crew_tools import build_crewai_tool_descriptors, build_tool_catalog
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


def test_agent_clarification_messages_include_guided_options(client):
    session = client.post(
        "/api/v1/agent/sessions",
        json={"title": "Guided Session", "initial_input": "A pilot returns through rain."},
    ).json()

    assistant_message = session["messages"][-1]
    metadata = assistant_message["metadata_json"]

    assert session["status"] == "clarifying"
    assert metadata["option_mode"] == "clarify"
    assert metadata["missing_keys"]
    assert metadata["reply_options"]
    assert metadata["reply_options"][-1]["custom"] is True
    assert "api_key" not in str(metadata).lower()


def test_agent_plan_ready_message_offers_generate_plan_action(client):
    session = client.post(
        "/api/v1/agent/sessions",
        json={"title": "Plan Ready", "initial_input": "A pilot studies the runway."},
    ).json()

    response = client.post(
        f"/api/v1/agent/sessions/{session['id']}/messages",
        json={
            "content": "Use these settings.",
            "facts": {
                "name": "Plan Ready",
                "visual_style": "cinematic realism",
                "world_setting": "modern Seattle airport in heavy rain",
                "aspect_ratio": "16:9",
                "prompt_mode": "initial_frame",
            },
        },
    )

    body = response.json()
    metadata = body["messages"][-1]["metadata_json"]
    actions = {option.get("action") for option in metadata["reply_options"]}
    assert body["status"] == "plan_ready"
    assert metadata["option_mode"] == "plan_ready"
    assert "generate_plan" in actions
    assert any(option.get("custom") for option in metadata["reply_options"])
    assert "prompt_mode" not in {item["key"] for item in body["memories"]}


def test_agent_plan_generation_returns_conversation_execution_choice(client):
    session = client.post(
        "/api/v1/agent/sessions",
        json={"title": "Plan Execute", "initial_input": "A pilot studies the runway."},
    ).json()
    client.post(
        f"/api/v1/agent/sessions/{session['id']}/messages",
        json={
            "content": "Use these settings.",
            "facts": {
                "name": "Plan Execute",
                "visual_style": "cinematic realism",
                "world_setting": "modern airport",
                "aspect_ratio": "16:9",
            },
        },
    )

    planned = client.post(f"/api/v1/agent/sessions/{session['id']}/plan", json={})

    assert planned.status_code == 201
    refreshed = client.get(f"/api/v1/agent/sessions/{session['id']}").json()
    latest = refreshed["messages"][-1]
    metadata = latest["metadata_json"]
    actions = {option.get("action") for option in metadata["reply_options"]}
    assert latest["role"] == "assistant"
    assert "完整制作计划" in latest["content"]
    assert metadata["option_mode"] == "plan_approval"
    assert {"approve_plan", "defer_plan"}.issubset(actions)
    assert any(
        option.get("plan_id") == planned.json()["id"]
        for option in metadata["reply_options"]
    )


def test_agent_placeholder_project_name_still_requires_naming(client):
    session = client.post(
        "/api/v1/agent/sessions",
        json={"title": "新的影片计划", "initial_input": "一个少年崇拜表哥的荒诞喜剧。"},
    ).json()

    response = client.post(
        f"/api/v1/agent/sessions/{session['id']}/messages",
        json={
            "content": "先用未命名影片，其它设定按下面来。",
            "facts": {
                "name": "未命名影片",
                "visual_style": "幽默讽刺喜剧",
                "world_setting": "当代县城生活环境",
                "aspect_ratio": "16:9",
            },
        },
    )

    body = response.json()
    latest = body["messages"][-1]
    metadata = latest["metadata_json"]
    assert body["status"] == "clarifying"
    assert "项目名称" in metadata["missing_information"]
    assert "name" not in {item["key"] for item in body["memories"]}
    assert any(
        option.get("facts", {}).get("name")
        and option["facts"]["name"] != "未命名影片"
        for option in metadata["reply_options"]
    )


def test_agent_plan_approval_asks_to_execute_asset_stage(client):
    session = client.post(
        "/api/v1/agent/sessions",
        json={"title": "Stage Dialog", "initial_input": "A pilot studies the runway."},
    ).json()
    client.post(
        f"/api/v1/agent/sessions/{session['id']}/messages",
        json={
            "content": "Use these settings.",
            "facts": {
                "name": "Stage Dialog",
                "visual_style": "cinematic realism",
                "world_setting": "modern airport",
                "aspect_ratio": "16:9",
            },
        },
    )
    plan = client.post(f"/api/v1/agent/sessions/{session['id']}/plan", json={}).json()

    approved = client.post(f"/api/v1/agent/plans/{plan['id']}/approve")

    assert approved.status_code == 200
    latest = approved.json()["messages"][-1]
    metadata = latest["metadata_json"]
    assert "项目《Stage Dialog》" in latest["content"]
    assert "资产提取" in latest["content"]
    assert metadata["option_mode"] == "stage_approval"
    assert any(
        option.get("action") == "approve_stage" and option.get("stage") == "assets"
        for option in metadata["reply_options"]
    )


def test_agent_real_world_question_offers_web_research(client):
    session = client.post(
        "/api/v1/agent/sessions",
        json={"title": "Research intent", "initial_input": "请联网搜索 1990 年代县城录像厅资料。"},
    ).json()

    options = session["messages"][-1]["metadata_json"]["reply_options"]

    assert any(option.get("action") == "research_web" for option in options)
    assert any(option.get("label") == "先不用联网，按剧本继续" for option in options)


def test_agent_session_research_appends_summary_message(monkeypatch, client):
    async def fake_search_web(query):
        assert query == "runway threshold lights"
        return {
            "query": query,
            "summary": "Runway threshold lights are green.",
            "sources": [{"title": "Runway lights", "url": "https://example.com/runway"}],
            "provider": "volcengine",
        }

    monkeypatch.setattr(main, "search_web", fake_search_web)
    session = client.post("/api/v1/agent/sessions", json={"title": "research"}).json()

    response = client.post(
        f"/api/v1/agent/sessions/{session['id']}/research",
        json={"query": "runway threshold lights"},
    )

    assert response.status_code == 200
    body = response.json()
    latest = body["messages"][-1]
    assert "联网搜索完成" in latest["content"]
    assert "Runway threshold lights are green" in latest["content"]
    assert "https://example.com/runway" in latest["content"]
    assert latest["metadata_json"]["option_mode"] == "research_result"
    assert latest["metadata_json"]["sources"][0]["url"] == "https://example.com/runway"


def test_agent_uses_valid_ai_generated_clarification_options(monkeypatch, client):
    class FakeDeepSeekClient:
        def chat_json(self, *_):
            return {
                "facts": {},
                "reply": "请选择一套视觉、世界和镜头提示词方向。",
                "reply_options": [
                    {
                        "label": "选项1：写实机场惊险片",
                        "content": "使用写实机场惊险片方向。",
                        "facts": {
                            "name": "Return Flight",
                            "visual_style": "电影感写实",
                            "world_setting": "当代美国西海岸机场，暴雨夜",
                            "aspect_ratio": "16:9",
                            "prompt_mode": "initial_frame",
                        },
                        "description": "适合紧张、真实的飞行员返航故事。",
                    },
                    {
                        "label": "选项2：近未来航空科幻",
                        "content": "使用近未来航空科幻方向。",
                        "facts": {
                            "name": "Return Flight",
                            "visual_style": "高概念科幻写实",
                            "world_setting": "近未来亚洲超级机场，台风夜",
                            "aspect_ratio": "2.39:1",
                            "prompt_mode": "storyboard",
                        },
                        "description": "适合更强视觉设定和连续动作参考。",
                    },
                ],
            }

    monkeypatch.setattr(orchestrator, "DeepSeekClient", FakeDeepSeekClient)
    monkeypatch.setattr(
        orchestrator,
        "get_settings",
        lambda: SimpleNamespace(master_agent_ai_enabled=True),
    )

    session = client.post(
        "/api/v1/agent/sessions",
        json={"title": "Return Flight", "initial_input": "A pilot returns through rain."},
    ).json()

    options = session["messages"][-1]["metadata_json"]["reply_options"]
    assert options[0]["source"] == "ai"
    assert options[0]["label"] == "选项1：写实机场惊险片"
    assert {chip["key"] for chip in options[0]["fact_chips"]} >= {
        "visual_style",
        "world_setting",
        "aspect_ratio",
    }
    assert "prompt_mode" not in options[0]["facts"]
    assert options[-1]["custom"] is True


def test_agent_rejects_invalid_ai_options_and_uses_combined_fallback(monkeypatch, client):
    class FakeDeepSeekClient:
        def chat_json(self, *_):
            return {
                "facts": {},
                "reply": "请选择缺失设定。",
                "reply_options": [
                    {
                        "label": "重复选项",
                        "content": "只回答风格。",
                        "facts": {"visual_style": "电影感写实"},
                        "description": "缺少其它字段。",
                    },
                    {
                        "label": "重复选项",
                        "content": "只回答风格。",
                        "facts": {"visual_style": "电影感写实"},
                        "description": "重复且缺字段。",
                    },
                ],
            }

    monkeypatch.setattr(orchestrator, "DeepSeekClient", FakeDeepSeekClient)
    monkeypatch.setattr(
        orchestrator,
        "get_settings",
        lambda: SimpleNamespace(master_agent_ai_enabled=True),
    )

    session = client.post(
        "/api/v1/agent/sessions",
        json={"title": "Fallback Session", "initial_input": "A pilot returns through rain."},
    ).json()

    options = session["messages"][-1]["metadata_json"]["reply_options"]
    assert options[0]["source"] == "fallback"
    assert len(options[0]["facts"]) >= 3
    assert options[0]["facts"]["name"] == "Fallback Session"
    assert "prompt_mode" not in options[0]["facts"]
    assert "美国" not in str(options)
    assert "科幻" not in str(options)
    assert "沿用剧本" not in str(options)
    assert "不额外预设" not in str(options)
    assert "暂不指定" not in str(options)
    assert options[-1]["custom"] is True


def test_agent_fallback_options_are_contextual_and_user_facing_for_theater(client):
    session = client.post(
        "/api/v1/agent/sessions",
        json={"title": "后台", "initial_input": "一个发生在剧场后台的现实主义戏剧。"},
    ).json()

    options = session["messages"][-1]["metadata_json"]["reply_options"]
    option_text = str(options)
    assert options[0]["source"] == "fallback"
    assert any(
        option.get("facts", {}).get("name") in {"后台", "后台灯下", "开演之前", "剧场暗门"}
        for option in options
    )
    assert "剧场" in option_text or "舞台" in option_text
    assert "美国" not in option_text
    assert "科幻" not in option_text
    assert "沿用剧本" not in option_text
    assert "不额外预设" not in option_text
    assert "暂不指定" not in option_text


def test_agent_ai_context_does_not_include_other_session_memory(monkeypatch, client):
    captured_prompts: list[str] = []

    class FakeDeepSeekClient:
        def chat_json(self, _system_prompt, user_prompt):
            captured_prompts.append(user_prompt)
            return {"facts": {}, "reply": "请确认当前剧本设定。", "reply_options": []}

    monkeypatch.setattr(orchestrator, "DeepSeekClient", FakeDeepSeekClient)
    monkeypatch.setattr(
        orchestrator,
        "get_settings",
        lambda: SimpleNamespace(master_agent_ai_enabled=True),
    )

    first = client.post("/api/v1/agent/sessions", json={"title": "Sci Fi"}).json()
    client.post(
        f"/api/v1/agent/sessions/{first['id']}/messages",
        json={
            "content": "确认科幻项目设定",
            "facts": {
                "name": "美国科幻项目",
                "visual_style": "高概念科幻",
                "world_setting": "近未来美国太空港",
                "aspect_ratio": "2.39:1",
            },
        },
    )
    client.post(
        "/api/v1/agent/sessions",
        json={"title": "Drama", "initial_input": "一个发生在剧场后台的现实主义戏剧。"},
    )

    second_prompt = captured_prompts[-1]
    assert "美国科幻项目" not in second_prompt
    assert "近未来美国太空港" not in second_prompt
    assert "高概念科幻" not in second_prompt
    assert "剧场后台" in second_prompt


def test_agent_option_facts_can_be_confirmed_into_memory(client):
    session = client.post(
        "/api/v1/agent/sessions",
        json={"title": "Memory Session", "initial_input": "A pilot returns through rain."},
    ).json()
    option = session["messages"][-1]["metadata_json"]["reply_options"][0]

    response = client.post(
        f"/api/v1/agent/sessions/{session['id']}/messages",
        json={"content": option["content"], "facts": option["facts"]},
    )

    memories = {item["key"]: item["value"] for item in response.json()["memories"]}
    for key, value in option["facts"].items():
        assert memories[key] == value


def test_agent_session_archive_filters_and_unarchive(client):
    first = client.post("/api/v1/agent/sessions", json={"title": "Active chat"}).json()
    second = client.post("/api/v1/agent/sessions", json={"title": "Archive me"}).json()

    archived = client.post(f"/api/v1/agent/sessions/{second['id']}/archive")
    assert archived.status_code == 200
    assert archived.json()["archived_at"] is not None

    active_ids = {item["id"] for item in client.get("/api/v1/agent/sessions").json()}
    archived_ids = {
        item["id"] for item in client.get("/api/v1/agent/sessions?archived=true").json()
    }
    all_ids = {
        item["id"] for item in client.get("/api/v1/agent/sessions?include_archived=true").json()
    }
    assert first["id"] in active_ids
    assert second["id"] not in active_ids
    assert second["id"] in archived_ids
    assert {first["id"], second["id"]}.issubset(all_ids)

    restored = client.post(f"/api/v1/agent/sessions/{second['id']}/unarchive")
    assert restored.status_code == 200
    assert restored.json()["archived_at"] is None
    active_ids = {item["id"] for item in client.get("/api/v1/agent/sessions").json()}
    assert second["id"] in active_ids


def test_delete_agent_session_cascades_agent_data_but_keeps_project(client):
    session = client.post(
        "/api/v1/agent/sessions",
        json={"title": "Delete session", "initial_input": "A pilot studies the runway."},
    ).json()
    client.post(
        f"/api/v1/agent/sessions/{session['id']}/messages",
        json={
            "content": "Use these settings.",
            "facts": {
                "name": "Kept Project",
                "visual_style": "cinematic realism",
                "world_setting": "modern airport",
                "aspect_ratio": "16:9",
                "prompt_mode": "initial_frame",
            },
        },
    )
    plan = client.post(f"/api/v1/agent/sessions/{session['id']}/plan", json={}).json()
    approved = client.post(f"/api/v1/agent/plans/{plan['id']}/approve").json()

    deleted = client.delete(f"/api/v1/agent/sessions/{session['id']}")

    assert deleted.status_code == 204
    assert client.get(f"/api/v1/agent/sessions/{session['id']}").status_code == 404
    project_ids = {item["id"] for item in client.get("/api/v1/projects").json()}
    assert approved["project_id"] in project_ids


def test_delete_missing_agent_session_returns_404(client):
    response = client.delete("/api/v1/agent/sessions/not-found")
    assert response.status_code == 404


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


def test_crew_tool_execute_api_can_create_project_after_plan_approval(client):
    session = client.post(
        "/api/v1/agent/sessions",
        json={"title": "crew-execute", "initial_input": "A pilot returns through rain."},
    ).json()
    client.post(
        f"/api/v1/agent/sessions/{session['id']}/messages",
        json={
            "content": "Use these settings.",
            "facts": {
                "name": "Crew Execute",
                "visual_style": "cinematic realism",
                "world_setting": "modern Seattle airport in heavy rain",
                "aspect_ratio": "16:9",
                "prompt_mode": "initial_frame",
            },
        },
    )
    plan = client.post(f"/api/v1/agent/sessions/{session['id']}/plan", json={}).json()

    response = client.post(
        "/api/v1/agent/crew/tools/create_project/execute",
        json={"plan_id": plan["id"]},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["tool"] == "create_project"
    assert body["result"]["project_id"]
    refreshed = client.get(f"/api/v1/agent/sessions/{session['id']}").json()
    assert refreshed["project_id"] == body["result"]["project_id"]


def test_crew_tool_execute_api_rejects_write_without_plan_id(client):
    response = client.post(
        "/api/v1/agent/crew/tools/generate_shot_prompts/execute",
        json={"project_id": "project-1"},
    )

    assert response.status_code == 409
    assert "plan_id" in response.json()["detail"]
    assert "api_key" not in str(response.json()).lower()


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
    assert body["tool_adapter"]["descriptors_ready"] is True
    assert body["tool_adapter"]["all_writes_require_approval"] is True
    assert "generate_shot_prompts" in body["tool_adapter"]["writable_tools"]
    descriptors = {descriptor["name"]: descriptor for descriptor in body["tool_descriptors"]}
    assert descriptors["retrieve_context"]["args_schema"]["required"] == ["query"]
    assert descriptors["generate_shot_prompts"]["metadata"]["requires_user_approval"] is True
    assert body["runtime_factory"]["factory_ready"] is True
    assert body["runtime_factory"]["agent_count"] >= 6
    assert body["runtime_factory"]["tool_handles_bound"] is True
    assert body["runtime_factory"]["tool_handle_count"] >= 6
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


def test_crew_runtime_preflight_allows_fallback_workflow_exercise(client):
    response = client.post("/api/v1/agent/crew/preflight")
    assert response.status_code == 200
    body = response.json()
    assert body["framework"] == "crewai"
    assert body["factory_ready"] is True
    assert body["catalog_ready"] is True
    assert body["descriptors_ready"] is True
    assert body["can_exercise_workflow"] is True
    assert body["agent_count"] >= 6
    assert body["task_count"] >= 6
    assert body["tool_descriptor_count"] >= 6
    assert body["tool_handle_count"] >= 6
    assert body["tool_handles_bound"] is True
    assert "api_key" not in str(body).lower()


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
    assert runtime["tool_adapter"]["descriptors_ready"] is True
    assert any(
        descriptor["name"] == "generate_storyboard"
        for descriptor in runtime["tool_descriptors"]
    )
    assert runtime["runtime_factory"]["factory_ready"] is True
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


def test_crew_tool_descriptors_are_strict_and_do_not_expose_secrets():
    descriptors = {
        descriptor.name: descriptor.model_dump()
        for descriptor in build_crewai_tool_descriptors()
    }

    assert set(descriptors) == set(build_tool_catalog())
    assert descriptors["generate_storyboard"]["args_schema"]["required"] == ["plan_id"]
    assert descriptors["generate_storyboard"]["args_schema"]["additionalProperties"] is False
    assert descriptors["create_project"]["metadata"]["mutates_state"] is True
    assert descriptors["create_project"]["metadata"]["requires_user_approval"] is True
    assert "api_key" not in str(descriptors).lower()


def test_crewai_runtime_instantiates_agents_tasks_and_crew():
    calls = {"agents": [], "tasks": [], "crews": []}

    class FakeAgent:
        def __init__(self, **kwargs):
            calls["agents"].append(kwargs)

    class FakeTask:
        def __init__(self, **kwargs):
            calls["tasks"].append(kwargs)

    class FakeCrew:
        def __init__(self, **kwargs):
            calls["crews"].append(kwargs)

    class FakeProcess:
        sequential = "sequential"

    class FakeCrewAIModule:
        Agent = FakeAgent
        Task = FakeTask
        Crew = FakeCrew
        Process = FakeProcess

    result = instantiate_crewai_runtime(FakeCrewAIModule)

    assert result.crew is not None
    assert result.status["instantiated"] is True
    assert result.status["tool_handles_bound"] is True
    assert result.status["tool_handle_count"] >= 6
    assert len(calls["agents"]) >= 6
    assert len(calls["tasks"]) >= 6
    assert len(calls["crews"]) == 1
    assert calls["crews"][0]["process"] == "sequential"
    assert "registered internal tools" in calls["tasks"][0]["expected_output"]
    producer_tools = calls["agents"][0]["tools"]
    assert {tool.name for tool in producer_tools} == {
        "retrieve_context",
        "create_project",
        "save_script",
    }
    tool_schema = (
        producer_tools[0].args_schema.model_json_schema()
        if hasattr(producer_tools[0].args_schema, "model_json_schema")
        else producer_tools[0].args_schema
    )
    assert "query" in tool_schema["properties"]
    assert tool_schema["additionalProperties"] is False
    with pytest.raises(CrewToolExecutionError, match="requires an approved workflow plan_id"):
        create_project_tool = next(tool for tool in producer_tools if tool.name == "create_project")
        create_project_tool._run()
    assert "api_key" not in str(result.status).lower()


def test_crewai_write_tools_require_approved_plan_id():
    with pytest.raises(
        CrewToolExecutionError, match="requires an approved workflow plan_id"
    ) as exc:
        execute_crewai_tool("generate_shot_prompts", project_id="project-1")

    assert "api_key" not in str(exc.value).lower()


def test_crewai_write_tools_delegate_to_stage_approval(monkeypatch):
    import app.services.crew_tool_executor as executor

    calls = {}

    def fake_approve_stage(plan_id, stage, *, db=None):
        calls["plan_id"] = plan_id
        calls["stage"] = stage
        calls["db"] = db
        return {"task_id": "task-1", "stage": stage, "status": "completed"}

    monkeypatch.setattr(executor, "_approve_stage", fake_approve_stage)

    result = execute_crewai_tool("generate_shot_prompts", plan_id="plan-1")

    assert result == {"task_id": "task-1", "stage": "prompts", "status": "completed"}
    assert calls == {"plan_id": "plan-1", "stage": "prompts", "db": None}


def test_crewai_retrieve_context_tool_uses_registered_retrieval(monkeypatch):
    import app.services.crew_tool_executor as executor

    calls = {}

    class FakeSession:
        def __enter__(self):
            return "db"

        def __exit__(self, *args):
            return None

    def fake_retrieve(payload, db):
        calls["payload"] = payload
        calls["db"] = db
        return [{"chunk_id": "chunk-1", "content": "runway lights"}]

    monkeypatch.setattr(executor, "SessionLocal", lambda: FakeSession())
    monkeypatch.setattr(executor, "retrieve_context", fake_retrieve)

    result = execute_crewai_tool(
        "retrieve_context",
        query="runway lights",
        project_id="project-1",
        limit=3,
    )

    assert result == [{"chunk_id": "chunk-1", "content": "runway lights"}]
    assert calls["payload"].query == "runway lights"
    assert calls["payload"].project_id == "project-1"
    assert calls["payload"].limit == 3
    assert calls["db"] == "db"


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


def test_asset_stage_completion_adds_agent_followup_message(monkeypatch, client):
    session, plan = _approved_agent_plan(client)
    monkeypatch.setattr(main, "extract_project_assets", lambda project_id, db: [])

    response = client.post(f"/api/v1/agent/plans/{plan['id']}/stages/assets/approve")

    assert response.status_code == 200
    refreshed = client.get(f"/api/v1/agent/sessions/{session['id']}").json()
    latest = refreshed["messages"][-1]
    metadata = latest["metadata_json"]
    assert "资产提取与资产提示词已完成" in latest["content"]
    assert "资产管理" in latest["content"]
    assert "继续拆分分镜" in str(metadata["reply_options"])
    assert any(
        option.get("action") == "approve_stage" and option.get("stage") == "shots"
        for option in metadata["reply_options"]
    )


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
    assert body["deepseek_thinking_enabled"] is True
    assert body["deepseek_reasoning_effort"] == "high"
    assert body["web_search_configured"] is False
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
