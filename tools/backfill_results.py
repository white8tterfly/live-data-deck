"""把自动化用例的执行结果回填到 test_cases/ 的「实测」列。

连接点是 TC 编号：用例用 @pytest.mark.tc("TC-RM-B03") 标注自己负责的编号，
本脚本跑一遍 pytest 收集各编号的通过/失败/跳过，再写回对应 md 文件的「实测」单元格。

三条约定，缺一不可：

- 只写自己绑定的编号：没绑定编号的行属人工项，脚本一律不动，历史结果也不会被清空。
- 单元格必须带测试时间与测试版本，否则事后分不清结果是在哪个代码状态上跑的。
- 启动先做全量校验：引用了 md 里不存在的编号直接报错退出，避免把结果写到不存在的行上；
  同一编号被多条用例覆盖是正常的（取最差结果回填），只提示不阻断。

用法：python tools/backfill_results.py --layer L1
      python tools/backfill_results.py --check      # 只校验编号，不跑测试、不回填
"""
from __future__ import annotations

import argparse
import datetime as dt
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEST_CASES_DIR = ROOT / "docs" / "test_cases"
INDEX_HTML = ROOT / "web" / "index.html"

# 实测列的词表沿用 test_cases/README.md；「跳过」是自动化新增，表示 live 标记未执行
OUTCOME_TEXT = {"passed": "通过", "failed": "失败", "error": "失败", "skipped": "跳过"}
OUTCOME_RANK = {"passed": 0, "skipped": 1, "failed": 2, "error": 2}

_ROW_RE = re.compile(r"^\|\s*(TC-[A-Z]+-[LBIE]\d+)\s*\|")
_HEADER_LINE_PREFIX = "- 最近自动化执行："


class TcCollector:
    """pytest 插件：把每条用例的 TC 标记与最终结果收集成 {编号: (结果, 备注)}。"""

    def __init__(self) -> None:
        self.tc_by_node: dict[str, list[str]] = {}
        self.outcome_by_node: dict[str, tuple[str, str]] = {}

    def pytest_collection_modifyitems(self, config, items) -> None:
        """收集阶段记下 nodeid 与 tc 标记的对应；标记参数允许字符串或字符串列表。"""
        for item in items:
            marker = item.get_closest_marker("tc")
            if marker is None:
                continue
            ids: list[str] = []
            for arg in marker.args:
                if isinstance(arg, (list, tuple, set)):
                    ids.extend(str(x) for x in arg)
                else:
                    ids.append(str(arg))
            if ids:
                self.tc_by_node[item.nodeid] = ids

    def pytest_runtest_logreport(self, report) -> None:
        """记录结果；setup 阶段的 skip 也要算（live 标记就是这样跳过的）。"""
        if report.when == "call" or (
                report.when == "setup" and report.outcome == "skipped"):
            msg = ""
            if report.outcome == "failed" and report.longreprtext:
                msg = report.longreprtext.strip().splitlines()[-1][:60]
            elif report.outcome == "skipped":
                msg = str(getattr(report, "longrepr", "") or "").strip()[:60]
            self.outcome_by_node[report.nodeid] = (report.outcome, msg)

    def results_by_tc(self) -> dict[str, tuple[str, str]]:
        """nodeid → 结果 归并到编号 → 结果；同一编号由多条用例绑定时取最差结果。"""
        out: dict[str, tuple[str, str]] = {}
        for node, ids in self.tc_by_node.items():
            outcome, msg = self.outcome_by_node.get(node, ("error", "用例未执行"))
            for tc in ids:
                prev = out.get(tc)
                if prev is None or OUTCOME_RANK.get(outcome, 2) > OUTCOME_RANK.get(prev[0], 2):
                    out[tc] = (outcome, msg)
        return out


def collect_bindings() -> dict[str, list[str]]:
    """只做收集（--collect-only），拿到编号绑定关系而不跑用例。"""
    import pytest

    collector = TcCollector()
    pytest.main([str(ROOT / "tests"), "-q", "--collect-only",
                 "-p", "no:cacheprovider"], plugins=[collector])
    return collector.tc_by_node


def run_pytest() -> dict[str, tuple[str, str]]:
    """真跑一遍 tests/，返回编号 → 结果。"""
    import pytest

    collector = TcCollector()
    code = pytest.main([str(ROOT / "tests"), "-q", "-p", "no:cacheprovider"],
                       plugins=[collector])
    print(f"[backfill] pytest 退出码 {code}")
    return collector.results_by_tc()


def collect_md_tc_ids() -> dict[str, Path]:
    """扫描 test_cases/ 全部 md，返回 编号 → 所在文件（保留先出现的那个）。"""
    found: dict[str, Path] = {}
    for md in sorted(TEST_CASES_DIR.rglob("*.md")):
        for line in md.read_text(encoding="utf-8").splitlines():
            m = _ROW_RE.match(line)
            if m:
                found.setdefault(m.group(1), md)
    return found


def validate(bindings: dict[str, list[str]],
             md_ids: dict[str, Path]) -> tuple[list[str], list[str]]:
    """返回 (错误清单, 提示清单)。

    错误 = 编号在 test_cases/ 的 md 中不存在，会把结果写到不存在的行上，必须阻断。
    提示 = 同一编号被多条用例覆盖，属正常（一个测试项常常需要多条用例一起验证），
    回填时按最差结果取值，这里只列出来供人工确认绑定有没有绑错。
    """
    errors: list[str] = []
    notes: list[str] = []
    owners: dict[str, str] = {}
    for node, ids in bindings.items():
        for tc in ids:
            if tc in owners:
                notes.append(f"编号 {tc} 由多条用例覆盖：{owners[tc]} 与 {node}")
            else:
                owners[tc] = node
            if tc not in md_ids:
                errors.append(f"编号 {tc}（来自 {node}）在 test_cases/ 的 md 中不存在")
    return errors, notes


