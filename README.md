# FilmAgent

FilmAgent 是一个本地优先的剧本到故事板提示词工作流。第一版覆盖：

1. 创建项目并保存剧本版本；
2. 使用 DeepSeek 生成或改写剧本；
3. 将已确认剧本拆解为结构化场次和分镜；
4. 编辑分镜并生成故事板图片提示词；
5. 所有中间数据保存在本地 SQLite。

## 安全约定

- `deepseekapi.txt`、`.env` 和本地数据库均不会进入 Git；
- DeepSeek Key 只在请求发送时从本地文件读取；
- 日志和接口响应不会返回 Key；
- 仓库仅提供不含真实密钥的 `.env.example`。

## 本地启动

```powershell
py -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\python -m uvicorn app.main:app --reload
```

默认地址为 `http://127.0.0.1:8000`，接口文档位于 `/docs`。

## 当前 API

- `GET /api/v1/health`
- `POST /api/v1/projects`
- `GET /api/v1/projects`
- `GET /api/v1/projects/{project_id}`
- `POST /api/v1/projects/{project_id}/scripts`
- `POST /api/v1/projects/{project_id}/scripts/generate`
- `POST /api/v1/scripts/{script_id}/approve`
- `POST /api/v1/scripts/{script_id}/shots/generate`
- `GET /api/v1/projects/{project_id}/scenes`
- `PATCH /api/v1/shots/{shot_id}`
- `POST /api/v1/shots/{shot_id}/prompts/generate`
- `GET /api/v1/shots/{shot_id}/prompts`

