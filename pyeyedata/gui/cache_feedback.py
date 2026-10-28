"""Concise feedback from actual cache decisions, including recomputation reasons."""
from collections.abc import Mapping


def cache_miss_feedback(audit: Mapping) -> list[str]:
    stages = audit.get("stages", [])
    misses = [item for item in stages if isinstance(item, Mapping)
              and item.get("outcome") == "miss"]
    labels = {"base": "质量与预处理", "eyes": "眼动数据流", "events": "事件检测",
              "artifact:individual": "个体结果", "artifact:group": "群体结果",
              "artifact:advanced": "高级结果"}
    reasons = {"not_retained": "没有保留匹配缓存",
               "artifact_integrity_mismatch": "缓存完整性核验失败，已拒绝复用",
               "cleared_cancelled_or_stale": "缓存已清除、取消或过期"}
    decisions = list(dict.fromkeys(
        f"{labels.get(item.get('stage'), item.get('stage'))}："
        f"{reasons.get(item.get('reason'), item.get('reason', '资格核验未通过'))}"
        for item in misses))
    messages = []
    if misses and not any(isinstance(item, Mapping) and item.get("outcome") == "hit" for item in stages):
        messages.append("本次没有命中可复用的会话缓存，按当前输入计算；会话缓存不代表跨启动断点接续。")
    if decisions:
        messages.append("本次未复用原因：" + "；".join(decisions[:8]))
    return messages
