"""CSV 导入校验。"""

import pytest

from app.csvutil import CsvImportError, parse_catalog


def test_basic_parse_and_aliases():
    raw = "来源,原编号,名称\nA,001,梅瓶\nB,002,瓷盘\n".encode("utf-8-sig")
    rows = parse_catalog(raw)
    assert rows == [("A", "001", "梅瓶"), ("B", "002", "瓷盘")]

    raw_en = b"source,original_no,name\nA,001,Mei Ping"
    assert parse_catalog(raw_en) == [("A", "001", "Mei Ping")]


def test_duplicate_combo_in_file_rejected():
    raw = "来源,原编号,名称\nA,001,梅瓶\nA,001,另一名称\n".encode()
    with pytest.raises(CsvImportError) as exc:
        parse_catalog(raw)
    assert "重复" in str(exc.value)


def test_missing_column_rejected():
    with pytest.raises(CsvImportError) as exc:
        parse_catalog("来源,名称\nA,梅瓶\n".encode())
    assert "原编号" in str(exc.value)


def test_empty_field_and_blank_lines():
    with pytest.raises(CsvImportError) as exc:
        parse_catalog("来源,原编号,名称\nA,,梅瓶\n".encode())
    assert "空字段" in str(exc.value)
    # 空行被跳过，不报错
    rows = parse_catalog("来源,原编号,名称\nA,001,梅瓶\n\n\n".encode())
    assert len(rows) == 1


def test_bad_encoding_rejected():
    with pytest.raises(CsvImportError):
        parse_catalog("来源,原编号,名称\n".encode("utf-16"))


def test_quoted_name_with_comma():
    rows = parse_catalog('来源,原编号,名称\nA,001,"梅瓶,青花"\n'.encode())
    assert rows[0][2] == "梅瓶,青花"
