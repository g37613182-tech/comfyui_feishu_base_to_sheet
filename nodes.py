import json
import io
import mimetypes
import os
import posixpath
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from xml.etree import ElementTree as ET


NODE_VERSION = "1.1.4"
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi", ".mpeg", ".mpg", ".3gp"}
MAX_DIRECT_DRIVE_UPLOAD_BYTES = 20 * 1024 * 1024


class AnyType(str):
    def __ne__(self, value: object) -> bool:
        return False


ANY_TYPE = AnyType("*")


class FeishuAPIError(RuntimeError):
    pass


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _json_loads_cell_value(raw: str) -> Any:
    raw = _clean_text_input(raw)
    if not raw:
        raise ValueError("cell_value_json is empty")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"cell_value_json must be valid JSON: {exc}") from exc


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


def _is_http_url(value: str) -> bool:
    value = _clean_text_input(value)
    return value.startswith("http://") or value.startswith("https://")


def _sheet_file_url(file_token: str, spreadsheet_url_or_token: str) -> str:
    file_token = _clean_text_input(file_token)
    if not file_token or "://" not in (spreadsheet_url_or_token or ""):
        return file_token
    parsed = urllib.parse.urlparse(spreadsheet_url_or_token)
    if not parsed.scheme or not parsed.netloc:
        return file_token
    return f"{parsed.scheme}://{parsed.netloc}/drive/file/{urllib.parse.quote(file_token, safe='')}"


def _drive_url_from_upload(upload_result: Dict[str, Any], file_token: str, spreadsheet_url_or_token: str) -> str:
    for key in ("url", "file_url", "link", "web_url"):
        value = upload_result.get(key)
        if isinstance(value, str) and _is_http_url(value):
            return value
    file_info = upload_result.get("file")
    if isinstance(file_info, dict):
        for key in ("url", "file_url", "link", "web_url"):
            value = file_info.get(key)
            if isinstance(value, str) and _is_http_url(value):
                return value
    return _sheet_file_url(file_token, spreadsheet_url_or_token)


def _safe_filename(name: str, default: str) -> str:
    name = _clean_text_input(name) or default
    name = os.path.basename(name.replace("\\", "/"))
    name = re.sub(r"[^\w.\- ()\u4e00-\u9fff]+", "_", name).strip(" ._")
    return name or default


def _extract_drive_folder_token(value: str) -> str:
    value = _clean_text_input(value)
    if not value:
        return ""
    if "://" not in value:
        return value

    parsed = urllib.parse.urlparse(value)
    query = urllib.parse.parse_qs(parsed.query)
    for key in ("folder_token", "folderToken", "parent_node", "parentNode"):
        item = query.get(key, [""])[0]
        if item:
            return item

    path = parsed.path or ""
    for pattern in (r"/drive/folder/([^/?#]+)", r"/folder/([^/?#]+)", r"/folders/([^/?#]+)"):
        match = re.search(pattern, path)
        if match:
            return match.group(1)
    return ""


def _comfy_managed_file_path(filename: str, subfolder: str = "", file_type: str = "output") -> str:
    filename = _clean_text_input(filename)
    if not filename:
        return ""
    try:
        import folder_paths
    except Exception:
        return ""

    file_type = _clean_text_input(file_type).lower()
    if file_type == "input":
        base_dir = folder_paths.get_input_directory()
    elif file_type == "temp":
        base_dir = folder_paths.get_temp_directory()
    else:
        base_dir = folder_paths.get_output_directory()

    candidate = os.path.abspath(os.path.join(base_dir, _clean_text_input(subfolder), filename))
    base_abs = os.path.abspath(base_dir)
    try:
        common = os.path.commonpath([base_abs, candidate])
    except ValueError:
        return ""
    if common != base_abs:
        return ""
    return candidate


def _extract_video_path_or_url(value: Any) -> str:
    seen: set = set()
    candidate_keys = (
        "video_path_or_url",
        "video_path",
        "file_path",
        "filepath",
        "full_path",
        "resource_path",
        "path",
        "url",
        "link",
    )

    def from_string(raw: str) -> str:
        raw = _clean_text_input(raw)
        if not raw:
            return ""
        if _is_http_url(raw) or os.path.isfile(raw):
            return raw
        if raw.strip().startswith(("{", "[")):
            try:
                return walk(json.loads(raw))
            except json.JSONDecodeError:
                return ""
        match = re.search(r"https?://[^\s\"'<>，。]+", raw)
        if match:
            return match.group(0)
        suffix = os.path.splitext(raw)[1].lower()
        if suffix in VIDEO_EXTENSIONS and (os.path.isabs(raw) or "/" in raw or "\\" in raw):
            return raw
        return ""

    def walk(item: Any) -> str:
        marker = id(item)
        if marker in seen:
            return ""
        seen.add(marker)

        if item is None:
            return ""
        if isinstance(item, str):
            return from_string(item)
        if isinstance(item, os.PathLike):
            return from_string(os.fspath(item))
        if isinstance(item, dict):
            for key in candidate_keys:
                result = from_string(str(item.get(key, ""))) if item.get(key) is not None else ""
                if result:
                    return result
            filename = item.get("filename") or item.get("file_name") or item.get("name")
            if filename:
                managed = _comfy_managed_file_path(
                    str(filename),
                    str(item.get("subfolder", "")),
                    str(item.get("type", "output")),
                )
                if managed:
                    return managed
            for nested in item.values():
                result = walk(nested)
                if result:
                    return result
            return ""
        if isinstance(item, (list, tuple)):
            for nested in item:
                result = walk(nested)
                if result:
                    return result
            return ""

        for key in candidate_keys:
            if hasattr(item, key):
                direct = getattr(item, key, "")
                result = walk(direct)
                if result:
                    return result
        data = {
            "filename": getattr(item, "filename", ""),
            "file_name": getattr(item, "file_name", ""),
            "name": getattr(item, "name", ""),
            "subfolder": getattr(item, "subfolder", ""),
            "type": getattr(item, "type", "output"),
        }
        managed = _comfy_managed_file_path(
            str(data.get("filename") or data.get("file_name") or data.get("name") or ""),
            str(data.get("subfolder") or ""),
            str(data.get("type") or "output"),
        )
        if managed:
            return managed
        return ""

    return walk(value)


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


