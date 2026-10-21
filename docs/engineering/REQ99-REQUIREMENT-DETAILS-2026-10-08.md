# 需求详情七字段展示

需求列表卡片提供详情查看，创建和编辑由 MCP req_create/req_update 承担。

详情依次展示标题、背景与目标、范围、方案与依据、验收标准、未决事项和来源关联，每项独立 section。读取 [需求数据契约](REQ99-REQUIREMENT-FIELDS-2026-09-30.md) 中的数据库字段 title/background_goal/scope/approach_basis/acceptance/undecided/source_ref；全部正文通过 textContent 渲染，保留换行并允许长文本换行。

空串、空白、null 和缺失字段显示“暂未填写<字段名>”。任何保留 description 的旧需求或部分补全需求额外展示“历史需求描述”，原文保留。source_ref 按存储原文展示 thread_id 与 message_id；格式验证见 [拆解门禁](REQ99-DECOMPOSITION-GATE-2026-10-08.md)。

验证命令：`node scripts/test_requirement_details.cjs`；`python -B scripts/test_requirement_details_browser.py`。Python 环境需安装 Playwright 及测试所需浏览器。浏览器测试全部使用模拟数据和本地资源，覆盖真实查看入口、刷新、七项展示、空值、HTML 注入防护及 390px 布局。沿用现有加载/失败/过期响应与关闭处理。

前端静态资源更新后刷新页面生效。
