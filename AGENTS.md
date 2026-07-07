# FilmAgent Agent 工作规范

本文件适用于整个仓库。用户明确要求：每完成一个独立、可验证的实施任务，都必须创建 Git commit 后再交付或进入下一个任务。

## 提交纪律

1. 将需求拆分为边界清楚的任务；一个任务完成并验证后立即提交，不累计多个无关功能。
2. 提交前检查 `git status` 和 diff，只暂存本任务有意修改的文件。保留并不得覆盖、清理或提交用户已有的无关改动。
3. 至少运行 `git diff --check`，并按改动范围执行相关检查：
   - Python：`.venv\Scripts\ruff check app tests`
   - 后端或工作流：`.venv\Scripts\pytest`
   - 前端 JavaScript：`node --check app/static/app.js`（本机提供 Node 时）
4. 检查失败时不得提交，除非用户明确要求提交当前失败状态；应先修复或向用户报告阻塞原因。
5. 使用简洁的 Conventional Commit 信息，例如 `feat: add scoped AI chat threads`、`fix: reject edits to locked shots`、`docs: add implementation handoff`。
6. 交付时报告 commit hash、实际运行的验证命令和结果，以及任何仍存在的限制或遗留问题。

## 计划进度更新

1. 每完成一个可验证步骤，必须同步更新当前任务计划，明确标记已完成、进行中和未完成的事项。
2. 如果用户提供了实施计划，应在交付时逐项说明哪些已经实现、哪些只是部分实现、哪些留待后续，不得用笼统的“已完成”覆盖未完成项。
3. 如果发现原计划中有依赖缺失、环境限制或需要另起任务的内容，应立即记录为遗留项，并在 commit 后的交付说明中列出。
4. 新会话接手时，应先读取最近的计划文档、handoff、`AGENTS.md` 和 git log，再继续推进，避免重复实现或跳过未完成步骤。
5. 对多阶段任务，应在阶段切换前更新计划状态；如果计划发生变化，应说明变更原因、影响范围和新的下一步。

## 安全与仓库卫生

- 禁止提交任何密钥或本地配置，包括 `config.local.env`、`.env`、`deepseekapi.txt` 及其内容。
- 禁止提交运行数据、数据库、生成图片、上传文件、缓存和测试临时目录，包括 `data/`、`storage/`、`.test-tmp*/`。
- 不得使用 `git reset --hard`、`git checkout --` 或其他会丢失用户改动的命令，除非用户明确授权该具体操作。
- 不修改或删除与当前任务无关的文件；发现脏工作区时，应绕开已有改动并只提交自己的文件。
- 不在日志、异常、接口响应、测试快照或文档中暴露 API Key。

## 完成定义

任务只有在实现符合需求、相关测试通过、diff 已检查且 commit 创建成功后才算完成。若因环境或外部依赖无法验证，应明确记录未验证项，不得把推测描述为已通过。

## 监控与校验文档

- 修改模型校验、运行记录、指标 API 或监控界面时，必须同步更新 `VALIDATION_METRICS_ROADMAP.md`。
- 所有 DeepSeek、OpenAI、Seedream 等外部模型调用都应进入统一监控，不得记录 Authorization 或 API Key。
- 监控与业务功能冲突时，保留业务能力，并以监控分支的数据记录和校验语义为准。
