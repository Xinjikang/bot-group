# 思考缓存生命周期与服务端背景存储

思考缓存使用有界 localStorage 快照，自定义背景保存到服务端。刷新页面保留浏览器缓存；缓存写入失败时保留旧快照和当前内存内容。

## 自定义背景

- 图片保存在引擎数据目录 `appearance/background`，同一服务共用一张。GET/PUT/DELETE `/appearance/background` 复用管理员鉴权，写操作检查同源。浏览器通过鉴权请求读取图片并创建临时 Blob URL。
- 临时文件加原子替换，保存失败保留原图。恢复默认写入零字节标记，释放图片空间，并阻止旧浏览器数据再次迁入。默认 `frontend/assets/viewport-planet.jpg` 及默认显示样式保持原样。
- 原 localStorage `ge_viewport_background` 条件迁移到服务端，成功后删除旧键。已有服务端设置优先。PNG/JPEG/WebP 最大 10 MiB、2500 万像素，服务端验证格式并实际解码。
- 初始化和保存期间禁用选择及重置，关闭按钮可用；错误以醒目 alert 显示。登录后自动重新读取。

## 思考缓存

- 已结束运行保留 7 天，最多 100 次；正在运行优先，按最近活动时间排序。
- 序列化快照最多 512Ki 个 UTF-16 码元，约 1MiB。发生配额错误后逐级减半，最低 4096 码元；失败保留旧快照，连续错误只记录一次警告。
- 超大优先运行保存最近 8 个气泡、每气泡最多 32 条记录，长正文和工具参数分别保留末尾 4000 / 2000 字符，仍超限继续收缩；恢复后的面板显示截断提示。增量游标及气泡关联保留。
- 保存和恢复时清理；读取旧快照后立即写回受限快照。旧数据首次升级获得 7 天宽限，之后保存已结束内容不会延期。
- 管理范围为浏览器持久化快照。当前页面完整思考、其他设置及 Hermes/Codex 会话源记录保持原样。完全无空间时保留内存，空间恢复后下一次保存重试。

## 验证与生效

- `node scripts/test_thinking_cache.cjs`：TTL、数量/体积限制、优先级、截断、配额降级、保留旧快照、告警限流。
- `node scripts/test_thinking_lifecycle.cjs`：已有思考恢复与回复关联。
- `python -X utf8 -B scripts/test_thinking_cache_browser.py`：真实 Chrome 满额后裁剪、刷新恢复；完全无空间时保留内存、告警限流及空间释放后重试。
- `python -X utf8 -B -m unittest scripts.test_background_storage -v`：实际接口鉴权、同源、校验、原子替换失败保留原图及重置；临时数据目录。
- `python -X utf8 -B scripts/test_background_storage_browser.py`：隔离 Chrome + 实际 API 验证保存、刷新、新浏览器读取、旧背景迁移与恢复默认。
- `python -X utf8 -B scripts/test_appearance_browser.py`：深浅主题、保存失败、默认背景和移动端布局。

上线需要手动重启引擎后刷新页面。隔离测试通过与生产页面验收分开记录。
