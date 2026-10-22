# -*- coding: utf-8 -*-
"""用 WPS COM 美化 Word 文档格式。
设置中文字体、标题样式、表格样式、段落行距、页边距。"""
import os
import time
import win32com.client


def beautify(path: str):
    wps = win32com.client.Dispatch("KWPS.Application")
    wps.Visible = False
    try:
        doc = wps.Documents.Open(path)
        # 页边距
        ps = doc.PageSetup
        ps.TopMargin = wps.CentimetersToPoints(2.5)
        ps.BottomMargin = wps.CentimetersToPoints(2.5)
        ps.LeftMargin = wps.CentimetersToPoints(3.0)
        ps.RightMargin = wps.CentimetersToPoints(3.0)

        # 遍历所有段落，设置字体和行距
        for para in doc.Paragraphs:
            try:
                rng = para.Range
                # 标题（样式名包含 Heading）
                style_name = para.Style.NameLocal
                if "标题" in style_name or "Heading" in style_name:
                    rng.Font.Name = "微软雅黑"
                    rng.Font.NameFarEast = "微软雅黑"
                    rng.Font.Bold = True
                    rng.Font.Size = 16 if "1" in style_name else 14
                    rng.Font.Color = 0x2E74B5
                else:
                    rng.Font.Name = "微软雅黑"
                    rng.Font.NameFarEast = "微软雅黑"
                    rng.Font.Size = 11
                    rng.Font.Color = 0x333333
                para.LineSpacingRule = 1  # 单倍
                para.SpaceAfter = 6
            except Exception:
                pass

        # 遍历所有表格，设置表头底色和边框
        for table in doc.Tables:
            try:
                table.Range.Font.Name = "微软雅黑"
                table.Range.Font.NameFarEast = "微软雅黑"
                table.Range.Font.Size = 10
                # 表头行加粗 + 底色
                header = table.Rows(1)
                header.Range.Font.Bold = True
                header.Range.Shading.BackgroundPatternColor = 0xDEEAF6
                # 边框
                table.Borders.Enable = True
            except Exception:
                pass

        doc.Save()
        doc.Close(False)
        print(f"OK: {os.path.basename(path)}")
    finally:
        wps.Quit()


if __name__ == "__main__":
    docs_dir = os.path.dirname(os.path.abspath(__file__))
    files = [
        "01-需求分析.docx",
        "02-系统设计.docx",
        "03-编码实现.docx",
        "04-测试设计.docx",
        "06-使用文档.docx",
        "07-产品亮点.docx",
    ]
    for name in files:
        path = os.path.join(docs_dir, name)
        if os.path.exists(path):
            try:
                beautify(path)
            except Exception as e:
                print(f"FAIL {name}: {e}")
            time.sleep(1)
