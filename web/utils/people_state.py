"""读取病人和持刀人员的ID、实时地面位置及当前演示目标。"""

import json
import math
import os
from pathlib import Path
import re
import sys
import traceback

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    __package__ = "web.utils"

_targets = {}
_tick_handle = None


def get_people_state(*, client=None):
    """返回病人和持刀人员的ID、类别与地面XYZ（米）。"""
    return _read_people(client)


def _read_people(client, person_id=None, kind=None):
    from Scene.vehicle.spawn_vehicle import request
    from UAV.runtime.airsim_client import AirSimClient

    if client is None:
        with AirSimClient() as connection:
            return _read_people(connection, person_id, kind)
    return request(client, "snapshot", _controller=Path(__file__).resolve(),
                   _module_name="city_people_state", _registry="_targets", person_id=person_id, kind=kind)


def get_person_state(person_id, *, client=None):
    """按人物ID读取实时位置。"""
    people = _read_people(client, person_id)["people"]
    if not people:
        raise RuntimeError(f"人物{person_id}不存在")
    if len(people) > 1:
        raise RuntimeError(f"存在多个人物{person_id}，无法唯一确定")
    return people[0]


def get_demo_target(kind, *, client=None):
    """返回该类别最近生成的演示人物及其实时位置。"""
    if kind not in ("patient", "armed_person"):
        raise ValueError("人物类别必须为patient或armed_person")
    return _read_people(client, kind=kind)["people"][0]


def _snapshot(person_id=None, kind=None):
    import unreal

    world = unreal.EditorLevelLibrary.get_game_world()
    if world is None:
        raise RuntimeError("当前没有运行中的仿真")
    origin = unreal.GameplayStatics.get_world_origin_location(world)
    people = []
    # 网页创建的人物已按ID登记，单人查询直接读取其Actor，不重新按名称扫描。
    module = sys.modules.get("city_abnormal_people_api")
    if kind is not None:
        person_id = getattr(module, "_demo_targets", {}).get(kind)
        if person_id is None:
            raise RuntimeError(f"尚未登记{kind}演示目标，请先生成对应人物")
    person = getattr(module, "_people", {}).get(person_id) if person_id else None
    registered = (person is not None and unreal.SystemLibrary.is_valid(person.actor)
                  and person.actor.get_world() == world)
    actors = ([person.actor] if registered else
              unreal.GameplayStatics.get_all_actors_of_class(world, unreal.Character))
    for actor in actors:
        names = [actor.get_name()] + [str(t) for t in actor.get_editor_property("tags")]
        ids = ([person_id] if registered else
               sorted(set(n for n in names if re.fullmatch(r"(?:armed_person|patient)_[0-9]+", n))))
        if len(ids) != 1 or person_id is not None and ids[0] != person_id:
            continue
        p = actor.get_actor_location()
        half = actor.get_editor_property("capsule_component").get_scaled_capsule_half_height()
        position = [(p.x + origin.x) / 100, (p.y + origin.y) / 100, (p.z + origin.z - half) / 100]
        if not all(math.isfinite(v) for v in position):
            continue
        people.append({"person_id": ids[0],
                       "kind": "armed_person" if ids[0].startswith("armed_person_") else "patient",
                       "position_m": position})
    if kind is not None and not people:
        raise RuntimeError(f"演示人物{person_id}已不存在，请重新生成")
    return {"people": people}


def dispatch(source, result):
    import unreal

    try:
        data = json.loads(Path(source).read_text(encoding="utf-8"))
        if data["operation"] != "snapshot":
            raise ValueError("不支持的人物查询操作")
        response = {"ok": True, "result": _snapshot(data.get("person_id"), data.get("kind"))}
    except Exception as error:
        response = {"ok": False, "error": str(error)}
        unreal.log_warning(traceback.format_exc())
    temporary = Path(result).with_suffix(".tmp")
    temporary.write_text(json.dumps(response, allow_nan=False), encoding="utf-8")
    os.replace(str(temporary), str(result))


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("person_id", nargs="?")
    args = parser.parse_args()
    result = get_person_state(args.person_id) if args.person_id else get_people_state()
    print(json.dumps(result, ensure_ascii=False))
