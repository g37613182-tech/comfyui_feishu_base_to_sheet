import json
import io
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


NODE_VERSION = "0.3.0"


class FeishuAPIError(RuntimeError):
    pass


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _read_secret(value: str, env_name: str) -> str:
    value = _clean_text_input(value)
    if value:
        return value
    return os.getenv(env_name, "").strip()


def _clean_text_input(value: str) -> str:
    value = (value or "").strip()
    if re.fullmatch(r"请输入[\w_]+", value):
        return ""
    return value


def _extract_token(value: str, patterns: Sequence[str]) -> str:
    value = _clean_text_input(value)
    if not value:
        return ""
    if "://" not in value:
        return value

    parsed = urllib.parse.urlparse(value)
    path = parsed.path or ""
    for pattern in patterns:
        match = re.search(pattern, path)
        if match:
            return match.group(1)
    return ""


def _extract_query_value(value: str, key: str) -> str:
    if "://" not in (value or ""):
        return ""
    parsed = urllib.parse.urlparse(value)
    return urllib.parse.parse_qs(parsed.query).get(key, [""])[0]


def _first_query_value(value: str, keys: Sequence[str]) -> str:
    for key in keys:
        item = _extract_query_value(value, key)
        if item:
            return item
    return ""


def _split_names(raw: str) -> List[str]:
    raw = _clean_text_input(raw)
    if not raw:
        return []
    parts = re.split(r"[\n,，]+", raw)
    return [part.strip() for part in parts if part.strip()]


def _col_to_number(col: str) -> int:
    result = 0
    for char in col.upper():
        if not ("A" <= char <= "Z"):
            raise ValueError(f"Invalid column letter: {col}")
        result = result * 26 + (ord(char) - ord("A") + 1)
    return result


def _number_to_col(number: int) -> str:
    if number < 1:
        raise ValueError("Column number must be >= 1")
    chars = []
    while number:
        number, rem = divmod(number - 1, 26)
        chars.append(chr(ord("A") + rem))
    return "".join(reversed(chars))


def _parse_cell(cell: str) -> Tuple[int, int]:
    cell = (cell or "A1").strip()
    if "!" in cell:
        cell = cell.split("!", 1)[1]
    match = re.fullmatch(r"\$?([A-Za-z]+)\$?([1-9][0-9]*)", cell)
    if not match:
        raise ValueError("start_cell must look like A1, B2, or sheet_id!A1")
    return _col_to_number(match.group(1)), int(match.group(2))


def _make_range(sheet_id: str, start_cell: str, rows: int, cols: int) -> str:
    if rows < 1 or cols < 1:
        raise ValueError("Cannot write an empty range")
    explicit_sheet = ""
    if "!" in (start_cell or ""):
        explicit_sheet, start_cell = start_cell.split("!", 1)
    sheet = (sheet_id or explicit_sheet).strip()
    if not sheet:
        raise ValueError("sheet_id is required unless start_cell includes sheet_id!A1")

    start_col, start_row = _parse_cell(start_cell)
    end_col = start_col + cols - 1
    end_row = start_row + rows - 1
    return f"{sheet}!{_number_to_col(start_col)}{start_row}:{_number_to_col(end_col)}{end_row}"


def _offset_cell(start_cell: str, row_offset: int) -> str:
    start_col, start_row = _parse_cell(start_cell)
    return f"{_number_to_col(start_col)}{start_row + row_offset}"


def _chunks(rows: List[List[Any]], size: int) -> Iterable[List[List[Any]]]:
    for index in range(0, len(rows), size):
        yield rows[index : index + size]


def _looks_like_epoch_ms(value: Any) -> bool:
    return isinstance(value, (int, float)) and value > 100000000000


