"""认知层 —— 黑武士的内核所在。

- :mod:`.kernel`  认知内核门面（PASM 接入 / 档位探测 / 降级）
- :mod:`.memory`  记忆服务（认知内核 × SQLite 副本）
- :mod:`.affect`  情绪动力学与极简世界模型（预测误差驱动行为）
"""

from .affect import AffectTracker
from .kernel import CognitiveKernel, build_kernel, HAS_PASM, pasm_import_error
from .memory import MemoryService

__all__ = [
    "CognitiveKernel",
    "MemoryService",
    "AffectTracker",
    "build_kernel",
    "HAS_PASM",
    "pasm_import_error",
]
