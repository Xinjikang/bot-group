# Windows 批处理编码与换行

`start.bat`、`stop.bat`、`install.bat`、`bootstrap.bat` 的注释和提示使用 ASCII，文件保存为 CRLF，保留原有解释器选择与退出码语义。UTF-8 中文及 LF 组合在部分 CMD 代码页下可能导致解析错误。

入口先选 PATH 中的 `python`，缺少时使用 `py -3`；两者均缺少时提示安装 Python 3.11+ 并返回 1。`cd /d "%~dp0"` 保证在程序目录运行，install 默认执行 install，显式 install/repair 参数及子进程退出码继续传递。

`.gitattributes` 使用 `*.bat -text whitespace=cr-at-eol`，使 CRLF 作为原始字节进入 Git blob，检出与 `git archive` 保留这些字节。维护时保持 ASCII 和纯 CRLF。`tools/make_release.py` 原样复制文件到源码 ZIP，发布方式见 [源码安装指南](../guides/RELEASE-GUIDE.md)。

## 回归

`test_windows_bat.py` 使用 distlib PE launcher 构造只记录 argv/cwd/退出码的解释器替身，验证 CP936/CP65001、python/py 优先级、空格路径、缺解释器、失败退出码、Git 检出/归档及源码 ZIP 字节一致性。使用隔离目录和隐藏测试控制台。

运行 `python -X utf8 -B -m unittest scripts.test_windows_bat scripts.test_install scripts.test_bootstrap scripts.test_service scripts.test_release_upgrade scripts.test_mcp_lock -q`。`BAT_ENTRY_EVIDENCE_DIR` 可指定主目录测试报告路径保存合成收据，具体结果按次记录。
