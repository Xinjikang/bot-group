# 桌面图标

图形为正立三棱锥的线框投影：四个圆形顶点与六条连接棱。前方棱使用浅金色，后方横棱使用暗金色，在相交处留出遮挡间隙。底板为深蓝圆角矩形，外部透明。

## 资源

- 原图：[`frontend/assets/bot-group-icon.png`](../../frontend/assets/bot-group-icon.png)，1254 × 1254，RGBA。
- Windows 图标：[`frontend/assets/bot-group.ico`](../../frontend/assets/bot-group.ico)，包含 16、24、32、48、64、128、256 像素七种尺寸。
- 原图使用内置 imagegen 生成，ICO 使用 Pillow 格式转换并保留透明通道。

## 安装契约

桌面名称为 `Bot Group`，两个单词的首字母大写。`engine.maintenance.desktop.ensure_shortcut` 将安装目录内的 ICO 设为快捷方式的 `IconLocation`。重复安装与升级复用原有快捷方式，并更新图标引用和名称大小写；资源缺失时采用默认程序图标。图标与原图位于受管理的 `frontend/assets/`，随发布清单、源码安装包和升级文件管理交付。

验证使用 `scripts.test_desktop`：检查 ICO 目录中的七种尺寸、发布资源清单，以及真实 Windows Shell 链接从默认图标更新到自定义图标后仍保持原启动参数与数据目录。