def _column_to_letter(column: Any) -> str:
    column = _clean_text_input(str(column))
    if not column:
        raise ValueError("column is required")
    if column.isdigit():
        return _number_to_col(int(column))
    if re.fullmatch(r"[A-Za-z]+", column):
        return column.upper()
    raise ValueError("column must be a letter like C or a 1-based number like 3")


def _cell_from_row_column(row: int, column: Any) -> str:
    row = int(row)
    if row < 1:
        raise ValueError("row must be >= 1")
    return f"{_column_to_letter(column)}{row}"


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


def _image_bytes_to_comfy_image(image_bytes: bytes) -> Any:
    from PIL import Image
    import numpy as np
    import torch

    image = Image.open(io.BytesIO(image_bytes))
    if image.mode in ("RGBA", "LA") or "transparency" in image.info:
        rgba = image.convert("RGBA")
        background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        background.alpha_composite(rgba)
        image = background.convert("RGB")
    else:
        image = image.convert("RGB")
    array = np.asarray(image).astype(np.float32) / 255.0
    return torch.from_numpy(array)[None,]


def _blank_comfy_image() -> Any:
    import torch

    return torch.zeros((1, 1, 1, 3), dtype=torch.float32)


def _cell_value_to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    return _json_dumps(value)


def _extract_file_tokens(value: Any) -> List[str]:
    tokens: List[str] = []

    def add_token(raw: Any, allow_plain: bool = False) -> None:
        if not isinstance(raw, str):
            return
        for token in re.findall(r"box[a-zA-Z0-9_-]+", raw):
            if token not in tokens:
                tokens.append(token)
        for token in re.findall(
            r"(?i)[\"']?(?:fileToken|file_token|imageToken|float_image_token)[\"']?\s*[:=]\s*[\"']([A-Za-z0-9_-]{8,})[\"']",
            raw,
        ):
            if token not in tokens:
                tokens.append(token)
        plain = raw.strip()
        if allow_plain and re.fullmatch(r"[A-Za-z0-9_-]{8,}", plain) and plain not in tokens:
            tokens.append(plain)

    def walk(item: Any) -> None:
        if isinstance(item, dict):
            for key in ("file_token", "fileToken", "imageToken", "float_image_token", "token"):
                add_token(item.get(key), allow_plain=True)
            add_token(item.get("text"))
            for value in item.values():
                walk(value)
        elif isinstance(item, list):
            for value in item:
                walk(value)
        else:
            if isinstance(item, str) and item.strip().startswith(("{", "[")):
                try:
                    walk(json.loads(item))
                except json.JSONDecodeError:
                    pass
            add_token(item)

    walk(value)
    return tokens


def _extract_file_items(value: Any) -> List[Dict[str, str]]:
    items: List[Dict[str, str]] = []

    def add_item(token: str = "", name: str = "", url: str = "", kind: str = "") -> None:
        token = _clean_text_input(token)
        url = _clean_text_input(url)
        name = _clean_text_input(name)
        if not token and not url:
            return
        item = {"token": token, "name": name, "url": url, "kind": kind}
        marker = (token, url)
        if marker not in [(existing.get("token"), existing.get("url")) for existing in items]:
            items.append(item)

    def parse_json_string(raw: str) -> bool:
        stripped = raw.strip()
        if not stripped.startswith(("{", "[")):
            return False
        try:
            walk(json.loads(stripped))
            return True
        except json.JSONDecodeError:
            return False

    def walk(item: Any) -> None:
        if isinstance(item, dict):
            token = ""
            for key in ("fileToken", "file_token", "imageToken", "float_image_token", "token", "text"):
                value = item.get(key)
                if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{8,}", value.strip()):
                    token = value.strip()
                    break
            name = ""
            for key in ("name", "file_name", "filename", "title", "text"):
                value = item.get(key)
                if isinstance(value, str) and value and value != token:
                    name = value
                    break
            url = ""
            for key in ("url", "link", "tmp_url"):
                value = item.get(key)
                if isinstance(value, str) and _is_http_url(value):
                    url = value
                    break
            kind = str(item.get("type") or item.get("objType") or item.get("mime_type") or item.get("mimeType") or "")
            add_item(token=token, name=name, url=url, kind=kind)
            for value in item.values():
                walk(value)
        elif isinstance(item, list):
            for value in item:
                walk(value)
        elif isinstance(item, str):
            if parse_json_string(item):
                return
            for url in re.findall(r"https?://[^\s\"'<>，。]+", item):
                add_item(url=url, name=os.path.basename(urllib.parse.urlparse(url).path), kind="")
            for token in _extract_file_tokens(item):
                add_item(token=token, kind="")

    walk(value)
    return items


def _looks_like_video_item(item: Dict[str, str]) -> bool:
    haystack = " ".join([item.get("name", ""), item.get("url", ""), item.get("kind", "")]).lower()
    if "video" in haystack or "mp4" in haystack:
        return True
    parsed_path = urllib.parse.urlparse(item.get("url", "")).path
    suffixes = [os.path.splitext(item.get("name", ""))[1].lower(), os.path.splitext(parsed_path)[1].lower()]
    return any(suffix in VIDEO_EXTENSIONS for suffix in suffixes)


