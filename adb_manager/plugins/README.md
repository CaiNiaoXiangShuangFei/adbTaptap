# 脚本插件模组

每个插件使用一个独立目录，至少包含 `plugin.json` 和一个 Python 入口文件。

`plugin.json` 示例：

```json
{
  "id": "my_plugin",
  "name": "我的插件",
  "description": "插件用途说明",
  "version": "1.0.0",
  "entry": "run.py"
}
```

入口脚本会收到两个参数：

- `--device`：当前设备序列号
- `--adb`：程序动态找到的 ADB 可执行文件路径

插件不应硬编码 ADB 或设备路径。输出到标准输出的内容会进入对应设备的插件日志。插件退出码为 `0` 表示成功，非 `0` 表示失败。
