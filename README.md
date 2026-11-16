# AI OCR Code Typer

Windows 截图识题、AI 生成代码与按键逐字输入工具（CodePilot）。

A Windows AI coding assistant with screenshot OCR, DeepSeek code generation, and key-triggered character-by-character typing.

支持 Windows 10/11，需要可用的 AI API Key 和网络连接。

## 从源码运行

安装 Python 3.12，在 PowerShell 中执行：

```powershell
git clone https://github.com/makealivi/ai-ocr-code-typer.git
cd ai-ocr-code-typer
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py
```

程序会打开配置网页。填写 API Key，选择语言并点击 **保存全部配置**，然后按照下方步骤使用。

## 构建 Windows 程序

完成上述依赖安装后，在项目目录执行：

```powershell
.\.venv\Scripts\python.exe -m pip install pyinstaller
powershell -ExecutionPolicy Bypass -File .\build.ps1
```

构建结果位于 `release/启动/`。分发时保留整个目录。

## 功能

- 按快捷键静默截取全屏，在本机 OCR 识别题目后请求 DeepSeek 生成代码，完整结果写入剪贴板。
- 每次按下 A–Z（大小写均可）或 `[];',./` 中任一键，输出代码的下一个字符，原按键不会输入编辑器。
- 网页可配置多套截图快捷键、编程语言、API Key、API 地址和模型，运行过程不弹窗。

## 使用方式

1. 从源码启动程序，或在构建后双击 `release/启动/启动.exe`，在网页填写 API Key，选择语言并点击 **保存全部配置**。API 地址默认为 `https://api.deepseek.com`，模型默认为 `deepseek-flash`。
2. 打开题目网页，按 **F1** 截取全屏，等待网页显示 **可以逐字输入**。
3. 切换到编辑器，按 A–Z 或 `[];',./` 逐字输出代码。
4. **F8** 暂停／恢复，暂停后可正常打字；**F9** 结束本轮，后台继续运行，已输入的文字和剪贴板代码保留。
5. 关闭网页后快捷键仍生效；再次双击启动文件可重开网页，点击 **退出程序** 关闭后台。

## 注意事项

- 由于编辑器会自动补 `}`，使用自动打字输入代码后，结尾会多几个 `}`，请注意手动删除。也可提前关闭编辑器的括号补全。
- 请关闭编辑器的自动缩进和自动格式化，使用英文输入模式，保持程序与编辑器的权限级别一致。
- 全屏截图包含全部显示器当时可见的内容，不会自动滚动网页；识别文字会发送到所配置的 API。
- 每次实际按下触发键仅输出一个字符，长按不会连续输出；Ctrl、Alt、Win 组合键保留原功能。
- 快捷键修改后需保存；接口或识别失败时，在网页查看错误并按提示处理。
- 支持 Windows 10/11；使用时保留启动文件所在的整个文件夹。API Key 保存在当前 Windows 用户的凭据管理器中。

## 许可证

[MIT License](LICENSE)