def _raw_cell_to_supported_value(value: Any, spreadsheet_url_or_token: str) -> Tuple[Any, str]:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value, "primitive"
    if isinstance(value, dict):
        value_type = str(value.get("type", "")).lower()
        if value_type in ("url", "formula", "mention", "multiplevalue"):
            return value, f"supported_{value_type}"

    for item in _extract_file_items(value):
        name = item.get("name", "") or "video"
        url = item.get("url", "")
        if not url and item.get("token"):
            url = _sheet_file_url(item["token"], spreadsheet_url_or_token)
        if _is_http_url(url):
            parsed_name = os.path.basename(urllib.parse.urlparse(url).path)
            return {
                "type": "url",
                "text": _safe_filename(name, parsed_name or "video"),
                "link": url,
            }, "file_as_url"

    return _cell_value_to_text(value), "json_text"


def _write_temp_video(video_bytes: bytes, file_name: str, file_token: str) -> str:
    file_name = _safe_filename(file_name, "video.mp4")
    base, ext = os.path.splitext(file_name)
    if not ext:
        ext = ".mp4"
    target_dir = os.path.join(tempfile.gettempdir(), "comfyui_feishu_sheet")
    os.makedirs(target_dir, exist_ok=True)
    token_part = re.sub(r"\W+", "", file_token or str(int(time.time())))[:12]
    path = os.path.join(target_dir, f"{base}_{token_part}{ext}")
    with open(path, "wb") as handle:
        handle.write(video_bytes)
    return path


def _looks_like_sheet_image_value(value: Any) -> bool:
    if isinstance(value, dict):
        keys = {str(key).lower() for key in value.keys()}
        if keys & {"filetoken", "file_token", "imagetoken", "float_image_token"}:
            return True
        return any(_looks_like_sheet_image_value(item) for item in value.values())
    if isinstance(value, list):
        return any(_looks_like_sheet_image_value(item) for item in value)
    if isinstance(value, str):
        if re.search(r"(?i)fileToken|file_token|imageToken|float_image_token", value):
            return True
        stripped = value.strip()
        if stripped.startswith(("{", "[")):
            try:
                return _looks_like_sheet_image_value(json.loads(stripped))
            except json.JSONDecodeError:
                return False
    return False


def _range_anchor_cell(cell_range: Any) -> str:
    if not isinstance(cell_range, str):
        return ""
    ref = cell_range.split("!", 1)[-1]
    return ref.split(":", 1)[0].replace("$", "").upper()


def _range_sheet_id(cell_range: Any) -> str:
    if not isinstance(cell_range, str) or "!" not in cell_range:
        return ""
    return cell_range.split("!", 1)[0]


def _zip_join(base_path: str, target: str) -> str:
    target = (target or "").replace("\\", "/")
    if target.startswith("/"):
        return target.lstrip("/")
    return posixpath.normpath(posixpath.join(posixpath.dirname(base_path), target))


def _relationship_targets(zf: zipfile.ZipFile, rels_path: str) -> Dict[str, str]:
    if rels_path not in zf.namelist():
        return {}
    root = ET.fromstring(zf.read(rels_path))
    targets: Dict[str, str] = {}
    for rel in root:
        if rel.tag.rsplit("}", 1)[-1] == "Relationship":
            rel_id = rel.attrib.get("Id")
            target = rel.attrib.get("Target")
            if rel_id and target:
                targets[rel_id] = target
    return targets


def _first_child_text(parent: ET.Element, local_name: str) -> Optional[str]:
    for child in parent:
        if child.tag.rsplit("}", 1)[-1] == local_name:
            return child.text
    return None


def _first_descendant(element: ET.Element, local_name: str) -> Optional[ET.Element]:
    for child in element.iter():
        if child.tag.rsplit("}", 1)[-1] == local_name:
            return child
    return None


