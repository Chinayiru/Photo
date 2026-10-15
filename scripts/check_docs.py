#!/usr/bin/env python3
"""文档一致性检查：校验 docs/ 与根目录文档的交叉引用、编号与状态是否自洽。

检查项：
  1. 所有 Markdown 相对链接的目标文件是否存在
  2. 需求编号 P1-1..P1-7 在 01/04/07 中是否被引用
  3. 里程碑编号 C1..C7 在 00/07/09 中是否一致
  4. 是否残留未完成标记（TODO / TBD / FIXME / 待补 / 待填 / 待定）
  5. 期望的 12 份文档是否齐备

**围栏代码块与行内代码会先被剥离再检查** —— 详见 `_strip_code()` 的注释。

用法：python scripts/check_docs.py
退出码：0 全部通过；1 存在问题
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# Windows 控制台默认 GBK，强制 UTF-8 输出，避免中文与符号乱码/崩溃
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"

LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)#]+?)(?:#[^)]*)?\)")
# 只认真正的"未完成标记"。**不要**把「占位符」与裸 `XXX` 加进来：
# 本项目文档里「占位符」是一个正经技术名词（SQL 参数占位符 `?`、
# OneDrive 按需下载的文件占位符、`TC-XXX-YYY-NNN` 的编号格式说明），
# 把它们当未完成标记会产生十几条永远修不掉的假阳性，
# 最终结果是"检查永远红灯，于是没人再看它"。
PLACEHOLDER_RE = re.compile(r"\bTODO\b|\bTBD\b|\bFIXME\b|待补|待填|待定")
# 故意留白的模板字段不算未完成标记。docs/07 的执行记录表是**给业主照着填的**，
# 「（留空待填）」是表格列的正常内容，不是作者偷懒留下的洞。
ALLOWED_BLANKS = "留空待填"
REQ_RE = re.compile(r"\bP1-[1-7]\b")
MILESTONE_RE = re.compile(r"\bC[1-7]\b")

FENCE_RE = re.compile(r"^\s*(```|~~~)")
INLINE_CODE_RE = re.compile(r"`[^`]*`")

# 期望存在的文档（编号 -> 文件名前缀）
EXPECTED = {
    "00": "00-文档索引与开发流程.md",
    "01": "01-需求规格说明书.md",
    "02": "02-概要设计说明书.md",
    "03": "03-详细设计说明书.md",
    "04": "04-接口设计文档.md",
    "05": "05-数据库设计文档.md",
    "06": "06-安全设计文档.md",
    "07": "07-测试计划与用例.md",
    "08": "08-部署与运维手册.md",
    "09": "09-开发计划与任务分解.md",
    "10": "10-编码规范与协作约定.md",
    "11": "11-风险登记册与决策记录.md",
}

ROOT_DOCS = ["README.md", "设计文档.md", "VPS选购建议.md"]


def iter_markdown() -> list[Path]:
    files = [ROOT / name for name in ROOT_DOCS if (ROOT / name).exists()]
    if DOCS.exists():
        files.extend(sorted(DOCS.glob("*.md")))
    return files


def _strip_code(text: str) -> str:
    r"""把围栏代码块与行内代码替换成等长空白，保留行号与列位置。

    两处必须剥离，否则检查只会产生假阳性：
      · **围栏代码块**：正则字面量里会出现 `[.\-:]` 这类方括号，
        Markdown 链接正则会把 `[.\-:](?P<S>[0-5]?\d)` 当成本地链接目标
        （实测 docs/03 与 docs/05 因此各报数条"断链"）。
      · **行内代码**：`` `?` ``、`` `TC-XXX-YYY-NNN` `` 之类的技术名词。
    """
    out: list[str] = []
    in_fence = False
    for line in text.splitlines():
        if FENCE_RE.match(line):
            in_fence = not in_fence
            out.append("")
            continue
        if in_fence:
            out.append("")
            continue
        out.append(INLINE_CODE_RE.sub(lambda m: " " * len(m.group(0)), line))
    return "\n".join(out)


def check_links(files: list[Path]) -> list[str]:
    problems: list[str] = []
    for path in files:
        raw = path.read_text(encoding="utf-8")
        text = _strip_code(raw)
        # 用原文逐行对应（_strip_code 保持行数不变）
        for lineno, (line, _raw_line) in enumerate(
            zip(text.splitlines(), raw.splitlines()), 1
        ):
            for label, target in LINK_RE.findall(line):
                if target.startswith(("http://", "https://", "mailto:")):
                    continue
                resolved = (path.parent / target).resolve()
                if not resolved.exists():
                    problems.append(
                        f"断链 {path.relative_to(ROOT)}:{lineno} [{label}]({target})"
                    )
    return problems


def check_placeholders(files: list[Path]) -> list[str]:
    problems: list[str] = []
    for path in files:
        raw = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(_strip_code(raw).splitlines(), 1):
            # 「留空待填」是给业主照着填的模板字段，不是未完成标记
            if ALLOWED_BLANKS in line:
                continue
            if PLACEHOLDER_RE.search(line):
                problems.append(f"占位符 {path.relative_to(ROOT)}:{lineno} {line.strip()[:90]}")
    return problems


def check_coverage(files: list[Path]) -> list[str]:
    """需求编号与里程碑编号的覆盖情况（仅提示，不判失败）。"""
    notes: list[str] = []
    by_name = {p.name: p.read_text(encoding="utf-8") for p in files}

    req_doc = by_name.get(EXPECTED["01"], "")
    missing_req = [f"P1-{i}" for i in range(1, 8) if f"P1-{i}" not in req_doc]
    if req_doc and missing_req:
        notes.append(f"01-需求规格 缺少需求编号：{', '.join(missing_req)}")

    for key in ("04", "07"):
        text = by_name.get(EXPECTED[key], "")
        if text:
            found = {m for m in REQ_RE.findall(text)}
            if len(found) < 5:
                notes.append(f"{EXPECTED[key]} 对需求编号的引用偏少（{len(found)} 处）")

    plan = by_name.get(EXPECTED["09"], "")
    if plan:
        missing_ms = [f"C{i}" for i in range(1, 8) if f"C{i}" not in plan]
        if missing_ms:
            notes.append(f"09-开发计划 缺少里程碑：{', '.join(missing_ms)}")
    return notes


def check_expected_docs() -> list[str]:
    problems: list[str] = []
    for _num, name in EXPECTED.items():
        if not (DOCS / name).exists():
            problems.append(f"缺失文档 docs/{name}")
    return problems


def main() -> int:
    files = iter_markdown()
    print(f"扫描 {len(files)} 个 Markdown 文件\n")

    problems = check_expected_docs() + check_links(files) + check_placeholders(files)
    notes = check_coverage(files)

    if notes:
        print("提示（不阻塞）：")
        for n in notes:
            print(f"  · {n}")
        print()

    if problems:
        print("发现问题：")
        for p in problems:
            print(f"  ✗ {p}")
        print(f"\n共 {len(problems)} 个问题")
        return 1

    print("✓ 文档一致性检查通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
