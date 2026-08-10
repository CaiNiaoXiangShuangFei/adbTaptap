# Windows 桌面版

双击项目根目录的 `启动桌面版.bat`。首次启动会将 pywebview 及其 Python 依赖安装到项目的 `runtime/desktop-packages`，之后直接打开独立桌面窗口，不再启动外部浏览器。

## 架构

- 桌面窗口使用 Edge WebView2 内嵌现有管理页面。
- Python HTTP 服务与桌面窗口运行在同一个进程中并使用随机端口；桌面窗口通过 `127.0.0.1` 访问，同时保留原有手机扫码局域网访问能力。
- 关闭最后一个桌面窗口时，本地 HTTP 服务同步关闭。
- 使用 Windows 命名互斥量防止重复启动多个桌面实例。
- 窗口大小、位置、最大化和置顶状态保存在项目 `runtime/desktop/window_state.json`。
- WebView 用户数据保存在项目 `runtime/desktop/webview-data`。
- 桌面依赖、下载缓存和临时目录均位于项目 `runtime`，不包含固定盘符。

Windows 需要 Microsoft Edge WebView2 Runtime。Windows 11 通常已包含；缺失时请安装微软官方 Evergreen WebView2 Runtime。

原来的浏览器版仍然保留，可继续运行 `adb_manager/server.py` 并通过浏览器访问。
