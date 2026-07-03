from app.config import Settings
from app.services.image_generation import GeneratedImage, generate_image


def _create_asset(client, prompt: str = "three-view character sheet") -> dict:
    project = client.post("/api/v1/projects", json={"name": "Image Test"}).json()
    return client.post(
        f"/api/v1/projects/{project['id']}/assets",
        json={"asset_type": "character", "name": "Pilot", "prompt": prompt},
    ).json()


def test_provider_status_never_exposes_keys(client, monkeypatch):
    settings = Settings(_env_file=None, openai_api_key="secret-openai", ark_api_key="secret-ark")
    monkeypatch.setattr("app.main.get_settings", lambda: settings)
    response = client.get("/api/v1/image-providers")
    assert response.status_code == 200
    body = response.json()
    assert [item["configured"] for item in body] == [True, True]
    assert "secret-openai" not in response.text
    assert "secret-ark" not in response.text


def test_generate_list_select_and_delete_asset_image(client, monkeypatch):
    asset = _create_asset(client)
    settings = Settings(_env_file=None, openai_api_key="configured")
    monkeypatch.setattr("app.main.get_settings", lambda: settings)
    monkeypatch.setattr(
        "app.main.generate_image",
        lambda *args, **kwargs: (GeneratedImage(b"generated-png", ".png"), "gpt-image-2"),
    )

    create = client.post(
        f"/api/v1/assets/{asset['id']}/images/generate",
        json={"provider": "openai"},
    )
    assert create.status_code == 202
    image_id = create.json()["id"]

    images = client.get(f"/api/v1/assets/{asset['id']}/images").json()
    generated = next(item for item in images if item["id"] == image_id)
    assert generated["status"] == "ready"
    assert generated["image_url"].startswith("/storage/projects/")
    assert "Image Test-" in generated["local_path"]
    assert "人物" in generated["local_path"]
    assert "Pilot" in generated["local_path"]
    assert generated["local_path"].endswith(f"Pilot-{image_id[:8]}.png")
    assert generated["is_primary"] is True

    metrics = client.get(f"/api/v1/projects/{asset['project_id']}/agent-metrics").json()
    image_run = next(
        run for run in metrics["recent_runs"] if run["operation"] == "image_generation"
    )
    assert image_run["provider"] == "openai"
    assert image_run["model"] == "gpt-image-2"
    assert image_run["status"] == "passed"

    upload = client.put(
        f"/api/v1/assets/{asset['id']}/image",
        content=b"uploaded-png",
        headers={"content-type": "image/png"},
    )
    assert upload.status_code == 200
    images = client.get(f"/api/v1/assets/{asset['id']}/images").json()
    assert len(images) == 2
    uploaded = next(item for item in images if item["source"] == "upload")
    assert uploaded["is_primary"] is True

    selected = client.patch(f"/api/v1/assets/{asset['id']}/images/{image_id}/primary")
    assert selected.status_code == 200
    assert selected.json()["image_url"] == generated["image_url"]

    deleted = client.delete(f"/api/v1/assets/{asset['id']}/images/{image_id}")
    assert deleted.status_code == 204
    remaining = client.get(f"/api/v1/assets/{asset['id']}/images").json()
    assert len(remaining) == 1
    assert remaining[0]["id"] == uploaded["id"]
    assert remaining[0]["is_primary"] is True


def test_generation_requires_prompt_and_configured_provider(client, monkeypatch):
    monkeypatch.setattr("app.main.get_settings", lambda: Settings(_env_file=None))
    asset = _create_asset(client, prompt="")
    no_prompt = client.post(
        f"/api/v1/assets/{asset['id']}/images/generate",
        json={"provider": "openai"},
    )
    assert no_prompt.status_code == 409

    client.patch(f"/api/v1/assets/{asset['id']}", json={"prompt": "character sheet"})
    no_key = client.post(
        f"/api/v1/assets/{asset['id']}/images/generate",
        json={"provider": "openai"},
    )
    assert no_key.status_code == 409
    assert "config.local.env" in no_key.json()["detail"]


def test_provider_payloads(monkeypatch):
    calls = []

    def fake_post(url, api_key, payload, settings):
        calls.append((url, api_key, payload))
        return GeneratedImage(b"image", ".png")

    monkeypatch.setattr("app.services.image_generation._post_image", fake_post)
    settings = Settings(_env_file=None, openai_api_key="oa", ark_api_key="ark")

    _, openai_model = generate_image("openai", "prompt", settings=settings)
    _, seedream_model = generate_image("seedream", "prompt", settings=settings)

    assert openai_model == "gpt-image-2"
    assert calls[0][0].endswith("/v1/images/generations")
    assert calls[0][2]["quality"] == "high"
    assert seedream_model == "doubao-seedream-5-0-260128"
    assert calls[1][0].endswith("/api/v3/images/generations")
    assert calls[1][2]["size"] == "2K"
    assert calls[1][2]["watermark"] is False
