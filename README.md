# ComfyUI Feishu Base To Sheet

一个 ComfyUI custom node：把飞书多维表格（Base/Bitable）里的记录读取出来，写入普通飞书电子表格（Sheet）。

## 安装

把 `comfyui_feishu_base_to_sheet` 目录复制到：

```text
ComfyUI/custom_nodes/comfyui_feishu_base_to_sheet
```

然后重启 ComfyUI。节点会出现在 `Feishu` 分类下，显示名为 `Feishu Base To Sheet`。

本节点只使用 Python 标准库，不需要额外安装依赖。

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
