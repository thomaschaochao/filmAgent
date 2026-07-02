# FilmAgent 校验分支开发规则

本文件适用于当前目录及全部子目录。未来任何 Agent 或开发者修改本 worktree 时，都必须遵守以下规则。

## 分支边界

- 本 worktree 对应 `codex/validation-metrics`。
- 只处理校验、质量评估、运行记录、指标 API、评估界面及其测试和文档。
- 如果任务属于其他产品方向，不要直接混入本分支；应新建独立分支或 worktree。
- 不覆盖、不删除其他 Agent 的未提交修改。

## 每次修改的必做流程

1. 修改前运行 `git status --short`，确认工作区状态和现有改动归属。
2. 完成一个逻辑完整、测试通过的修改单元。
3. 更新 `VALIDATION_METRICS_ROADMAP.md`：
   - 将本次成果加入“当前已经完成”。
   - 更新测试状态。
   - 删除或调整已经解决的限制。
   - 更新后续计划和推荐起点。
4. 运行与改动风险匹配的测试；Python 代码至少运行相关 pytest 和 Ruff，前端脚本至少执行 JavaScript 语法检查。
5. 检查 `git diff`，确保没有密钥、数据库、生成图片、测试临时目录或其他无关文件。
6. 每次修改完成后必须创建一次 Git commit，不把已完成的修改长期留在未提交状态。

## Commit 规则

- 一个 commit 对应一个清晰的修改单元。
- commit message 使用英文 Conventional Commit，例如：
  - `feat: record model usage metrics`
  - `fix: preserve storyboard on validation failure`
  - `docs: update validation roadmap`
- 提交前必须确认测试结果；如果存在环境原因无法执行的测试，要在交接信息和路线图中说明。
- 不使用 `git reset --hard`、强制推送或覆盖他人历史，除非用户明确授权。
