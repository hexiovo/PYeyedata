#!/usr/bin/env python3
"""Assemble the portable, attachment-ready single-file skill artifact."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REFERENCES = (
    "references/intake-and-orchestration.md",
    "references/native-processing-and-statistics.md",
    "references/conversions-and-data.md",
    "references/study-pack-and-runbook.md",
)
REFERENCE_TITLES = {
    "references/intake-and-orchestration.md": "研究信息收集与流程编排",
    "references/native-processing-and-statistics.md": "原生预处理与统计边界",
    "references/conversions-and-data.md": "输入映射、转换与追溯",
    "references/study-pack-and-runbook.md": "研究配置包与主试操作手册",
}


def main() -> None:
    skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
    _, body = skill.split("---", 2)[1:]
    for relative, title in REFERENCE_TITLES.items():
        body = body.replace(f"({relative})", f"(#{title})")
    body = body.replace("`scripts/validate_plan.py`", "下文内嵌的 `validate_plan.py` 校验工具")
    parts = [
        "# PYeyedata 实验配置技能｜单文件版\n\n"
        "使用时，请将本文件与实验设计、现有数据字典和脱敏样例一并提供给支持读取 Markdown 附件的 AI 工具。本文件包含技能说明、全部参考流程和完整校验工具源码，无须依赖未附带的文档或本机绝对路径。"
        "如需调用原生校验，还须使用匹配版本的 PYeyedata 源码和运行环境；缺少时应将校验状态记为 `not_run`，不能声称方案可加载。请根据工具的实际能力和可核验证据如实报告。\n",
        "## 技能说明\n" + body.strip() + "\n",
    ]
    for relative in REFERENCES:
        path = ROOT / relative
        parts.append("## " + REFERENCE_TITLES[relative] + "\n\n" + path.read_text(encoding="utf-8").strip() + "\n")
    helper = (ROOT / "scripts" / "validate_plan.py").read_text(encoding="utf-8").rstrip()
    parts.append("## 内嵌校验工具：`validate_plan.py`\n\n" +
                 "如需使用，请将以下源码另存为 `validate_plan.py`，并在目标 PYeyedata 检出目录及对应环境中运行。"
                 "工具会调用所选源码的真实 API 校验配置，不会执行分析。\n\n" +
                 "```python\n" + helper + "\n```\n")
    destination = ROOT / "SINGLE_FILE_SKILL.md"
    destination.write_text("\n".join(parts), encoding="utf-8", newline="\n")
    print(destination)
    print(f"bytes={destination.stat().st_size}")


if __name__ == "__main__":
    main()
