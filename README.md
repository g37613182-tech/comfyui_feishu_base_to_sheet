# ComfyUI Feishu Sheet Tools

用于在 ComfyUI 中读取和写入飞书普通电子表格（Sheet）的自定义节点。

安装后在 `Feishu` 分类下只会出现两个正式节点：

- `Feishu Sheet Reader v1.0.0`
- `Feishu Sheet Writer v1.0.0`

## 安装

把本目录复制到：

```text
ComfyUI/custom_nodes/comfyui_feishu_base_to_sheet
```

然后重启 ComfyUI。

OpenAPI 请求只使用 Python 标准库；图片输入/输出会使用 ComfyUI 环境通常自带的 `PIL`、`numpy` 和 `torch`。

## 飞书应用

你需要准备一个飞书自建应用：

- `app_id`
- `app_secret`
- 目标 Sheet 可读权限
- 目标 Sheet 可写权限
- 如果要读取单元格内嵌图片，可能还需要电子表格导出/下载相关权限

权限开通后，记得把应用添加到目标 Sheet 协作者里，否则可能遇到 `91403 Forbidden`。

如果不想把密钥放到 ComfyUI 工作流中，可以在启动 ComfyUI 前设置环境变量：

```powershell
$env:FEISHU_APP_ID="cli_xxx"
$env:FEISHU_APP_SECRET="xxx"
```

然后节点里的 `app_id` / `app_secret` 留空即可。

## Feishu Sheet Reader

按行列读取普通 Sheet 的一个单元格。

主要输入：

- `spreadsheet_url_or_token`：Sheet URL 或 spreadsheet token。
- `sheet_id`：工作表 ID；如果 URL 带 `?sheet=xxxx`，可以留空。
- `row`：行号，从 1 开始。
- `column`：列号，支持 `B` 或 `2`。
- `read_mode`：`auto` / `text` / `image`。
- `date_time_render_option`：日期时间返回格式。
- `timeout_seconds`：请求和导出超时时间。

输出：

- `text`：文本值；复杂值会转成 JSON 字符串。
- `image`：ComfyUI `IMAGE`；没有图片时返回 1x1 占位图。
- `status_json`：诊断信息，包含原始单元格值、图片 token、导出路径等。
- `has_image`：是否成功读到真实图片。

图片读取逻辑：

- 识别 `box...`、`fileToken`、`file_token`，也支持飞书把图片对象作为 JSON 字符串返回的情况。
- 优先尝试直接下载图片素材。
- 再查询同单元格的浮动图片。
- 必要时导出 Sheet 为 XLSX，并从 Excel 图片锚点中提取目标单元格图片。

## Feishu Sheet Writer

按行列写入普通 Sheet 的一个单元格。

主要输入：

- `spreadsheet_url_or_token`：Sheet URL 或 spreadsheet token。
- `sheet_id`：工作表 ID；如果 URL 带 `?sheet=xxxx`，可以留空。
- `row`：行号，从 1 开始。
- `column`：列号，支持 `C` 或 `3`。
- `mode`：`auto` / `text` / `image`。
- `text`：要写入的文本。
- `image`：要写入的 ComfyUI `IMAGE`。
- `image_name`：写入飞书时使用的图片名称。

`auto` 模式下，如果连接了 `image` 输入就写图片；否则写 `text`。

## 注意

- `/wiki/` 链接不会自动解析成真实 Sheet token，请使用真实 `/sheets/` URL 或 spreadsheet token。
- 读取内嵌单元格图片可能比读取文本慢，因为飞书普通读取接口不总是暴露可直接下载的图片 token。
- 出问题时优先查看 `status_json`，里面有版本号、读取范围、原始返回值和图片查找路径。
