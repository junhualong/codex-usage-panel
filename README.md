# Codex 实时用量面板

Windows 桌面上的 Codex 本地用量面板：自动跟随主窗口当前聊天，显示 token、缓存命中率和近似生成速度。可随 Codex 打开、关闭，并保留面板位置和显示偏好。

这是独立辅助工具，与 OpenAI 没有隶属关系。统计来自本机日志；不需要 API Key，程序运行时不发送网络请求。

## 直接使用（推荐）

使用 **Windows 10 / 11 x64** 和本机 Codex 桌面应用。便携包内置运行环境，无需安装 Python、PowerShell 7 或其他依赖，也无需管理员权限。

1. 获取 `CodexUsagePanel-v1.0.0-windows-x64.zip`，把整个 `CodexUsagePanel` 文件夹解压到固定位置。
2. 双击 `CodexUsagePanel.exe`。入口会启动后台监听；Codex 已打开时显示面板，否则等待 Codex 打开。
3. 如需重启电脑后自动使用，勾选“登录 Windows 后自动待命，随 Codex 打开和关闭面板”。
4. 可点击“创建桌面快捷方式”。关闭启动设置窗口后，后台监听继续运行。

**不要只复制 EXE。** 同目录的 `_internal` 是运行所需组件。更新时先用“停止面板与监听”，再完整替换程序文件夹。移动目录前先取消旧位置的自动启动，再从新位置启用；不同安装的启动项不会相互覆盖。

