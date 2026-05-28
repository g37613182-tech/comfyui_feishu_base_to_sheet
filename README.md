# ComfyUI Feishu Sheet Tools

用于在 ComfyUI 中读取和写入飞书普通电子表格（Sheet）的自定义节点。
安装后在 `Feishu` 分类下只会出现两个正式节点：

- `Feishu Sheet Reader v1.1.1`
- `Feishu Sheet Writer v1.1.1`

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
- 读取内嵌单元格图片时，可能还需要电子表格导出/下载相关权限
- 写入或读取视频文件引用时，可能还需要云空间文件上传/下载相关权限

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
- `read_mode`：`auto` / `text` / `image` / `video`。
- `date_time_render_option`：日期时间返回格式。
- `timeout_seconds`：请求、下载和导出超时时间。

输出：

- `text`：文本值；复杂值会转成 JSON 字符串。
- `image`：ComfyUI `IMAGE`；没有图片时返回 1x1 占位图。
- `status_json`：诊断信息，包含原始单元格值、图片/视频 token、导出路径等。
- `has_image`：是否成功读到真实图片。
- `video_path_or_url`：视频 URL，或下载到本机临时目录后的视频路径。
- `has_video`：是否成功识别到视频链接或文件引用。

图片读取逻辑：

- 识别 `box...`、`fileToken`、`file_token`，也支持飞书把图片对象作为 JSON 字符串返回的情况。
- 优先尝试直接下载图片素材。
- 再查询同单元格的浮动图片。
- 必要时导出 Sheet 为 XLSX，并从 Excel 图片锚点中提取目标单元格图片。

视频读取逻辑：

- 普通 Sheet 里的视频通常是一个可点击的 URL 或云空间文件引用，不是原生视频单元格。
- `auto` 模式会识别常见视频扩展名或 MIME 信息，例如 `.mp4`、`.mov`、`.webm`。
- `video` 模式会更激进地把单元格里的文件引用当成视频尝试解析。
- 如果能下载云空间文件，会输出本地临时视频路径；如果下载失败，会尽量输出可点击的文件链接并把错误放进 `status_json`。

## Feishu Sheet Writer

按行列写入普通 Sheet 的一个单元格。

主要输入：

- `spreadsheet_url_or_token`：Sheet URL 或 spreadsheet token。
- `sheet_id`：工作表 ID；如果 URL 带 `?sheet=xxxx`，可以留空。
- `row`：行号，从 1 开始。
- `column`：列号，支持 `C` 或 `3`。
- `mode`：`auto` / `text` / `image` / `video`。
- `text`：要写入的文本。
- `image`：要写入的 ComfyUI `IMAGE`。
- `image_name`：写入飞书时使用的图片名称。
- `video`：可选，直接连接 ComfyUI 视频/资源类输出；节点会尝试从常见字段里解析本地路径或 URL。
- `video_path_or_url`：视频 URL 或本地视频文件路径。
- `video_name`：写入飞书时使用的视频名称。
- `drive_folder_token`：本地视频上传到飞书云空间时使用的目标文件夹 token 或文件夹链接；留空时尝试上传到云空间根目录。

`auto` 模式优先级：

1. 如果填写了 `video_path_or_url`，写视频链接或视频文件引用。
2. 如果连接了 `image` 输入，写图片。
3. 否则写 `text`。

视频写入逻辑：

- `video_path_or_url` 是 `http://` 或 `https://` 时，会写成 Sheet 富文本 URL 对象。
- `video_path_or_url` 是本地文件路径时，会先上传到飞书云空间，再把云空间文件引用写入单元格。
- 从另一个 Sheet 搬视频时，可以把 Reader 的 `video_path_or_url` 直接接到 Writer 的 `video_path_or_url`。
- 从 ComfyUI 生成视频写回 Sheet 时，优先把生成视频节点的输出接到 Writer 的 `video`；如果它输出的是字符串路径，也可以接到 `video_path_or_url`。
- `drive_folder_token` 建议填一个你能访问的飞书云空间文件夹 token 或文件夹链接；留空会尝试上传到云空间根目录，但有些租户/应用权限下根目录文件不一定方便找到。
- 直接上传本地视频目前限制为 20 MB；更大的视频建议先上传到飞书云空间，或者传入一个公开视频/飞书文件 URL。

## 注意

- `/wiki/` 链接不会自动解析成真实 Sheet token，请使用真实 `/sheets/` URL 或 spreadsheet token。
- 读取内嵌单元格图片可能比读取文本慢，因为飞书普通读取接口不总是暴露可直接下载的图片 token。
- 读写视频文件引用需要云空间相关权限；如果只写 URL，一般只需要 Sheet 写权限。
- 出问题时优先查看 `status_json`，里面有版本号、读取/写入范围、原始返回值和查找路径。