def _format_epoch_ms(value: Any) -> str:
    try:
        dt = datetime.fromtimestamp(float(value) / 1000.0, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return str(value)
    if dt.hour == 0 and dt.minute == 0 and dt.second == 0:
        return dt.strftime("%Y-%m-%d")
    return dt.isoformat().replace("+00:00", "Z")


def _compact_dict_value(value: Dict[str, Any]) -> str:
    for key in ("text", "name", "email", "title", "url", "link"):
        item = value.get(key)
        if isinstance(item, str) and item:
            return item
    return _json_dumps(value)


def _to_sheet_cell(value: Any, field: Optional[Dict[str, Any]], flatten_complex: bool) -> Any:
    if value is None:
        return ""

    field_type = None if not field else field.get("type")
    if field_type == 5 and _looks_like_epoch_ms(value):
        return _format_epoch_ms(value)

    if isinstance(value, (str, int, float, bool)):
        return value

    if not flatten_complex:
        return _json_dumps(value)

    if isinstance(value, list):
        if not value:
            return ""
        compacted = []
        for item in value:
            if item is None:
                continue
            if isinstance(item, (str, int, float, bool)):
                compacted.append(str(item))
            elif isinstance(item, dict):
                compacted.append(_compact_dict_value(item))
            else:
                compacted.append(_json_dumps(item))
        return ", ".join(compacted)

    if isinstance(value, dict):
        return _compact_dict_value(value)

    return str(value)


def _image_to_png_bytes(image: Any) -> bytes:
    from PIL import Image
    import numpy as np

    if isinstance(image, (list, tuple)):
        if not image:
            raise ValueError("image input is empty")
        image = image[0]

    if hasattr(image, "detach"):
        image = image.detach().cpu().numpy()

    array = np.asarray(image)
    if array.ndim == 4:
        array = array[0]
    if array.ndim == 3 and array.shape[0] in (1, 3, 4) and array.shape[-1] not in (1, 3, 4):
        array = np.transpose(array, (1, 2, 0))
    if array.ndim == 2:
        pass
    elif array.ndim != 3 or array.shape[-1] not in (1, 3, 4):
        raise ValueError(f"Unsupported IMAGE shape for Sheet image write: {array.shape}")

    if np.issubdtype(array.dtype, np.floating):
        array = np.clip(array, 0.0, 1.0) * 255.0
    array = np.clip(array, 0, 255).astype(np.uint8)
    if array.ndim == 3 and array.shape[-1] == 1:
        array = array[:, :, 0]

    buffer = io.BytesIO()
    Image.fromarray(array).save(buffer, format="PNG")
    return buffer.getvalue()


class FeishuOpenAPI:
    def __init__(self, domain: str, timeout: int) -> None:
        self.domain = (domain or "https://open.feishu.cn").rstrip("/")
        self.timeout = max(5, int(timeout or 30))

    def request(
        self,
        method: str,
        path: str,
        token: Optional[str] = None,
        query: Optional[Dict[str, Any]] = None,
        body: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        query = {key: value for key, value in (query or {}).items() if value not in (None, "")}
        url = f"{self.domain}{path}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"

        headers = {"Content-Type": "application/json; charset=utf-8"}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        data = None
        if body is not None:
            data = _json_dumps(body).encode("utf-8")

        last_error = None
        for attempt in range(3):
            req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read().decode("utf-8")
            except urllib.error.HTTPError as exc:
                raw = exc.read().decode("utf-8", errors="replace")
                last_error = self._format_http_error(exc.code, raw)
                if exc.code not in (429, 500, 502, 503, 504) or attempt == 2:
                    raise FeishuAPIError(f"FeishuBaseToSheet v{NODE_VERSION}: {last_error}") from exc
                time.sleep(0.8 * (attempt + 1))
                continue
            except urllib.error.URLError as exc:
                last_error = f"Network error while calling Feishu OpenAPI: {exc.reason}"
                if attempt == 2:
                    raise FeishuAPIError(f"FeishuBaseToSheet v{NODE_VERSION}: {last_error}") from exc
                time.sleep(0.8 * (attempt + 1))
                continue

            payload = json.loads(raw) if raw else {}
            code = payload.get("code", 0)
            if code != 0:
                msg = payload.get("msg") or payload.get("message") or "Feishu OpenAPI returned an error"
                raise FeishuAPIError(f"FeishuBaseToSheet v{NODE_VERSION}: Feishu OpenAPI error code={code}: {msg}")
            return payload

        raise FeishuAPIError(f"FeishuBaseToSheet v{NODE_VERSION}: {last_error or 'Unknown Feishu OpenAPI error'}")

    @staticmethod
    def _format_http_error(code: int, raw: str) -> str:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return f"HTTP {code}: {raw[:500]}"
        msg = payload.get("msg") or payload.get("message") or raw[:500]
        api_code = payload.get("code")
        return f"HTTP {code}, Feishu code={api_code}: {msg}"

    def tenant_access_token(self, app_id: str, app_secret: str) -> str:
        payload = self.request(
            "POST",
            "/open-apis/auth/v3/tenant_access_token/internal",
            body={"app_id": app_id, "app_secret": app_secret},
        )
        token = payload.get("tenant_access_token")
        if not token:
            raise FeishuAPIError("tenant_access_token missing from auth response")
        return token

    def list_fields(self, token: str, app_token: str, table_id: str) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        page_token = ""
        while True:
            payload = self.request(
                "GET",
                f"/open-apis/bitable/v1/apps/{urllib.parse.quote(app_token, safe='')}/tables/{urllib.parse.quote(table_id, safe='')}/fields",
                token=token,
                query={"page_size": 100, "page_token": page_token},
            )
            data = payload.get("data", {})
            items.extend(data.get("items", []))
            if not data.get("has_more"):
                return items
            page_token = data.get("page_token", "")

    def search_records(
        self,
        token: str,
        app_token: str,
        table_id: str,
        view_id: str,
        field_names: Sequence[str],
        page_size: int,
        max_records: int,
    ) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        page_token = ""
        page_size = min(max(1, int(page_size or 500)), 500)

        body: Dict[str, Any] = {"automatic_fields": False}
        if view_id:
            body["view_id"] = view_id
        if field_names:
            body["field_names"] = list(field_names)

        while True:
            payload = self.request(
                "POST",
                f"/open-apis/bitable/v1/apps/{urllib.parse.quote(app_token, safe='')}/tables/{urllib.parse.quote(table_id, safe='')}/records/search",
                token=token,
                query={
                    "page_size": page_size,
                    "page_token": page_token,
                    "text_field_as_array": "false",
                    "user_id_type": "open_id",
                },
                body=body,
            )
            data = payload.get("data", {})
            items.extend(data.get("items", []))
            if max_records > 0 and len(items) >= max_records:
                return items[:max_records]
            if not data.get("has_more"):
                return items
            page_token = data.get("page_token", "")

    def write_values(
        self,
        token: str,
        spreadsheet_token: str,
        cell_range: str,
        values: List[List[Any]],
        mode: str,
    ) -> Dict[str, Any]:
        spreadsheet_token = urllib.parse.quote(spreadsheet_token, safe="")
        if mode == "append":
            payload = self.request(
                "POST",
                f"/open-apis/sheets/v2/spreadsheets/{spreadsheet_token}/values_append",
                token=token,
                body={"valueRange": {"range": cell_range, "values": values}},
            )
        else:
            payload = self.request(
                "POST",
                f"/open-apis/sheets/v2/spreadsheets/{spreadsheet_token}/values_batch_update",
                token=token,
                body={"valueRanges": [{"range": cell_range, "values": values}]},
            )
        return payload.get("data", {})

    def write_image(
        self,
        token: str,
        spreadsheet_token: str,
        cell_range: str,
        image_bytes: bytes,
        image_name: str,
    ) -> Dict[str, Any]:
        spreadsheet_token = urllib.parse.quote(spreadsheet_token, safe="")
        payload = self.request(
            "POST",
            f"/open-apis/sheets/v2/spreadsheets/{spreadsheet_token}/values_image",
            token=token,
            body={
                "range": cell_range,
                "image": list(image_bytes),
                "name": image_name or "image.png",
            },
        )
        return payload.get("data", {})


class FeishuBaseToSheet:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "app_id": ("STRING", {"default": ""}),
                "app_secret": ("STRING", {"default": ""}),
                "base_url_or_token": ("STRING", {"default": ""}),
                "base_table_id": ("STRING", {"default": ""}),
                "spreadsheet_url_or_token": ("STRING", {"default": ""}),
                "sheet_id": ("STRING", {"default": ""}),
                "start_cell": ("STRING", {"default": "A1"}),
                "mode": (["overwrite", "append"], {"default": "overwrite"}),
                "include_header": ("BOOLEAN", {"default": True}),
                "flatten_complex_values": ("BOOLEAN", {"default": True}),
                "max_records": ("INT", {"default": 0, "min": 0, "max": 1000000, "step": 1}),
                "page_size": ("INT", {"default": 500, "min": 1, "max": 500, "step": 1}),
                "timeout_seconds": ("INT", {"default": 30, "min": 5, "max": 300, "step": 1}),
            },
            "optional": {
                "view_id": ("STRING", {"default": ""}),
                "field_names": ("STRING", {"default": "", "multiline": True}),
                "openapi_domain": ("STRING", {"default": "https://open.feishu.cn"}),
            },
        }

    RETURN_TYPES = ("STRING", "INT", "INT")
    RETURN_NAMES = ("status_json", "rows_written", "columns_written")
    FUNCTION = "copy"
    CATEGORY = "Feishu"
    OUTPUT_NODE = True

    @classmethod
    def IS_CHANGED(cls, *args, **kwargs):
        return time.time()

    def copy(
        self,
        app_id: str,
        app_secret: str,
        base_url_or_token: str,
        base_table_id: str,
        spreadsheet_url_or_token: str,
        sheet_id: str,
        start_cell: str,
        mode: str,
        include_header: bool,
        flatten_complex_values: bool,
        max_records: int,
        page_size: int,
        timeout_seconds: int,
        view_id: str = "",
        field_names: str = "",
        openapi_domain: str = "https://open.feishu.cn",
    ) -> Tuple[str, int, int]:
        app_id = _read_secret(app_id, "FEISHU_APP_ID")
        app_secret = _read_secret(app_secret, "FEISHU_APP_SECRET")
        base_url_or_token = _clean_text_input(base_url_or_token)
        base_table_id = _clean_text_input(base_table_id)
        spreadsheet_url_or_token = _clean_text_input(spreadsheet_url_or_token)
        sheet_id = _clean_text_input(sheet_id)
        start_cell = _clean_text_input(start_cell) or "A1"
        view_id = _clean_text_input(view_id)
        field_names = _clean_text_input(field_names)
        openapi_domain = _clean_text_input(openapi_domain) or "https://open.feishu.cn"
        if not app_id or not app_secret:
            raise ValueError("app_id/app_secret are required, or set FEISHU_APP_ID and FEISHU_APP_SECRET")

        if "/wiki/" in (base_url_or_token or ""):
            raise ValueError("Wiki links are not Base app tokens. Open the real Base URL and use its /base/... link or app token.")
        app_token = _extract_token(base_url_or_token, [r"/base/([^/?#]+)", r"/bitable/([^/?#]+)"])
        table_id = (base_table_id or _first_query_value(base_url_or_token, ["table", "table_id"])).strip()
        view_id = (view_id or _first_query_value(base_url_or_token, ["view", "view_id"])).strip()
        spreadsheet_token = _extract_token(spreadsheet_url_or_token, [r"/sheets/([^/?#]+)"])
        sheet_id = (sheet_id or _first_query_value(spreadsheet_url_or_token, ["sheet", "sheet_id"])).strip()
        if not sheet_id and "!" in (start_cell or ""):
            sheet_id = start_cell.split("!", 1)[0].strip()

        if not app_token:
            raise ValueError("base_url_or_token must be a Base URL or app_token")
        if not table_id:
            raise ValueError("base_table_id is required, or pass a Base URL containing ?table=tbl...")
        if not spreadsheet_token:
            raise ValueError("spreadsheet_url_or_token must be a Sheet URL or spreadsheet token")
        if not sheet_id:
            raise ValueError("sheet_id is required, or pass a Sheet URL containing ?sheet=xxxx")

        api = FeishuOpenAPI(openapi_domain, timeout_seconds)
        access_token = api.tenant_access_token(app_id, app_secret)

        fields = api.list_fields(access_token, app_token, table_id)
        requested_names = _split_names(field_names)
        field_by_name = {field.get("field_name"): field for field in fields if field.get("field_name")}
        missing_names = [name for name in requested_names if name not in field_by_name]
        if missing_names:
            raise ValueError(f"These Base field names were not found: {', '.join(missing_names)}")
        ordered_names = requested_names or [field.get("field_name", "") for field in fields if field.get("field_name")]
        if not ordered_names:
            raise ValueError("No fields found in the Base table")
        if len(ordered_names) > 100:
            raise ValueError("Feishu Sheets can write at most 100 columns per request. Use field_names to select 100 or fewer fields.")

        records = api.search_records(
            access_token,
            app_token,
            table_id,
            view_id,
            requested_names,
            page_size,
            int(max_records or 0),
        )

        rows: List[List[Any]] = []
        if include_header:
            rows.append(list(ordered_names))

        for record in records:
            record_fields = record.get("fields", {}) or {}
            rows.append(
                [
                    _to_sheet_cell(
                        record_fields.get(name),
                        field_by_name.get(name),
                        bool(flatten_complex_values),
                    )
                    for name in ordered_names
                ]
            )

        if not rows:
            status = {
                "ok": True,
                "version": NODE_VERSION,
                "message": "No rows to write",
                "records_read": len(records),
                "rows_written": 0,
                "columns_written": len(ordered_names),
            }
            return _json_dumps(status), 0, len(ordered_names)

        written_ranges = []
        write_results = []
        row_offset = 0
        for chunk in _chunks(rows, 5000):
            chunk_start_cell = start_cell if mode == "append" else _offset_cell(start_cell, row_offset)
            cell_range = _make_range(sheet_id, chunk_start_cell, len(chunk), len(ordered_names))
            write_results.append(api.write_values(access_token, spreadsheet_token, cell_range, chunk, mode))
            written_ranges.append(cell_range)
            row_offset += len(chunk)

        status = {
            "ok": True,
            "version": NODE_VERSION,
            "ranges": written_ranges,
            "mode": mode,
            "records_read": len(records),
            "rows_written": len(rows),
            "data_rows_written": len(records),
            "columns_written": len(ordered_names),
            "feishu_response": write_results[-1] if write_results else {},
        }
        return _json_dumps(status), len(rows), len(ordered_names)


