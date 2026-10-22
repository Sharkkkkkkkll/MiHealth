# -*- coding: utf-8 -*-
"""把 docs/*.md 转成 Word (.docx)，保留标题/表格/列表/代码块格式。"""
import os
import re
from docx import Document
from docx.shared import Pt, RGBColor, Inches
from docx.enum.text import WD_ALIGN_PARAGRAPH


def add_markdown(doc: Document, md: str):
    lines = md.splitlines()
    i = 0
    in_code = False
    code_buf = []
    in_table = False
    table_rows = []

    def flush_code():
        nonlocal code_buf
        if code_buf:
            p = doc.add_paragraph()
            run = p.add_run("\n".join(code_buf))
            run.font.name = "Consolas"
            run.font.size = Pt(9)
            run.font.color.rgb = RGBColor(0x33, 0x33, 0x33)
            p.paragraph_format.left_indent = Inches(0.3)
            code_buf = []

    def flush_table():
        nonlocal table_rows
        if not table_rows:
            return
        # 表头
        header = table_rows[0]
        body = table_rows[1:]
        t = doc.add_table(rows=1 + len(body), cols=len(header))
        t.style = "Light Grid Accent 1"
        for j, cell in enumerate(header):
            t.rows[0].cells[j].text = cell
        for ri, row in enumerate(body, start=1):
            for j, cell in enumerate(row):
                t.rows[ri].cells[j].text = cell
        doc.add_paragraph()
        table_rows = []

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # 代码块
        if stripped.startswith("```"):
            if in_code:
                flush_code()
                in_code = False
            else:
                in_code = True
            i += 1
            continue
        if in_code:
            code_buf.append(line)
            i += 1
            continue

        # 表格
        if stripped.startswith("|") and i + 1 < len(lines) and re.match(r"^\s*\|[\s:|-]+\|\s*$", lines[i+1]):
            if table_rows:
                flush_table()
            header = [c.strip() for c in stripped.strip("|").split("|")]
            table_rows = [header]
            i += 2
            while i < len(lines) and lines[i].strip().startswith("|"):
                row = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                table_rows.append(row)
                i += 1
            flush_table()
            continue
        if table_rows and stripped.startswith("|"):
            table_rows.append([c.strip() for c in stripped.strip("|").split("|")])
            i += 1
            continue
        if table_rows and not stripped.startswith("|"):
            flush_table()

        # 标题
        m = re.match(r"^(#{1,6})\s+(.*)", stripped)
        if m:
            level = len(m.group(1))
            text = m.group(2).strip()
            h = doc.add_heading(level=min(level, 4))
            run = h.add_run(text)
            run.font.color.rgb = RGBColor(0x1F, 0x26, 0x30)
            i += 1
            continue

        # 图片 ![alt](path)
        m = re.match(r"^!\[(.*?)\]\((.*?)\)$", stripped)
        if m:
            img = m.group(2)
            if os.path.exists(img):
                p = doc.add_paragraph()
                run = p.add_run()
                run.add_picture(img, width=Inches(6.5))
            else:
                p = doc.add_paragraph()
                p.add_run(f"[图片缺失: {img}]")
            i += 1
            continue

        # 分隔线
        if stripped in ("---", "***", "___"):
            doc.add_paragraph()
            i += 1
            continue

        # 列表
        m = re.match(r"^\s*[-*]\s+(.*)", stripped)
        if m:
            p = doc.add_paragraph(style="List Bullet")
            add_inline(p, m.group(1))
            i += 1
            continue
        m = re.match(r"^\s*\d+\.\s+(.*)", stripped)
        if m:
            p = doc.add_paragraph(style="List Number")
            add_inline(p, m.group(1))
            i += 1
            continue

        # 普通段落
        if stripped:
            p = doc.add_paragraph()
            add_inline(p, stripped)
        i += 1

    if in_code:
        flush_code()
    if table_rows:
        flush_table()


def add_inline(p, text: str):
    """处理 **bold**、`code`、[text](url) 等行内格式。"""
    # 简单处理：先按 **bold** 分段，再处理 code
    parts = re.split(r"(\*\*.*?\*\*|`.*?`)", text)
    for part in parts:
        if not part:
            continue
        if part.startswith("**") and part.endswith("**"):
            run = p.add_run(part[2:-2])
            run.bold = True
        elif part.startswith("`") and part.endswith("`"):
            run = p.add_run(part[1:-1])
            run.font.name = "Consolas"
            run.font.size = Pt(10)
        else:
            p.add_run(part)


def convert(src: str, dst: str):
    doc = Document()
    # 设置默认字体
    style = doc.styles["Normal"]
    style.font.name = "Microsoft YaHei"
    style.font.size = Pt(11)
    with open(src, encoding="utf-8") as f:
        md = f.read()
    add_markdown(doc, md)
    doc.save(dst)
    print(f"OK: {dst}")


if __name__ == "__main__":
    docs_dir = os.path.dirname(os.path.abspath(__file__))
    files = [
        "01-需求分析.md",
        "02-系统设计.md",
        "03-编码实现.md",
        "04-测试设计.md",
        "06-使用文档.md",
        "07-产品亮点.md",
    ]
    for name in files:
        src = os.path.join(docs_dir, name)
        dst = os.path.join(docs_dir, name.replace(".md", ".docx"))
        if os.path.exists(src):
            convert(src, dst)
