# FilmAgent

FilmAgent 是一个本地优先的剧本到故事板提示词工作流。第一版覆盖：

1. 创建项目并保存剧本版本；
2. 使用 DeepSeek 生成或改写剧本；
3. 将已确认剧本拆解为结构化场次和分镜；
4. 管理人物、场景和道具资产，并使用 GPT Image 2 或 Seedream 生成参考图；
5. 编辑分镜并生成故事板图片提示词；
6. 所有中间数据保存在本地 SQLite。

## 安全约定

- `config.local.env`、`deepseekapi.txt`、`.env` 和本地数据库均不会进入 Git；
- API Key 只在服务端发送供应商请求时读取；
- 日志和接口响应不会返回 Key；
- 仓库仅提供不含真实密钥的配置模板。

## 配置 API Key

复制 `config.local.env.example` 为 `config.local.env`，然后在这个文件中填写需要使用的 DeepSeek、OpenAI 或火山方舟 Key。没有填写 Key 的供应商会在界面中显示为不可用。

修改配置后需要重启 FilmAgent。旧的 `deepseekapi.txt` 仍可继续使用。

## 本地启动

```powershell
py -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\python -m uvicorn app.main:app --reload
```

默认地址为 `http://127.0.0.1:8000`，接口文档位于 `/docs`。

## 当前 API

- `GET /api/v1/health`
- `GET /api/v1/image-providers`
- `POST /api/v1/projects`
- `GET /api/v1/projects`
- `GET /api/v1/projects/{project_id}`
- `POST /api/v1/projects/{project_id}/scripts`
- `POST /api/v1/projects/{project_id}/scripts/generate`
- `POST /api/v1/scripts/{script_id}/approve`
- `POST /api/v1/scripts/{script_id}/shots/generate`
- `GET /api/v1/projects/{project_id}/scenes`
- `GET /api/v1/assets/{asset_id}/images`
- `POST /api/v1/assets/{asset_id}/images/generate`
- `PATCH /api/v1/assets/{asset_id}/images/{image_id}/primary`
- `DELETE /api/v1/assets/{asset_id}/images/{image_id}`
- `PATCH /api/v1/shots/{shot_id}`
- `POST /api/v1/shots/{shot_id}/prompts/generate`
- `GET /api/v1/shots/{shot_id}/prompts`
