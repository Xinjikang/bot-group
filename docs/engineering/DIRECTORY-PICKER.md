# 项目目录选择

项目主目录输入框旁的文件夹按钮打开网页内选择器，展示**引擎所在机器的服务器目录**。输入框继续支持粘贴及手动修改，最终路径由项目预览和保存流程校验。

## 交互

- 点击目录行进入子目录，面包屑支持逐级返回，“服务器根目录”返回盘符/根视图。
- 底部“选择此目录”确认当前位置并派发 input/change 事件；取消保留原输入。空目录加载成功后可确认，加载中和失败状态禁用确认。
- “加载更多”追加分页。异步导航只接受最后一次请求，关闭选择器后忽略迟到结果。目录权限错误显示提示，其他状态在页面内说明。
- 对话框居中且受视口约束；内容增长和窗口变化重新定位，底部操作保持可用。

## 接口

`GET /local/directory-list?path=&offset=&limit=` 返回当前目录与子目录列表，含 `navigable`、分页总数和 `has_more`。单次 limit 上限 500，path 为空返回根视图；文件内容保持在服务器。权限不足返回明确目录状态。

接口要求管理员身份。带 token 的 user 可远程使用；免密 dev 模式限制为回环与同源访问。实现位于 `engine/directory_list.py` 和前端目录选择模块。

兼容的 Windows 原生 `POST /local/directory-picker` 入口由 `engine/directory_picker.py` 管理，要求可交互 Windows 会话、管理员、回环和同源；同一时刻只打开一个窗口，取消保留原值。当前项目页面使用网页选择器。

## 回归

`test_directory_list.py` 验证接口鉴权、权限错误及分页；`test_directory_picker.py` 验证原生兼容入口；`test_directory_picker_browser.py` 验证路径回填、导航竞态、确认/取消、居中及窄屏布局。原生桌面探针需明确交互范围，见 [测试索引](../../scripts/TEST_INDEX.md)。
