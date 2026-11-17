"""CSV 目录导入解析。

要求表头包含：来源、原编号、名称（兼容英文列名 source/original_no/name）。
- 来源 + 原编号 组合必须唯一：文件内重复直接报错，不允许进入数据库
- 单元格去除首尾空白，名称允许包含逗号（标准 CSV 引号转义）
- 自动处理 UTF-8 BOM；编码错误给出明确提示
"""

import csv
import io

REQUIRED = ("source", "original_no", "name")
COLUMN_LABELS = {"source": "来源", "original_no": "原编号", "name": "名称"}
HEADER_ALIASES = {
    "来源": "source",
    "source": "source",
    "原编号": "original_no",
    "original_no": "original_no",
    "originalno": "original_no",
    "编号": "original_no",
    "名称": "name",
    "name": "name",
    "文物名称": "name",
    "品名": "name",
}


class CsvImportError(Exception):
    pass


def _decode(raw):
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        raise CsvImportError("仅支持 UTF-8 编码的 CSV（检测到 UTF-16），请另存为 UTF-8 后重试")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CsvImportError("文件不是有效的 UTF-8 编码，请用 UTF-8 保存后重新上传") from exc


def parse_catalog(raw_bytes):
    """解析上传内容，返回 [(source, original_no, name), ...]（保留文件顺序）。"""
    text = _decode(raw_bytes)
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        raise CsvImportError("CSV 文件为空")

    columns = {}
    for idx, cell in enumerate(header):
        key = HEADER_ALIASES.get(cell.strip().lower())
        if key and key not in columns:
            columns[key] = idx
    missing = [COLUMN_LABELS[c] for c in REQUIRED if c not in columns]
    if missing:
        raise CsvImportError(
            "表头缺少必需列：%s（实际表头：%s）"
            % ("、".join(missing), "、".join(h.strip() for h in header) or "（空）")
        )

    rows = []
    seen = set()
    for line_no, row in enumerate(reader, start=2):
        if not row or all(not c.strip() for c in row):
            continue  # 跳过空行
        try:
            source = row[columns["source"]].strip()
            original_no = row[columns["original_no"]].strip()
            name = row[columns["name"]].strip()
        except IndexError:
            raise CsvImportError("第 %d 行列数不足，请检查该行" % line_no)
        if not source or not original_no or not name:
            raise CsvImportError("第 %d 行存在空字段（来源、原编号、名称均必填）" % line_no)
        key = (source, original_no)
        if key in seen:
            raise CsvImportError(
                "第 %d 行违反唯一性：来源 %r + 原编号 %r 在文件内重复"
                % (line_no, source, original_no)
            )
        seen.add(key)
        rows.append((source, original_no, name))

    if not rows:
        raise CsvImportError("CSV 没有可导入的数据行")
    return rows
