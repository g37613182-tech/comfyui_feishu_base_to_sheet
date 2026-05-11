# ComfyUI Feishu Base To Sheet

一个 ComfyUI custom node：把飞书多维表格（Base/Bitable）里的记录读取出来，写入普通飞书电子表格（Sheet）。

## 安装

把 `comfyui_feishu_base_to_sheet` 目录复制到：

```text
ComfyUI/custom_nodes/comfyui_feishu_base_to_sheet
```

然后重启 ComfyUI。节点会出现在 `Feishu` 分类下，显示名为 `Feishu Base To Sheet`。

OpenAPI 请求只使用 Python 标准库；图片输入/输出会使用 ComfyUI 环境里通常自带的 `PIL`、`numpy` 和 `torch`。

## 飞书权限

你需要一个飞书自建应用，并准备好：

- `app_id`
- `app_secret`
- Base 文档可读权限
- Sheet 文档可编辑权限

常见需要开启的能力包括 Base 记录读取、Base 应用只读、电子表格写入等。权限开通后，记得把应用添加到目标 Base 和 Sheet 文档协作者里，否则可能会遇到 `91403 Forbidden`。

如果不想把密钥放到 ComfyUI 工作流里，可以在启动 ComfyUI 前设置环境变量：

```powershell
$env:FEISHU_APP_ID="cli_xxx"
$env:FEISHU_APP_SECRET="xxx"
```

然后节点里的 `app_id` / `app_secret` 留空即可。

## 参数

- `base_url_or_token`：Base URL，或 `/base/` 后面的 app token。
- `base_table_id`：Base 数据表 ID。也可以直接传带 `?table=tbl...` 的 Base URL，此项留空。
- `view_id`：可选。为空时读取全表；传入视图 ID 时按该视图读取。
- `field_names`：可选。为空时按 Base 字段顺序复制全部字段；也可以用逗号或换行指定字段名。
- `spreadsheet_url_or_token`：普通 Sheet URL，或 `/sheets/` 后面的 spreadsheet token。
- `sheet_id`：目标工作表 ID。若 Sheet URL 带 `?sheet=xxxx`，可以留空自动解析。
- `start_cell`：写入起点，比如 `A1`。
- `mode`：`overwrite` 覆盖指定范围，`append` 追加到目标表。
- `include_header`：是否写入字段名表头。
- `flatten_complex_values`：是否把人员、附件、链接、多选等复杂值压成字符串。关闭后会写入 JSON 字符串。
- `max_records`：最多读取多少条；`0` 表示不限制。

## 注意

- Base URL 里的 `/wiki/` token 不是 Base app token，本节点暂不自动解析 Wiki 链接；请使用真实 Base URL 或 app token。
- 附件字段会被写成可读字符串或 JSON，不会自动下载或上传附件。
- 日期字段会尽量从毫秒时间戳转为 `YYYY-MM-DD` 或 ISO 时间字符串。
- 节点会自动按 5000 行分批写入；飞书 Sheet 单次最多 100 列，超过时请用 `field_names` 选择需要搬运的字段。

## 写入图片到 Sheet 单元格

`Feishu Image To Sheet Cell v0.6.0` 可以把 ComfyUI 的 `IMAGE` 写入普通飞书 Sheet 的单个单元格。

典型连接方式：

```text
BAResourceConvert(output_type=图片) -> Feishu Image To Sheet Cell v0.6.0(image)
```

关键参数：

- `spreadsheet_url_or_token`：目标飞书普通表格 URL 或 token。
- `sheet_id`：目标工作表 ID；如果 URL 带 `?sheet=xxxx` 可以留空。
- `cell`：单元格位置，例如 `C2`。也可以写成 `sheet_id!C2`。
- `image_name`：写入图片名称，例如 `attachment.png`。

这个节点调用飞书 Sheet 的 `values_image` 接口，适合把 Base 附件下载后的图片写进 Sheet。非图片附件不能作为单元格图片写入，建议写文件名、JSON 或上传到云空间后写链接。

## 统一写入文本或图片

推荐使用 `Feishu Value To Sheet Cell v0.6.0`。它可以按行列写入文本或图片：

- `row`：目标行号，从 1 开始。
- `column`：目标列，支持 `C` 或 `3`，二者都会定位到 C 列。
- `mode`：`auto` / `text` / `image`。
- `text`：文本输入。
- `image`：ComfyUI `IMAGE` 输入。

`auto` 模式下，如果连接了 `image` 输入就写图片；否则写 `text`。写图片时仍然调用飞书 Sheet 的 `values_image` 接口，写文本时调用普通单元格写入接口。

## 读取 Sheet 单元格文本或图片

使用 `Feishu Sheet Cell Reader v0.6.0` 从普通飞书 Sheet 的指定单元格读取内容：

- `spreadsheet_url_or_token`：飞书普通表格 URL 或 spreadsheet token。
- `sheet_id`：工作表 ID；如果 URL 带 `?sheet=xxxx` 可以留空。
- `row`：目标行号，从 1 开始。
- `column`：目标列，支持 `C` 或 `3`。
- `read_mode`：`auto` / `text` / `image`。
- `date_time_render_option`：日期时间返回格式，默认 `FormattedString`。

输出包含：

- `text`：单元格文本；如果单元格返回的是复杂 JSON，会转成 JSON 字符串。
- `image`：ComfyUI `IMAGE`；没有图片时返回 1x1 空图。
- `status_json`：读取范围、版本、原始返回值、图片 token 等诊断信息。
- `has_image`：是否成功读到图片。

图片读取会优先识别单元格返回值中的 `box...` 素材 token，并额外查询同一单元格上的浮动图片。若 `read_mode=image` 仍然找不到 token，节点会自动把表格导出为 XLSX，并从 Excel 内嵌图片锚点中提取目标单元格图片。这个回退路径会稍慢，并需要应用有电子表格导出/下载相关权限；如果仍找不到图片，节点会返回空图和 `has_image=false`，同时把候选图片锚点写进 `status_json` 方便诊断。