def _xlsx_extract_cell_image(xlsx_bytes: bytes, sheet_index: int, cell: str) -> Tuple[Optional[bytes], Dict[str, Any]]:
    rel_id_attr = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
    embed_attr = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed"
    target_col, target_row = _parse_cell(cell)
    target_col -= 1
    target_row -= 1
    details: Dict[str, Any] = {
        "xlsx_sheet_index": sheet_index,
        "target_cell": f"{_number_to_col(target_col + 1)}{target_row + 1}",
        "candidate_cells": [],
    }

    with zipfile.ZipFile(io.BytesIO(xlsx_bytes)) as zf:
        workbook_path = "xl/workbook.xml"
        if workbook_path not in zf.namelist():
            details["error"] = "xl/workbook.xml not found in exported XLSX"
            return None, details

        workbook_root = ET.fromstring(zf.read(workbook_path))
        workbook_sheets = [
            item for item in workbook_root.iter() if item.tag.rsplit("}", 1)[-1] == "sheet"
        ]
        if not workbook_sheets:
            details["error"] = "No worksheets found in exported XLSX"
            return None, details

        sheet_index = max(0, min(int(sheet_index or 0), len(workbook_sheets) - 1))
        sheet_el = workbook_sheets[sheet_index]
        details["xlsx_sheet_name"] = sheet_el.attrib.get("name", "")
        workbook_rels = _relationship_targets(zf, "xl/_rels/workbook.xml.rels")
        worksheet_target = workbook_rels.get(sheet_el.attrib.get(rel_id_attr, ""))
        if not worksheet_target:
            details["error"] = "Worksheet relationship not found in exported XLSX"
            return None, details

        worksheet_path = _zip_join(workbook_path, worksheet_target)
        details["worksheet_path"] = worksheet_path
        if worksheet_path not in zf.namelist():
            details["error"] = f"Worksheet file not found: {worksheet_path}"
            return None, details

        worksheet_root = ET.fromstring(zf.read(worksheet_path))
        worksheet_rels_path = (
            f"{posixpath.dirname(worksheet_path)}/_rels/{posixpath.basename(worksheet_path)}.rels"
        )
        worksheet_rels = _relationship_targets(zf, worksheet_rels_path)
        drawing_paths: List[str] = []
        for drawing in worksheet_root.iter():
            if drawing.tag.rsplit("}", 1)[-1] != "drawing":
                continue
            drawing_target = worksheet_rels.get(drawing.attrib.get(rel_id_attr, ""))
            if drawing_target:
                drawing_paths.append(_zip_join(worksheet_path, drawing_target))
        details["drawing_paths"] = drawing_paths

        for drawing_path in drawing_paths:
            if drawing_path not in zf.namelist():
                continue
            drawing_root = ET.fromstring(zf.read(drawing_path))
            drawing_rels_path = (
                f"{posixpath.dirname(drawing_path)}/_rels/{posixpath.basename(drawing_path)}.rels"
            )
            drawing_rels = _relationship_targets(zf, drawing_rels_path)

            for anchor in drawing_root:
                local = anchor.tag.rsplit("}", 1)[-1]
                if local not in ("oneCellAnchor", "twoCellAnchor"):
                    continue
                from_el = None
                for child in anchor:
                    if child.tag.rsplit("}", 1)[-1] == "from":
                        from_el = child
                        break
                if from_el is None:
                    continue

                try:
                    anchor_col = int(_first_child_text(from_el, "col") or "-1")
                    anchor_row = int(_first_child_text(from_el, "row") or "-1")
                except ValueError:
                    continue

                blip = _first_descendant(anchor, "blip")
                image_rel_id = blip.attrib.get(embed_attr, "") if blip is not None else ""
                image_target = drawing_rels.get(image_rel_id, "")
                image_path = _zip_join(drawing_path, image_target) if image_target else ""
                candidate = {
                    "cell": f"{_number_to_col(anchor_col + 1)}{anchor_row + 1}",
                    "image_path": image_path,
                }
                if len(details["candidate_cells"]) < 20:
                    details["candidate_cells"].append(candidate)

                if anchor_col == target_col and anchor_row == target_row and image_path in zf.namelist():
                    details["matched_image_path"] = image_path
                    details["matched_anchor_type"] = local
                    return zf.read(image_path), details

    return None, details


