"""在指定地点附近的临街人行道生成病人。"""

from pathlib import Path
import sys

if __name__ == "__main__" and not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    __package__ = "Abnormal.patient"


def spawn_patient(position) -> str:
    """输入 UE 世界 (x, y) 米制坐标，返回唯一 patient_ID。

    在 100 米内选择可用人行道，自动贴地；生成后不适约 6 秒、倒地并保持卧地。
    每次新增一名病人；没有合适落点、素材缺失或仿真不可连接时抛出异常。
    """
    from ..armed_person.spawn_armed_person import _spawn_person

    return _spawn_person(position, "patient")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("x", type=float, help="参考地点 UE 世界 X，米")
    parser.add_argument("y", type=float, help="参考地点 UE 世界 Y，米")
    args = parser.parse_args()
    print(spawn_patient((args.x, args.y)))
