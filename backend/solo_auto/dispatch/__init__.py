"""v3 调度中心。

该包只在 ``dispatch.engine = "v3"`` 时参与装配；v2 调度仍保留为完整回退路径。
"""

from solo_auto.dispatch.kernel import Dispatcher

__all__ = ["Dispatcher"]
