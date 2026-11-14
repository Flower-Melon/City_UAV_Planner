"""在指定地点附近的临街人行道生成病人。"""

from pathlib import Path
import sys

if __name__ == "__main__" and not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    __package__ = "Scene.patient"


def spawn_patient(position=None, *, zone_id=None) -> str:
    """按街区zone_id随机生成，或在position附近生成，返回唯一patient_ID。

    指定position时在100米内选点；仅指定zone_id时随机选择街区人行道。
    与持刀人员当前位置及日常巡游段保持30米间距；生成后不适约6秒、倒地并保持卧地。
    同一街区最多一名病人；已有目标或没有合适落点时抛出异常。
    """
    from ..armed_person.spawn_armed_person import _spawn_person

    return _spawn_person(position, "patient", zone_id=zone_id)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("x", type=float, help="参考地点 UE 世界 X，米")
    parser.add_argument("y", type=float, help="参考地点 UE 世界 Y，米")
    args = parser.parse_args()
    print(spawn_patient((args.x, args.y)))