项目：[junhualong/codex-usage-panel](https://github.com/junhualong/codex-usage-panel)。发布 ZIP 适合放在 GitHub Releases 的附件中；源码的“Download ZIP”不能代替便携包。仓库也提供手动运行的 GitHub Actions 构建流程，产物在该次运行的 Artifacts 中。

当前构建未做数字签名。支持目标为 Windows 10/11 x64；本地已验证 Windows 11，尚未完成其他电脑或干净虚拟机上的验收。

## 自动识别与设置

首次运行无需填写路径。默认读取：

- 会话日志：`%USERPROFILE%\.codex\sessions`；设置了 `CODEX_HOME` 时使用其下的 `sessions`。
- Codex 导航日志：`%LOCALAPPDATA%\Codex\Logs`。
- 工具配置和状态：`%LOCALAPPDATA%\CodexUsagePanel`。

运行设置、窗口位置、诊断日志均写入用户目录，不写进程序安装目录。可从启动设置中打开配置目录。便携包不包含作者的聊天、配置、账户或快捷方式。

高级用户可修改生成的 `config.json`；空字符串表示自动识别：

| 字段 | 用途 |
| --- | --- |
| `codex_home` | 自定义 Codex 数据目录；空值时优先使用 `CODEX_HOME` |
| `sessions_root` | 自定义会话日志目录，优先于 `codex_home` |
| `desktop_logs_root` | 自定义桌面导航日志目录 |
| `refresh_ms` | 日志检查间隔，250～60000 毫秒，默认 1000 |
| `auto_follow` | 自动跟随当前本地聊天，默认 true |
| `follow_window_id` | 主窗口编号，默认 1 |

路径支持 `%环境变量%` 和 `~`。修改配置后停止监听并重新启动。源代码模式下，如果项目根目录已经有 `config.json`，会优先保留其配置和旧状态位置，不自动覆盖。

## 面板和统计口径

- 累计/单次：切换会话累计与最近一次模型调用。
- 总 token = 输入 + 输出；缓存输入已包含在输入中，推理输出已包含在输出中。
- 缓存命中率 = 缓存命中输入 / 输入。没有有效计数时显示“—”。
- `≈ tok/s` 为本轮最近一次已结算调用的平均输出速度，包含推理与工具参数输出，排除工具执行时间。它不是逐 token 的瞬时速度。
- 每轮开始清空上一轮速度，缺少计时信息的日志显示“—”。日志通常在模型调用结束后写入计数。
- 点“明细”展开，再点“收起”返回原来的紧凑位置；靠近底部时向上展开。拖动空白处移动，右键可打开启动设置。
- 关闭 Codex 主窗口后面板通常在约 1～2 秒内退出；后台监听继续等待重开。最小化 Codex 不关闭面板。
- 手动关闭面板后，本次 Codex 打开期间不会反复弹出；可用入口的“启动 / 显示面板”重新显示。

目前只支持主窗口的本地聊天。远程主机、云端聊天和多个主窗口的独立跟随尚未实现。累计 token 不等于当前上下文占用，缓存命中率也不代表费用折扣。Codex 日志格式变化时可能需要适配。

## 停止和移除

临时停止：启动设置 → “停止面板与监听”。

完全移除：取消自动启动 → 停止面板与监听 → 删除程序文件夹和桌面快捷方式。个人配置在 `%LOCALAPPDATA%\CodexUsagePanel`，由用户决定是否保留；它不属于 Codex 本身的数据目录。

## 从源码运行

开发环境使用 Windows x64、Python 3.12（包含 Tk）。在仓库目录执行：

```powershell
python -m pip install -r requirements.txt
python -B -X utf8 launcher.py
```

通用入口与便携 EXE 使用相同逻辑。历史的 `usage_panel.py` 和 `codex_autostart.py` 命令仍兼容；`start_watcher.ps1` 仅保留为旧版本参考，当前运行不再调用它。

常用命令（EXE 可换成 `python launcher.py`）：

```powershell
.\CodexUsagePanel.exe --start
.\CodexUsagePanel.exe --shutdown
.\CodexUsagePanel.exe --install
.\CodexUsagePanel.exe --disable
.\CodexUsagePanel.exe --inspect --report .\diagnostics.json
```

`--install` 只注册本用户登录启动；`--start` 启动监听；`--disable` 取消该安装的登录启动并停止监听，现有面板可用 `--shutdown` 退出。诊断报告包含本机路径，分享前请自行检查。

## 构建便携包

在仓库目录建立本地构建环境：

```powershell
python -m venv .local\build-venv
.\.local\build-venv\Scripts\python.exe -m pip install -r requirements-build.txt
.\.local\build-venv\Scripts\python.exe -B -X utf8 tools\run_tests.py
.\.local\build-venv\Scripts\python.exe -B -X utf8 tools\build_windows.py
```

构建产物：

- `dist/CodexUsagePanel/`：可直接运行的完整文件夹。
- `dist/CodexUsagePanel-v1.0.0-windows-x64.zip`：给其他人的便携包。
- `dist/SHA256SUMS.txt`：ZIP 的 SHA-256 校验值。

构建只收集程序、运行时和允许分发的说明文件，并检查个人配置、会话日志是否误入包。中间文件放在 `.local/build`；`.local`、`dist`、本机配置和快捷方式都被 Git 忽略。第三方许可见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)，本项目自身的开源许可证尚未选择。

## 验证和诊断

`tools/run_tests.py` 运行逻辑测试。下面的 Windows 桌面集成测试会显示短暂测试窗口，使用 `.local/tests` 中的隔离样本：

```powershell
python -B -X utf8 tests\gui_lifecycle_test.py
python -B -X utf8 tests\test_panel_geometry.py
python -B -X utf8 tests\test_restart_survival.py
python -B -X utf8 tests\test_portable.py
```

其中进程生命周期的源码测试使用常规 Python 安装运行；虚拟环境的 `pythonw.exe` 重定向进程会改变测试中的 PID 关系。重启生存测试要求 Codex 保持打开，只结束自己创建的测试 Job，不关闭真实 Codex。便携包测试需先完成构建。

无法启动时查看数据目录内的 `launcher-error.log`；旧入口可能使用 `autostart-error.log`，面板回调错误使用 `panel-error.log`。自定义 Codex 数据目录且未继承环境变量时，在配置中显式填写 `codex_home`。

后台进程通过 [Windows WMI](https://learn.microsoft.com/en-us/windows/win32/cimwin32prov/create-method-in-class-win32-process) 独立启动，避免被调用方的 Job 一同终止；打包目录依据 [PyInstaller 运行时路径规则](https://pyinstaller.org/en/stable/runtime-information.html) 区分程序与用户数据。
