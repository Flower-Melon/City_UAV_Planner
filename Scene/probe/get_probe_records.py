"""查询交通探头采集的车辆经过记录。"""

import json
import math
from pathlib import Path
import re
import sys

if __name__ == "__main__" and not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def get_probe_records(probe_id=None, plate=None, time_range=None):
    """查询最新一次仿真的过车记录，返回按采集顺序排列的记录列表。

    probe_id、plate 可省略；time_range 为起止仿真秒数 (start, end)。
    """
    if time_range is None:
        start = end = None
    else:
        if not isinstance(time_range, (tuple, list)) or len(time_range) != 2:
            raise ValueError("time_range 必须为起止仿真秒数 (start, end)")
        start, end = time_range
    from UAV.runtime.airsim_state import editor_log
    events_dir = editor_log().parent.parent / "TrafficProbes"
    page = _read_probe_records(events_dir, probe_id=probe_id, plate=plate, start=start, end=end)
    records = page['records']
    while page['has_more']:
        page = _read_probe_records(events_dir, probe_id=probe_id, plate=plate, start=start, end=end,
                                   session=page['session'], cursor=page['cursor'])
        records.extend(page['records'])
    return records


def _read_probe_records(events_dir, *, probe_id=None, plate=None, start=None, end=None,
                        session=None, cursor=None, limit=500):
    """内部增量读取，供记录查询和空地融合复用。"""
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 500:
        raise ValueError("每页记录数量必须为 1～500")
    for value in (start, end):
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                  or not math.isfinite(value) or value < 0):
            raise ValueError("起止时间必须为非负的仿真秒数")
    if start is not None and end is not None and start > end:
        raise ValueError("开始时间不能大于结束时间")
    if session is not None and not re.fullmatch(r"events_\d{8}_\d{6}", session):
        raise ValueError("无效的仿真场次")
    if cursor is not None and (not isinstance(cursor, dict)
                               or not isinstance(cursor.get("session"), str)
                               or not isinstance(cursor.get("offset"), int)
                               or isinstance(cursor.get("offset"), bool)
                               or cursor["offset"] < 0):
        raise ValueError("无效的读取游标")
    events_dir = Path(events_dir)
    if not events_dir.is_dir():
        raise FileNotFoundError("探头记录目录不存在，请确认 UE 已启用探头采集")
    files = sorted(p for p in events_dir.glob("events_*.jsonl")
                   if p.is_file() and re.fullmatch(r"events_\d{8}_\d{6}", p.stem))
    sessions = [p.stem for p in reversed(files)]
    if session is not None and session not in sessions:
        raise FileNotFoundError("所选仿真场次不存在")
    if not files:
        return {"session": None, "sessions": [], "records": [], "cursor": None,
                "has_more": False, "reset": True}
    path = events_dir / (session + ".jsonl") if session else files[-1]
    selected = path.stem
    size = path.stat().st_size
    reset = cursor is None or cursor["session"] != selected or cursor["offset"] > size
    offset = 0 if reset else cursor["offset"]
    suffix = plate.strip().upper() if plate else None
    records = []
    with path.open("rb") as stream:
        if offset:
            stream.seek(offset - 1)
            if stream.read(1) != b"\n":
                raise ValueError("读取游标未指向完整记录边界")
        stream.seek(offset)
        while stream.tell() < size and len(records) < limit:
            line = stream.readline(size - stream.tell())
            if not line.endswith(b"\n"):
                break  # UE 可能正在写最后一行；下次从该行起点重新读取。
            try:
                row = json.loads(line.decode("utf-8-sig"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise ValueError(f"{selected} 在字节 {offset} 处的记录无效") from error
            offset = stream.tell()
            if probe_id is not None and row["probe"] != probe_id:
                continue
            if suffix and not row.get("plate", "").upper().endswith(suffix):
                continue
            if start is not None and row["t"] < start:
                continue
            if end is not None and row["t"] > end:
                continue
            records.append(row)
    return {"session": selected, "sessions": sessions, "records": records,
            "cursor": {"session": selected, "offset": offset},
            "has_more": len(records) == limit and offset < size, "reset": reset}


if __name__ == "__main__":
    print(json.dumps(get_probe_records(), ensure_ascii=False, indent=2))
