# 第三方组件

Windows 便携包包含 Python 3.12、Tcl/Tk 8.6、pywin32 以及 PyInstaller 的启动程序。
它们的许可证随便携包放在 `licenses` 文件夹；Tcl/Tk 自带资源中的许可证也予以保留。

- Python：https://www.python.org/ — PSF License，附带组件条款见 `Python.txt`。
- Tcl/Tk：https://www.tcl-lang.org/ — BSD 风格许可证，见 `Tcl.txt`、`Tk.txt`。
- pywin32：https://github.com/mhammond/pywin32 — 见 `pywin32-win32-license.txt` 和 `pywin32-win32com-License.txt`。
- PyInstaller：https://pyinstaller.org/ — GPL 及启动程序分发例外，见 `pyinstaller-COPYING.txt`。该例外允许将打包后的应用按应用自身条款分发。

`packaging/licenses/Tcl.txt` 来自 Tcl 官方仓库的 `core-8-6-branch/license.terms`，用于补齐 Windows Python 安装中未单独提供的 Tcl 许可证。

上述第三方许可证不替代本项目的代码授权。本项目当前尚未选择开源许可证。