def _sheet_index_from_metainfo(metainfo: Dict[str, Any], sheet_id: str) -> Tuple[int, Dict[str, Any]]:
    sheets = metainfo.get("sheets", [])
    for fallback_index, item in enumerate(sheets):
        current_id = item.get("sheetId") or item.get("sheet_id")
        if current_id == sheet_id:
            try:
                return int(item.get("index", fallback_index)), item
            except (TypeError, ValueError):
                return fallback_index, item
    return 0, {}


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

    def request_bytes(
        self,
        method: str,
        path: str,
        token: Optional[str] = None,
        query: Optional[Dict[str, Any]] = None,
    ) -> bytes:
        query = {key: value for key, value in (query or {}).items() if value not in (None, "")}
        url = f"{self.domain}{path}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"

        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        req = urllib.request.Request(url, headers=headers, method=method.upper())
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            raise FeishuAPIError(f"FeishuBaseToSheet v{NODE_VERSION}: {self._format_http_error(exc.code, raw)}") from exc
        except urllib.error.URLError as exc:
            raise FeishuAPIError(f"FeishuBaseToSheet v{NODE_VERSION}: Network error while calling Feishu OpenAPI: {exc.reason}") from exc

    def request_multipart(
        self,
        path: str,
        token: str,
        fields: Dict[str, Any],
        file_field: str,
        file_path: str,
        file_name: str,
    ) -> Dict[str, Any]:
        boundary = f"----ComfyUIFeishu{int(time.time() * 1000)}"
        content_type = mimetypes.guess_type(file_name)[0] or "application/octet-stream"
        body = io.BytesIO()

        def write_line(value: bytes) -> None:
            body.write(value + b"\r\n")

        for key, value in fields.items():
            write_line(f"--{boundary}".encode("utf-8"))
            write_line(f'Content-Disposition: form-data; name="{key}"'.encode("utf-8"))
            write_line(b"")
            write_line(str(value).encode("utf-8"))

        write_line(f"--{boundary}".encode("utf-8"))
        disposition = f'Content-Disposition: form-data; name="{file_field}"; filename="{file_name}"'
        write_line(disposition.encode("utf-8"))
        write_line(f"Content-Type: {content_type}".encode("utf-8"))
        write_line(b"")
        with open(file_path, "rb") as handle:
            body.write(handle.read())
        body.write(b"\r\n")
        write_line(f"--{boundary}--".encode("utf-8"))

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        }
        req = urllib.request.Request(f"{self.domain}{path}", data=body.getvalue(), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            raise FeishuAPIError(f"FeishuBaseToSheet v{NODE_VERSION}: {self._format_http_error(exc.code, raw)}") from exc
        except urllib.error.URLError as exc:
            raise FeishuAPIError(f"FeishuBaseToSheet v{NODE_VERSION}: Network error while calling Feishu OpenAPI: {exc.reason}") from exc

        payload = json.loads(raw) if raw else {}
        code = payload.get("code", 0)
        if code != 0:
            msg = payload.get("msg") or payload.get("message") or "Feishu OpenAPI returned an error"
            raise FeishuAPIError(f"FeishuBaseToSheet v{NODE_VERSION}: Feishu OpenAPI error code={code}: {msg}")
        return payload

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

    def read_values(
        self,
        token: str,
        spreadsheet_token: str,
        cell_range: str,
        date_time_render_option: str,
    ) -> Dict[str, Any]:
        spreadsheet_token = urllib.parse.quote(spreadsheet_token, safe="")
        encoded_range = urllib.parse.quote(cell_range, safe="")
        payload = self.request(
            "GET",
            f"/open-apis/sheets/v2/spreadsheets/{spreadsheet_token}/values/{encoded_range}",
            token=token,
            query={
                "dateTimeRenderOption": date_time_render_option,
                "user_id_type": "open_id",
            },
        )
        return payload.get("data", {})

    def query_float_images(self, token: str, spreadsheet_token: str, sheet_id: str) -> List[Dict[str, Any]]:
        spreadsheet_token = urllib.parse.quote(spreadsheet_token, safe="")
        sheet_id = urllib.parse.quote(sheet_id, safe="")
        payload = self.request(
            "GET",
            f"/open-apis/sheets/v3/spreadsheets/{spreadsheet_token}/sheets/{sheet_id}/float_images/query",
            token=token,
        )
        return payload.get("data", {}).get("items", [])

    def sheet_metainfo(self, token: str, spreadsheet_token: str) -> Dict[str, Any]:
        spreadsheet_token = urllib.parse.quote(spreadsheet_token, safe="")
        payload = self.request(
            "GET",
            f"/open-apis/sheets/v2/spreadsheets/{spreadsheet_token}/metainfo",
            token=token,
            query={"user_id_type": "open_id"},
        )
        return payload.get("data", {})

    def export_spreadsheet_xlsx(
        self,
        token: str,
        spreadsheet_token: str,
        poll_timeout_seconds: int,
    ) -> Tuple[bytes, Dict[str, Any]]:
        create_payload = self.request(
            "POST",
            "/open-apis/drive/v1/export_tasks",
            token=token,
            body={
                "file_extension": "xlsx",
                "token": spreadsheet_token,
                "type": "sheet",
            },
        )
        ticket = create_payload.get("data", {}).get("ticket")
        if not ticket:
            raise FeishuAPIError(f"FeishuBaseToSheet v{NODE_VERSION}: Export task did not return a ticket")

        deadline = time.time() + max(5, int(poll_timeout_seconds or 30))
        last_result: Dict[str, Any] = {}
        while time.time() <= deadline:
            query_payload = self.request(
                "GET",
                f"/open-apis/drive/v1/export_tasks/{urllib.parse.quote(str(ticket), safe='')}",
                token=token,
            )
            last_result = query_payload.get("data", {}).get("result", {})
            file_token = last_result.get("file_token")
            job_status = last_result.get("job_status")
            if str(job_status) == "0" and file_token:
                xlsx_bytes = self.request_bytes(
                    "GET",
                    f"/open-apis/drive/v1/export_tasks/file/{urllib.parse.quote(str(file_token), safe='')}/download",
                    token=token,
                )
                return xlsx_bytes, {"ticket": ticket, "file_token": file_token, "result": last_result}
            if str(job_status) in ("-1", "2", "3"):
                raise FeishuAPIError(
                    f"FeishuBaseToSheet v{NODE_VERSION}: XLSX export failed: {_json_dumps(last_result)}"
                )
            time.sleep(1)

        raise FeishuAPIError(
            f"FeishuBaseToSheet v{NODE_VERSION}: XLSX export timed out: {_json_dumps(last_result)}"
        )

    def download_media(self, token: str, file_token: str) -> bytes:
        return self.request_bytes(
            "GET",
            f"/open-apis/drive/v1/medias/{urllib.parse.quote(file_token, safe='')}/download",
            token=token,
        )

    def upload_drive_file(
        self,
        token: str,
        file_path: str,
        file_name: str,
        folder_token: str,
    ) -> Dict[str, Any]:
        size = os.path.getsize(file_path)
        if size > MAX_DIRECT_DRIVE_UPLOAD_BYTES:
            max_mb = MAX_DIRECT_DRIVE_UPLOAD_BYTES // (1024 * 1024)
            raise ValueError(
                f"Direct video upload is limited to {max_mb} MB for now. "
                "Use a video URL or upload the file to Feishu Drive first."
            )
        folder_token = _extract_drive_folder_token(folder_token)
        payload = self.request_multipart(
            "/open-apis/drive/v1/files/upload_all",
            token,
            {
                "file_name": file_name,
                "parent_type": "explorer",
                "parent_node": folder_token,
                "size": size,
            },
            "file",
            file_path,
            file_name,
        )
        return payload.get("data", {})

    def download_drive_file(self, token: str, file_token: str) -> bytes:
        try:
            return self.request_bytes(
                "GET",
                f"/open-apis/drive/v1/files/{urllib.parse.quote(file_token, safe='')}/download",
                token=token,
            )
        except FeishuAPIError:
            return self.download_media(token, file_token)

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


class FeishuValueToSheetCell:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "app_id": ("STRING", {"default": ""}),
                "app_secret": ("STRING", {"default": ""}),
                "spreadsheet_url_or_token": ("STRING", {"default": ""}),
                "sheet_id": ("STRING", {"default": ""}),
                "row": ("INT", {"default": 1, "min": 1, "max": 1000000, "step": 1}),
                "column": ("STRING", {"default": "A"}),
                "mode": (["auto", "text", "image", "video", "raw"], {"default": "auto"}),
                "image_name": ("STRING", {"default": "image.png"}),
                "video_name": ("STRING", {"default": "video.mp4"}),
                "timeout_seconds": ("INT", {"default": 30, "min": 5, "max": 300, "step": 1}),
            },
            "optional": {
                "cell_value_json": ("STRING", {"default": "", "multiline": True}),
                "text": ("STRING", {"default": "", "multiline": True}),
                "image": ("IMAGE",),
                "video": (ANY_TYPE,),
                "video_path_or_url": ("STRING", {"default": ""}),
                "drive_folder_token": ("STRING", {"default": ""}),
                "openapi_domain": ("STRING", {"default": "https://open.feishu.cn"}),
            },
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("status_json",)
    FUNCTION = "write_value"
    CATEGORY = "Feishu"
    OUTPUT_NODE = True

    @classmethod
    def IS_CHANGED(cls, *args, **kwargs):
        return time.time()

    def write_value(
        self,
        app_id: str,
        app_secret: str,
        spreadsheet_url_or_token: str,
        sheet_id: str,
        row: int,
        column: str,
        mode: str,
        image_name: str,
        video_name: str,
        timeout_seconds: int,
        cell_value_json: str = "",
        text: str = "",
        image: Any = None,
        video: Any = None,
        video_path_or_url: str = "",
        drive_folder_token: str = "",
        openapi_domain: str = "https://open.feishu.cn",
    ) -> Tuple[str]:
        app_id = _read_secret(app_id, "FEISHU_APP_ID")
        app_secret = _read_secret(app_secret, "FEISHU_APP_SECRET")
        spreadsheet_url_or_token = _clean_text_input(spreadsheet_url_or_token)
        sheet_id = _clean_text_input(sheet_id)
        cell = _cell_from_row_column(row, column)
        image_name = _clean_text_input(image_name) or "image.png"
        cell_value_json = _clean_text_input(cell_value_json)
        video_path_or_url = _clean_text_input(video_path_or_url)
        extracted_video_path_or_url = _extract_video_path_or_url(video)
        if not video_path_or_url and extracted_video_path_or_url:
            video_path_or_url = extracted_video_path_or_url
        drive_folder_token = _extract_drive_folder_token(drive_folder_token)
        text = "" if text is None else str(text)
        openapi_domain = _clean_text_input(openapi_domain) or "https://open.feishu.cn"
        if not app_id or not app_secret:
            raise ValueError("app_id/app_secret are required, or set FEISHU_APP_ID and FEISHU_APP_SECRET")

        spreadsheet_token = _extract_token(spreadsheet_url_or_token, [r"/sheets/([^/?#]+)"])
        sheet_id = (sheet_id or _first_query_value(spreadsheet_url_or_token, ["sheet", "sheet_id"])).strip()
        if not spreadsheet_token:
            raise ValueError("spreadsheet_url_or_token must be a Sheet URL or spreadsheet token")
        if not sheet_id:
            raise ValueError("sheet_id is required, or pass a Sheet URL containing ?sheet=xxxx")

        api = FeishuOpenAPI(openapi_domain, timeout_seconds)
        access_token = api.tenant_access_token(app_id, app_secret)
        cell_range = _make_range(sheet_id, cell, 1, 1)

        if mode == "raw" or (mode == "auto" and cell_value_json):
            raw_value = _json_loads_cell_value(cell_value_json)
            raw_error = ""
            raw_fallback = False
            raw_fallback_reason = ""
            try:
                write_result = api.write_values(access_token, spreadsheet_token, cell_range, [[raw_value]], "overwrite")
                write_value = raw_value
            except FeishuAPIError as exc:
                raw_error = str(exc)
                write_value, raw_fallback_reason = _raw_cell_to_supported_value(raw_value, spreadsheet_url_or_token)
                if write_value == raw_value:
                    raise
                raw_fallback = True
                write_result = api.write_values(access_token, spreadsheet_token, cell_range, [[write_value]], "overwrite")
            status = {
                "ok": True,
                "version": NODE_VERSION,
                "write_type": "raw_cell_fallback" if raw_fallback else "raw_cell",
                "range": cell_range,
                "row": int(row),
                "column": _column_to_letter(column),
                "raw_cell": raw_value,
                "written_cell": write_value,
                "raw_fallback": raw_fallback,
                "raw_fallback_reason": raw_fallback_reason,
                "raw_error": raw_error,
                "feishu_response": write_result,
            }
            return (_json_dumps(status),)

        if mode == "video" or (mode == "auto" and (video_path_or_url or video is not None)):
            if not video_path_or_url:
                raise ValueError("mode=video requires video_path_or_url or a video input containing a path/URL")
            inferred_name = os.path.basename(urllib.parse.urlparse(video_path_or_url).path) or "video.mp4"
            requested_video_name = _clean_text_input(video_name)
            if not requested_video_name or requested_video_name == "video.mp4":
                requested_video_name = inferred_name
            safe_video_name = _safe_filename(requested_video_name, "video.mp4")
            if _is_http_url(video_path_or_url):
                cell_value = {
                    "type": "url",
                    "text": safe_video_name,
                    "link": video_path_or_url,
                }
                write_result = api.write_values(access_token, spreadsheet_token, cell_range, [[cell_value]], "overwrite")
                status = {
                    "ok": True,
                    "version": NODE_VERSION,
                    "write_type": "video_url",
                    "range": cell_range,
                    "row": int(row),
                    "column": _column_to_letter(column),
                    "video_name": safe_video_name,
                    "video_path_or_url": video_path_or_url,
                    "video_input_detected": bool(video is not None),
                    "feishu_response": write_result,
                }
                return (_json_dumps(status),)

            if not os.path.isfile(video_path_or_url):
                raise ValueError(f"video_path_or_url is not a local file: {video_path_or_url}")
            if requested_video_name == "video.mp4":
                safe_video_name = _safe_filename(os.path.basename(video_path_or_url), "video.mp4")
            upload_result = api.upload_drive_file(
                access_token,
                video_path_or_url,
                safe_video_name,
                drive_folder_token,
            )
            file_token = (
                upload_result.get("file_token")
                or upload_result.get("fileToken")
                or upload_result.get("token")
                or upload_result.get("file", {}).get("token")
                or upload_result.get("file", {}).get("file_token")
            )
            if not file_token:
                raise FeishuAPIError(
                    f"FeishuBaseToSheet v{NODE_VERSION}: Drive upload did not return file_token: "
                    f"{_json_dumps(upload_result)}"
                )
            drive_url = _drive_url_from_upload(upload_result, file_token, spreadsheet_url_or_token)
            cell_value = {
                "type": "url",
                "text": safe_video_name,
                "link": drive_url,
            }
            write_result = api.write_values(access_token, spreadsheet_token, cell_range, [[cell_value]], "overwrite")
            status = {
                "ok": True,
                "version": NODE_VERSION,
                "write_type": "video_file_url",
                "range": cell_range,
                "row": int(row),
                "column": _column_to_letter(column),
                "video_name": safe_video_name,
                "video_path_or_url": video_path_or_url,
                "video_input_detected": bool(video is not None),
                "file_token": file_token,
                "drive_url": drive_url,
                "drive_parent_node": drive_folder_token,
                "drive_parent_note": "empty means Feishu Drive root",
                "upload": upload_result,
                "feishu_response": write_result,
            }
            return (_json_dumps(status),)

        if mode == "image" or (mode == "auto" and image is not None):
            if image is None:
                raise ValueError("mode=image requires an IMAGE input")
            image_bytes = _image_to_png_bytes(image)
            write_result = api.write_image(access_token, spreadsheet_token, cell_range, image_bytes, image_name)
            status = {
                "ok": True,
                "version": NODE_VERSION,
                "write_type": "image",
                "range": cell_range,
                "row": int(row),
                "column": _column_to_letter(column),
                "image_name": image_name,
                "image_bytes": len(image_bytes),
                "feishu_response": write_result,
            }
            return (_json_dumps(status),)

        write_result = api.write_values(access_token, spreadsheet_token, cell_range, [[text]], "overwrite")
        status = {
            "ok": True,
            "version": NODE_VERSION,
            "write_type": "text",
            "range": cell_range,
            "row": int(row),
            "column": _column_to_letter(column),
            "text_length": len(text),
            "feishu_response": write_result,
        }
        return (_json_dumps(status),)