class FeishuImageToSheetCell:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
                "app_id": ("STRING", {"default": ""}),
                "app_secret": ("STRING", {"default": ""}),
                "spreadsheet_url_or_token": ("STRING", {"default": ""}),
                "sheet_id": ("STRING", {"default": ""}),
                "cell": ("STRING", {"default": "A1"}),
                "image_name": ("STRING", {"default": "image.png"}),
                "timeout_seconds": ("INT", {"default": 30, "min": 5, "max": 300, "step": 1}),
            },
            "optional": {
                "openapi_domain": ("STRING", {"default": "https://open.feishu.cn"}),
            },
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("status_json",)
    FUNCTION = "write_image"
    CATEGORY = "Feishu"
    OUTPUT_NODE = True

    @classmethod
    def IS_CHANGED(cls, *args, **kwargs):
        return time.time()

    def write_image(
        self,
        image: Any,
        app_id: str,
        app_secret: str,
        spreadsheet_url_or_token: str,
        sheet_id: str,
        cell: str,
        image_name: str,
        timeout_seconds: int,
        openapi_domain: str = "https://open.feishu.cn",
    ) -> Tuple[str]:
        app_id = _read_secret(app_id, "FEISHU_APP_ID")
        app_secret = _read_secret(app_secret, "FEISHU_APP_SECRET")
        spreadsheet_url_or_token = _clean_text_input(spreadsheet_url_or_token)
        sheet_id = _clean_text_input(sheet_id)
        cell = _clean_text_input(cell) or "A1"
        image_name = _clean_text_input(image_name) or "image.png"
        openapi_domain = _clean_text_input(openapi_domain) or "https://open.feishu.cn"
        if not app_id or not app_secret:
            raise ValueError("app_id/app_secret are required, or set FEISHU_APP_ID and FEISHU_APP_SECRET")

        spreadsheet_token = _extract_token(spreadsheet_url_or_token, [r"/sheets/([^/?#]+)"])
        sheet_id = (sheet_id or _first_query_value(spreadsheet_url_or_token, ["sheet", "sheet_id"])).strip()
        if not sheet_id and "!" in cell:
            sheet_id = cell.split("!", 1)[0].strip()
        if not spreadsheet_token:
            raise ValueError("spreadsheet_url_or_token must be a Sheet URL or spreadsheet token")
        if not sheet_id:
            raise ValueError("sheet_id is required, or pass a Sheet URL containing ?sheet=xxxx")

        cell_range = _make_range(sheet_id, cell, 1, 1)
        image_bytes = _image_to_png_bytes(image)
        api = FeishuOpenAPI(openapi_domain, timeout_seconds)
        access_token = api.tenant_access_token(app_id, app_secret)
        write_result = api.write_image(access_token, spreadsheet_token, cell_range, image_bytes, image_name)
        status = {
            "ok": True,
            "version": NODE_VERSION,
            "range": cell_range,
            "image_name": image_name,
            "image_bytes": len(image_bytes),
            "feishu_response": write_result,
        }
        return (_json_dumps(status),)


NODE_CLASS_MAPPINGS = {
    "FeishuBaseToSheet": FeishuBaseToSheet,
    "FeishuBaseToSheetV020": FeishuBaseToSheet,
    "FeishuImageToSheetCell": FeishuImageToSheetCell,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "FeishuBaseToSheet": "Feishu Base To Sheet v0.3.0",
    "FeishuBaseToSheetV020": "Feishu Base To Sheet v0.3.0",
    "FeishuImageToSheetCell": "Feishu Image To Sheet Cell v0.3.0",
}
