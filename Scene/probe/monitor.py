"""读取探头布设与 UE 过车记录，不依赖网页或仿真通信。"""

import json
import math
from pathlib import Path
import re


class ProbeMonitor:
    """layout_path 为布设 JSON，events_dir 为 UE 的 Saved/TrafficProbes。"""

    def __init__(self, layout_path, events_dir=None):
        self.layout_path = Path(layout_path)
        self.events_dir = Path(events_dir) if events_dir is not None else None

    def get_probes(self, *, probe_id=None, junction=None):
        """返回布设列表；可按探头或路口编号筛选，不代表运行时启用状态。"""
        data = json.loads(self.layout_path.read_text(encoding="utf-8-sig"))
        return [p for p in data["probes"]
                if (probe_id is None or p["id"] == probe_id)
                and (junction is None or p["junction"] == junction)]

    def get_records(self, *, probe_id=None, junction=None, plate=None, start=None,
                    end=None, session=None, cursor=None, limit=200):
        """分页读取一场仿真的记录，按文件顺序返回，时间为仿真秒。

        默认选择最新场次；cursor 原样传回以继续读取。切换筛选条件时清空
        cursor。plate 支持完整车牌或后缀；未指定车牌时保留读牌失败记录。
        """
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
        if self.events_dir is None:
            raise ValueError("需要指定 UE 探头记录目录 events_dir")
        if not self.events_dir.is_dir():
            raise FileNotFoundError("探头记录目录不存在，请确认 UE 已启用探头采集")
        files = sorted(p for p in self.events_dir.glob("events_*.jsonl")
                       if p.is_file() and re.fullmatch(r"events_\d{8}_\d{6}", p.stem))
        sessions = [p.stem for p in reversed(files)]
        if session is not None and session not in sessions:
            raise FileNotFoundError("所选仿真场次不存在")
        if not files:
            return {"session": None, "sessions": [], "records": [], "cursor": None,
                    "has_more": False, "reset": True}
        path = self.events_dir / (session + ".jsonl") if session else files[-1]
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
                if junction is not None and row["junction"] != junction:
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
