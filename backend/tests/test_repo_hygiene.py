"""仓库整洁：换行符不许被工具悄悄改写。

## 为什么需要这个文件

仓库里**混着两种换行符**，这是既成事实：

- 18 个 `.py` 是 CRLF（一开始就这么提交的，`git show HEAD:<f>` 里也是 CRLF）；
- 其余文件是 LF。

混着本身不致命。真正的问题是**混着的时候没人发现得了**。实测踩到过：

用 PowerShell 的 `Set-Content` 改 `docs/07-测试计划与用例.md` 的 3 个表格行，
它把整个文件 3425 行从 LF 换成了 CRLF。于是 `git diff` 是
**+3425 / -3425**，而真正的语义改动只有 18 行；`git diff --ignore-all-space`
直接变**空** —— 证明这 3425 行里一个字的语义都没有，全是噪音。
翻提交记录的人会以为那份 200KB 的文档被重写了。

代价不只是难读：`git blame` 对整份文档失效，`git log -S` 找不到真实改动，
review 时不可能从 6840 行 diff 里挑出那 18 行。而**差点就这么推上去了** ——
当时我已经 `git add -A` 并提交了，是事后看 `--stat` 才发现数字不对。

## 这个文件做什么

把当前的 CRLF 文件集合当作**基线**钉住：基线之外的任何已跟踪文本文件
出现 CRLF，就是被某个工具改写了，直接失败并点名。

不做的两件事，都是为了不制造新的噪音：

1. **不逼现有 CRLF 文件转成 LF**。那会是一个 1.4 万行的纯换行提交，
   把 `git blame` 对 19 个源文件全部作废 —— 修一个可读性问题却制造一个
   更大的可读性问题。它们会在各自下次被真正修改时自然转成 LF。
2. **不加 `.gitattributes`**。试过：加了之后那 19 个文件会**永远**显示为
   `M`（Git 每次比对都认为工作区是 CRLF、索引里该是 LF），除非先提交
   那 1.4 万行规范化。也就是说它把"一次性噪音"换成了"持续噪音"。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

# 基线：这些文件在引入本测试时**已经是** CRLF，属于历史遗留，不算违规。
# 新增条目请三思 —— 正常情况下不该有新的 CRLF 文件。
CRLF_BASELINE = frozenset({
    "backend/app/archive.py",
    "backend/app/core/dispatch.py",
    "backend/app/events.py",
    "backend/app/handlers/browse.py",
    "backend/app/handlers/uploads.py",
    "backend/app/indexer.py",
    "backend/app/server.py",
    "backend/app/state.py",
    "backend/app/static.py",
    "backend/app/thumbs.py",
    "backend/app/uploads.py",
    "backend/tests/test_archive.py",
    "backend/tests/test_events.py",
    "backend/tests/test_handlers.py",
    "backend/tests/test_server.py",
    "backend/tests/test_static.py",
    "backend/tests/test_uploads.py",
    "docs/04-接口设计文档.md",
    "scripts/smoke_real.py",
})

TEXT_SUFFIXES = frozenset({
    ".py", ".md", ".txt", ".json", ".toml", ".yml", ".yaml",
    ".js", ".mjs", ".ts", ".vue", ".css", ".html", ".spec",
    ".ini", ".cfg", ".sh", ".bat", ".cmd", ".ps1", ".gitignore",
})


def _tracked_text_files() -> list[str]:
    r = subprocess.run(
        ["git", "ls-files", "-z"], cwd=REPO,
        capture_output=True, check=True,
    )
    out = []
    for raw in r.stdout.split(b"\x00"):
        if not raw:
            continue
        name = raw.decode("utf-8", "surrogateescape")
        p = Path(name)
        if p.suffix.lower() in TEXT_SUFFIXES or p.name == ".gitignore":
            out.append(name)
    return out


def _has_git() -> bool:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--git-dir"], cwd=REPO,
            capture_output=True, check=False,
        ).returncode == 0
    except OSError:            # 没有 git 可执行文件
        return False


def _head_crlf() -> dict[str, int]:
    """HEAD 里每个 blob 的 CRLF 行数。

    用 `cat-file --batch` 一次拿完（实测 137 个文件 0.2 秒）。
    不用 `git show HEAD:<f>` 逐个起进程：那是 137 次进程启动，
    单是这个测试就要 5 秒以上，最后会被人加个 `@pytest.mark.slow`
    然后从默认跑里摘掉 —— 那这条防线就等于没有。

    注意用 `-z` 拿文件名：`git ls-files` 默认会把非 ASCII 名字
    转义成 `"docs/07-\346\265..."` 这种 C 风格引号，直接 `Path()`
    打开会失败并被静默跳过 —— 仓库里恰好有 14 个中文名文件，
    第一批扫描就是这么把它们全漏掉的（于是 `docs/04` 的 4759 行
    CRLF 没被发现）。
    """
    names = [
        b.decode("utf-8", "surrogateescape")
        for b in subprocess.run(
            ["git", "ls-files", "-z"], cwd=REPO, capture_output=True, check=True,
        ).stdout.split(b"\x00")
        if b
    ]
    req = "\n".join(f"HEAD:{n}" for n in names).encode("utf-8")
    out = subprocess.run(
        ["git", "cat-file", "--batch"], cwd=REPO,
        input=req, capture_output=True, check=True,
    ).stdout

    result: dict[str, int] = {}
    pos = 0
    for name in names:
        nl = out.index(b"\n", pos)
        header = out[pos:nl].decode("utf-8", "replace")
        pos = nl + 1
        parts = header.split()
        if len(parts) < 3 or not parts[2].isdigit():
            continue                      # missing / 非 blob
        size = int(parts[2])
        blob = out[pos:pos + size]
        pos += size + 1
        n = blob.count(b"\r\n")
        if n:
            result[name] = n
    return result


pytestmark = pytest.mark.skipif(
    not _has_git(), reason="不在 git 仓库里（例如打包后的分发目录）",
)


class TestLineEndings:
    def test_no_new_crlf_files(self) -> None:
        """基线之外出现 CRLF = 被 `Set-Content` 之类的工具改写了。

        这条是那次事故的直接防线：`docs/07` 全文 3425 行被改成 CRLF 时，
        如果早就有这条断言，`pytest` 会立刻点名它，而不是等提交完去看
        `--stat` 里的 6840 行才发现。

        ★ 判据**分两向**，而不是"工作区有没有 CRLF"：后者会把 19 个
        历史遗留文件永久判成失败，于是这条测试只有两种结局：
        被人加进忽略名单，或者被人删掉。

          · 基线之外的文件 → 工作区不许有 CRLF（被 Set-Content 之类改成 CRLF）
          · 基线之内的文件 → 工作区必须有 CRLF（被 `newline="\\n"` 之类洗成 LF）

        ★ 第二向是补上的。原先只查一向，于是 `docs/04`（基线内、CRLF）
        被整份改写成 LF、5103 行换行全变、`git diff --stat` 显示 +5455/−5155
        而 `--ignore-all-space` 只有 +369/−69 —— **全库 1397 条测试全绿通过**，
        一直要等提交推送后才被 `test_baseline_matches_head` 发现，
        而那条的提示是"请从基线删掉"，指的恰好是相反的方向。
        """
        head_crlf = _head_crlf()
        offenders = []
        for name in _tracked_text_files():
            try:
                n = (REPO / name).read_bytes().count(b"\r\n")
            except OSError:
                continue
            if name in CRLF_BASELINE:
                # 基线内：HEAD 里是 CRLF，工作区也必须是 CRLF
                if name in head_crlf and not n:
                    offenders.append((name, 0))
            else:
                # 基线外：工作区不许出现 CRLF
                if n and name not in head_crlf:
                    offenders.append((name, n))

        assert not offenders, (
            "以下已跟踪文本文件的换行符被整篇改写了 —— "
            "多半是被某个工具（PowerShell 的 Set-Content、编辑器、格式化器）"
            "整篇改写了换行符。这种改动会让整个文件在 diff 里显示为全改，"
            "`git blame` 失效，但 `git diff --ignore-all-space` 是空的"
            "（即 0 行语义改动）。\n"
            "  · CRLF=0 的：HEAD 里是 CRLF，被洗成了 LF —— 转回 CRLF\n"
            "  · CRLF>0 的：HEAD 里是 LF，被改成了 CRLF —— 转回 LF\n"
            + "\n".join(
                f"  {name}  CRLF={n}"
                + ("  → 转回 CRLF" if n == 0 else "  → 转回 LF")
                for name, n in offenders
            )
            + "\n提示：用 Python 读写二进制（read_bytes/write_bytes）不会改换行符；"
              "`io.open(..., newline='')` 读、`newline=''` 写也不会。"
              "而 PowerShell 的 Set-Content / Out-File、以及"
              "`io.open(..., newline='\\n')` 写整份文件都会。"
        )

    def test_baseline_matches_head(self) -> None:
        """基线必须与 HEAD 的实际情况一致 —— 否则它会慢慢变成假名单。

        三种情况都要发现：
        ① 基线里的文件已经不在 HEAD 里了（改名/删除）；
        ② 基线里的文件在 HEAD 里已经不是 CRLF（说明可以把它从基线删掉，
           基线在无谓地放宽标准）；
        ③ HEAD 里有 CRLF 文件却不在基线里（有人新增了 CRLF 文件，
           却没更新基线 —— 那下一条 `test_no_new_crlf_files` 会永远
           放过它，因为它在 HEAD 里本来就是 CRLF）。
        """
        head_crlf = _head_crlf()
        problems = []

        for name in sorted(CRLF_BASELINE - set(head_crlf)):
            if (REPO / name).is_file():
                problems.append(
                    f"{name}（基线里有，HEAD 里却变成了 LF —— "
                    "不是删基线，而是把它转回 CRLF 再提交）"
                )
            else:
                problems.append(f"{name}（基线里有，但文件已不存在，请从基线删掉）")
        for name in sorted(set(head_crlf) - CRLF_BASELINE):
            problems.append(f"{name}（HEAD 里是 CRLF，不在基线里，请加进基线）")

        assert not problems, (
            "CRLF_BASELINE 与 HEAD 不一致，请更新 backend/tests/test_repo_hygiene.py：\n"
            + "\n".join(f"  {p}" for p in problems)
        )