class FeishuSheetCellReader:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "app_id": ("STRING", {"default": ""}),
                "app_secret": ("STRING", {"default": ""}),
                "spreadsheet_url_or_token": ("STRING", {"default": ""}),
                "sheet_id": ("STRING", {"default": ""}),
                "row": ("INT", {"default": 1, "min": 1, "max": 1000000, "step": 1}),
                "column": ("STRING", {"default": "A"}),
                "read_mode": (["auto", "text", "image", "video"], {"default": "auto"}),
                "date_time_render_option": (["FormattedString", "SerialNumber"], {"default": "FormattedString"}),
                "timeout_seconds": ("INT", {"default": 60, "min": 5, "max": 300, "step": 1}),
            },
            "optional": {
                "openapi_domain": ("STRING", {"default": "https://open.feishu.cn"}),
            },
        }

    RETURN_TYPES = ("STRING", "IMAGE", "STRING", "BOOLEAN", "STRING", "BOOLEAN", "STRING")
    RETURN_NAMES = ("text", "image", "status_json", "has_image", "video_path_or_url", "has_video", "cell_value_json")
    FUNCTION = "read_cell"
    CATEGORY = "Feishu"
    OUTPUT_NODE = True

    @classmethod
    def IS_CHANGED(cls, *args, **kwargs):
        return time.time()

    def read_cell(
        self,
        app_id: str,
        app_secret: str,
        spreadsheet_url_or_token: str,
        sheet_id: str,
        row: int,
        column: str,
        read_mode: str,
        date_time_render_option: str,
        timeout_seconds: int,
        openapi_domain: str = "https://open.feishu.cn",
    ) -> Tuple[str, Any, str, bool, str, bool, str]:
        app_id = _read_secret(app_id, "FEISHU_APP_ID")
        app_secret = _read_secret(app_secret, "FEISHU_APP_SECRET")
        spreadsheet_url_or_token = _clean_text_input(spreadsheet_url_or_token)
        sheet_id = _clean_text_input(sheet_id)
        openapi_domain = _clean_text_input(openapi_domain) or "https://open.feishu.cn"
        if not app_id or not app_secret:
            raise ValueError("app_id/app_secret are required, or set FEISHU_APP_ID and FEISHU_APP_SECRET")

        spreadsheet_token = _extract_token(spreadsheet_url_or_token, [r"/sheets/([^/?#]+)"])
        sheet_id = (sheet_id or _first_query_value(spreadsheet_url_or_token, ["sheet", "sheet_id"])).strip()
        if not spreadsheet_token:
            raise ValueError("spreadsheet_url_or_token must be a Sheet URL or spreadsheet token")
        if not sheet_id:
            raise ValueError("sheet_id is required, or pass a Sheet URL containing ?sheet=xxxx")

        cell = _cell_from_row_column(row, column)
        cell_range = _make_range(sheet_id, cell, 1, 1)
        api = FeishuOpenAPI(openapi_domain, timeout_seconds)
        access_token = api.tenant_access_token(app_id, app_secret)

        data = api.read_values(access_token, spreadsheet_token, cell_range, date_time_render_option)
        values = data.get("valueRange", {}).get("values", [[]])
        value = values[0][0] if values and values[0] else ""
        text = _cell_value_to_text(value)
        cell_value_json = _json_dumps(value)

        file_items = _extract_file_items(value) if read_mode in ("auto", "video") else []
        video_items = [item for item in file_items if _looks_like_video_item(item)]
        if read_mode == "video" and not video_items:
            video_items = file_items
        if read_mode in ("auto", "video") and video_items:
            item = video_items[0]
            video_output = item.get("url", "")
            video_source = "url" if video_output else ""
            video_lookup_error = ""
            if not video_output and item.get("token"):
                try:
                    video_bytes = api.download_drive_file(access_token, item["token"])
                    video_output = _write_temp_video(video_bytes, item.get("name", "") or "video.mp4", item["token"])
                    video_source = "drive_download"
                except Exception as exc:
                    video_lookup_error = str(exc)
                    video_output = _sheet_file_url(item["token"], spreadsheet_url_or_token)
                    video_source = "file_token"
            status = {
                "ok": bool(video_output),
                "version": NODE_VERSION,
                "read_type": "video",
                "video_source": video_source,
                "range": cell_range,
                "row": int(row),
                "column": _column_to_letter(column),
                "video_path_or_url": video_output,
                "file_item": item,
                "video_lookup_error": video_lookup_error,
                "raw_cell": value,
            }
            return text, _blank_comfy_image(), _json_dumps(status), False, video_output, bool(video_output), cell_value_json

        if read_mode == "video":
            status = {
                "ok": False,
                "version": NODE_VERSION,
                "read_type": "video",
                "range": cell_range,
                "row": int(row),
                "column": _column_to_letter(column),
                "raw_cell": value,
                "file_items": file_items,
            }
            return text, _blank_comfy_image(), _json_dumps(status), False, "", False, cell_value_json

        image_like_value = _looks_like_sheet_image_value(value)
        file_tokens = _extract_file_tokens(value) if read_mode in ("auto", "image") else []
        image_lookup_error = ""
        image_source = "cell_value_token"
        if read_mode in ("auto", "image") and not file_tokens:
            try:
                float_images = api.query_float_images(access_token, spreadsheet_token, sheet_id)
            except FeishuAPIError as exc:
                image_lookup_error = str(exc)
            else:
                for item in float_images:
                    item_range = item.get("range")
                    if _range_sheet_id(item_range) in ("", sheet_id) and _range_anchor_cell(item_range) == cell.upper():
                        token = item.get("float_image_token")
                        if token:
                            file_tokens = [token]
                            image_source = "float_image"
                            break

        if file_tokens:
            try:
                image_bytes = api.download_media(access_token, file_tokens[0])
                image = _image_bytes_to_comfy_image(image_bytes)
            except Exception as exc:
                image_lookup_error = str(exc)
            else:
                status = {
                    "ok": True,
                    "version": NODE_VERSION,
                    "read_type": "image",
                    "image_source": image_source,
                    "range": cell_range,
                    "row": int(row),
                    "column": _column_to_letter(column),
                    "file_token": file_tokens[0],
                    "image_bytes": len(image_bytes),
                    "raw_cell": value,
                }
                return text, image, _json_dumps(status), True, "", False, cell_value_json

        should_try_xlsx = read_mode == "image" or (read_mode == "auto" and image_like_value)
        if should_try_xlsx:
            export_status: Dict[str, Any] = {}
            xlsx_details: Dict[str, Any] = {}
            sheet_info: Dict[str, Any] = {}
            try:
                metainfo = api.sheet_metainfo(access_token, spreadsheet_token)
                sheet_index, sheet_info = _sheet_index_from_metainfo(metainfo, sheet_id)
                xlsx_bytes, export_status = api.export_spreadsheet_xlsx(
                    access_token,
                    spreadsheet_token,
                    timeout_seconds,
                )
                image_bytes, xlsx_details = _xlsx_extract_cell_image(xlsx_bytes, sheet_index, cell)
            except FeishuAPIError as exc:
                image_lookup_error = f"{image_lookup_error}; {exc}" if image_lookup_error else str(exc)
                image_bytes = None
            if image_bytes:
                image = _image_bytes_to_comfy_image(image_bytes)
                status = {
                    "ok": True,
                    "version": NODE_VERSION,
                    "read_type": "image",
                    "image_source": "xlsx_export",
                    "range": cell_range,
                    "row": int(row),
                    "column": _column_to_letter(column),
                    "raw_cell": value,
                    "image_lookup_error": image_lookup_error,
                    "export": export_status,
                    "xlsx": xlsx_details,
                    "sheet": sheet_info,
                }
                return text, image, _json_dumps(status), True, "", False, cell_value_json
            status = {
                "ok": False,
                "version": NODE_VERSION,
                "read_type": "image",
                "range": cell_range,
                "row": int(row),
                "column": _column_to_letter(column),
                "raw_cell": value,
                "image_like_value": image_like_value,
                "image_lookup_error": image_lookup_error,
                "export": export_status,
                "xlsx": xlsx_details,
                "sheet": sheet_info,
            }
            return text, _blank_comfy_image(), _json_dumps(status), False, "", False, cell_value_json

        status = {
            "ok": True,
            "version": NODE_VERSION,
            "read_type": "text",
            "range": cell_range,
            "row": int(row),
            "column": _column_to_letter(column),
            "image_like_value": image_like_value,
            "image_lookup_error": image_lookup_error,
            "raw_cell": value,
        }
        return text, _blank_comfy_image(), _json_dumps(status), False, "", False, cell_value_json


class FeishuSheetWriter(FeishuValueToSheetCell):
    pass


class FeishuSheetReader(FeishuSheetCellReader):
    pass


NODE_CLASS_MAPPINGS = {
    "FeishuSheetReader": FeishuSheetReader,
    "FeishuSheetWriter": FeishuSheetWriter,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "FeishuSheetReader": "Feishu Sheet Reader v1.1.4",
    "FeishuSheetWriter": "Feishu Sheet Writer v1.1.4",
}