def git_short_sha() -> str:
    try:
        sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                             capture_output=True, text=True, timeout=10)
        return sha.stdout.strip() or "nogit"
    except (OSError, subprocess.SubprocessError):
        return "nogit"


def git_is_dirty() -> bool:
    try:
        st = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT,
                            capture_output=True, text=True, timeout=10)
        return bool(st.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        return False


def web_versions() -> str:
    """读 web/index.html 的四个 ?v=，按 styles/onboarding/pulse/app 顺序拼成 64/14/9/78。"""
    try:
        html = INDEX_HTML.read_text(encoding="utf-8")
    except OSError:
        return "?"
    vals = []
    for name in ("styles.css", "onboarding.js", "pulse-strip.js", "app.js"):
        m = re.search(re.escape(name) + r"\?v=(\d+)", html)
        vals.append(m.group(1) if m else "?")
    return "/".join(vals)


def build_version() -> tuple[str, str]:
    """返回 (单元格用的短版本, 摘要行用的完整版本)。

    短版本如 v1.6.2+a1b2c3d；工作区有未提交改动时追加 -dirty，
    否则事后无法区分结果是在哪个代码状态上跑出来的。
    """
    sys.path.insert(0, str(ROOT))
    try:
        from recorder import __version__ as ver
    except Exception:
        ver = "unknown"
    short = f"v{ver}+{git_short_sha()}"
    if git_is_dirty():
        short += "-dirty"
    return short, f"{short} / web:{web_versions()}"


def render_cell(outcome: str, msg: str, version: str, stamp: str) -> str:
    """单元格文本：通过 · v1.6.2+abc1234 · 2026-10-18 17:30；失败项括号里带原因。"""
    text = OUTCOME_TEXT.get(outcome, "失败")
    if outcome in ("failed", "error") and msg:
        text = f"失败（{msg}）"
    return f"{text} · {version} · {stamp}"


def backfill_file(md: Path, updates: dict[str, str], summary_line: str) -> int:
    """就地更新单个 md：只改绑定编号所在行的最后一格，并维护文件头的摘要行。

    未绑定编号的行原样保留（人工结果不被覆盖）。返回实际改动的行数。
    """
    lines = md.read_text(encoding="utf-8").splitlines()
    changed = 0
    out: list[str] = []
    header_done = False
    for line in lines:
        if line.startswith(_HEADER_LINE_PREFIX):
            out.append(summary_line)
            header_done = True
            continue
        m = _ROW_RE.match(line)
        if m and m.group(1) in updates:
            parts = line.split("|")
            if len(parts) >= 4:
                parts[-2] = f" {updates[m.group(1)]} "
                line = "|".join(parts)
                changed += 1
        out.append(line)
    if not header_done:
        insert_at = 1
        for i, line in enumerate(out):
            if line.startswith("- 最近更新："):
                insert_at = i + 1
                break
        out.insert(insert_at, summary_line)
    md.write_text("\n".join(out) + "\n", encoding="utf-8")
    return changed


def main() -> int:
    ap = argparse.ArgumentParser(description="把自动化测试结果回填到 test_cases/ 的实测列")
    ap.add_argument("--layer", default="L1", help="本轮对应的层，目前仅 L1 会回填功能用例")
    ap.add_argument("--check", action="store_true", help="只做编号校验，不跑测试、不回填")
    args = ap.parse_args()

    md_ids = collect_md_tc_ids()
    print(f"[backfill] test_cases/ 共 {len(md_ids)} 个 TC 编号")

    bindings = collect_bindings()
    errors, notes = validate(bindings, md_ids)
    if errors:
        print("[backfill] 编号校验失败，未写入任何单元格：")
        for e in errors:
            print("  -", e)
        return 2
    print(f"[backfill] 编号校验通过：{len(bindings)} 条用例带 tc 标记")
    for n in notes:
        print("  ·", n)
    if args.check:
        return 0

    if args.layer != "L1":
        print(f"[backfill] 层 {args.layer} 不支持回填（L0 的 SMK 属基建自查项，不写功能用例）")
        return 0

    results = run_pytest()
    if not results:
        print("[backfill] 没有采集到任何绑定编号的结果，检查用例是否带了 @pytest.mark.tc")
        return 2

    short_ver, full_ver = build_version()
    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    updates = {tc: render_cell(outcome, msg, short_ver, stamp)
               for tc, (outcome, msg) in results.items()}
    passed = sum(1 for o, _ in results.values() if o == "passed")

    # 同一个 md 里可能绑着多个编号，合并成一次写入，避免重复改写同一个文件
    by_md: dict[Path, dict[str, str]] = {}
    for tc, md in md_ids.items():
        if tc in updates:
            by_md.setdefault(md, {})[tc] = updates[tc]

    summary_line = (f"{_HEADER_LINE_PREFIX}{stamp}（{full_ver}）"
                    f"· {args.layer} 覆盖 {len(updates)} 项（通过 {passed}）")
    touched = 0
    for md, ups in by_md.items():
        touched += backfill_file(md, ups, summary_line)

    print(f"[backfill] 版本 {full_ver}")
    print(f"[backfill] 写回 {len(updates)} 个编号，涉及 {len(by_md)} 个 md，改动 {touched} 行")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
