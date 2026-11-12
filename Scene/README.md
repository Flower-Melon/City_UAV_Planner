# 仿真场景操作说明

## 1 异常车辆 API

### 1.1 spawn_vehicle

提供一个参考位置，系统选择该位置所在或最近的地块，再在该地块外围寻找能顺时针闭环的车道。

| 入参 | 类型 | 含义 |
|---|---|---|
| `position` | `(x, y)` 或 `[x, y]` | 参考位置的 UE 世界 XY 坐标，单位米；实际出生点由系统在目标地块周边车道上选择 |

| 出参 | 类型 | 含义 |
|---|---|---|
| 返回值 | `str` | 新车辆的唯一 ID，后续控制该车时使用 |

入参样例与调用：

```python
from Scene.vehicle import spawn_vehicle

position = (2286.37, 18.91)
vehicle_id = spawn_vehicle(position)
```

出参样例：

```python
"abnormal_vehicle_02"  # vehicle_id，实际编号以返回值为准
```

每次调用都会新增一辆车。创建多辆车时，分别保存返回的 ID 即可独立控制。

### 1.2 set_vehicle_behavior

设置指定车辆的驾驶模式，持续执行至下一次指令。`mixed` 模式内部会随机切换动作，其余模式保持指定行为。

| 入参 | 类型 | 含义 |
|---|---|---|
| `vehicle_id` | `str` | `spawn_vehicle()` 返回的车辆 ID |
| `behavior` | `str` | 行为名称，取值见下表 |

| `behavior` | 行为 | 默认目标 |
|---|---|---|
| `"cruise"` | 巡航 | 75 km/h |
| `"surge"` | 高速行驶 | 104.4 km/h |
| `"weave"` | 左右横摆 | 75 km/h，横摆周期 5 秒，最大意图幅度 3 米 |
| `"brake"` | 急减速后保持低速 | 36 km/h |
| `"wrong_way"` | 占用对向车道行驶 | 75 km/h，无可行对向路面时停车 |
| `"mixed"` | 混合驾驶 | 从巡航开始，按原场景规则自动切换横摆、反向修正、冲刺、急减速和逆行 |
| `"stop"` | 减速停车 | 车辆保留在场景中 |

| 出参 | 类型 | 含义 |
|---|---|---|
| 返回值 | `None` | 确认指令已应用后返回；加减速过程继续执行 |

入参样例与调用：

```python
from Scene.vehicle import spawn_vehicle, set_vehicle_behavior

vehicle_id = spawn_vehicle((2286.37, 18.91))
result = set_vehicle_behavior(vehicle_id, "mixed")

# 需要停车时调用
set_vehicle_behavior(vehicle_id, "stop")
```

出参样例：

```python
None  # result
```

## 2 持刀人员 API

### 2.1 spawn_armed_person

指定一个地点，在其附近马路旁的人行道上新增一名持刀人员。人物沿附近同一人行道短段往返巡游，随机切换站立、行走、奔跑和威胁动作。新人物自动登记为网页持刀处置的演示目标，再次生成时替换登记ID。

| 入参 | 类型 | 含义 |
|---|---|---|
| `position` | `(x, y)` 或 `[x, y]` | 参考地点的 UE 世界 XY 坐标，单位米；实际出生点选择该地点附近可用的人行道 |

| 出参 | 类型 | 含义 |
|---|---|---|
| 返回值 | `str` | 新人员的唯一 ID，例如 `"armed_person_01"`，实际编号以返回值为准 |

入参样例与调用：

```python
from Scene.armed_person import spawn_armed_person

person_id = spawn_armed_person((2277.93, 73.82))
```

## 3 病人 API

### 3.1 spawn_patient

指定一个地点，在其附近马路旁的人行道上新增一名病人。生成后按原场景逻辑执行：不适约 6 秒 → 倒地 → 卧地等待救援。新病人自动登记为网页投药的演示目标，再次生成时替换登记ID。

| 入参 | 类型 | 含义 |
|---|---|---|
| `position` | `(x, y)` 或 `[x, y]` | 参考地点的 UE 世界 XY 坐标，单位米；实际出生点选择该地点附近可用的人行道 |

| 出参 | 类型 | 含义 |
|---|---|---|
| 返回值 | `str` | 新病人的唯一 ID，例如 `"patient_01"`，实际编号以返回值为准 |

入参样例与调用：

```python
from Scene.patient import spawn_patient

patient_id = spawn_patient((2277.93, 73.82))
```

## 4 交通探头 API

### 4.1 ProbeMonitor

创建探头查询对象，后续通过该对象获取探头位置和车辆经过记录。

| 入参 | 类型 | 含义 |
|---|---|---|
| `layout_path` | `str` 或 `Path` | 探头布设文件路径 |
| `events_dir` | `str` 或 `Path` | UE 工程的 `Saved/TrafficProbes` 目录；只查询探头布设时可省略 |

入参样例与调用：

```python
from Scene.probe import ProbeMonitor

monitor = ProbeMonitor(
    layout_path="Map/objects/traffic_probes.json",
    events_dir="D:/你的UE工程/Saved/TrafficProbes",
)
```

### 4.2 get_probes

获取探头的编号、所属路口、位置和朝向。不指定条件时返回全部探头。

| 入参 | 类型 | 含义 |
|---|---|---|
| `probe_id` | `str` | 可选，指定探头编号 |
| `junction` | `str` | 可选，指定路口编号 |

| 出参 | 类型 | 含义 |
|---|---|---|
| 返回值 | `list[dict]` | 符合条件的探头信息列表；无匹配时为 `[]` |

入参样例与调用：

```python
probes = monitor.get_probes(probe_id="P_001-1")
```

出参样例（仅列主要字段）：

```python
[
    {
        "id": "P_001-1",              # 探头编号
        "junction": "P_001",          # 所属路口
        "region": "3_1",              # 地图分区
        "xy_m": [-128.31, -961.70],    # 探头 UE 世界 XY 坐标，单位米
        "yaw_deg": 8.6,               # 探头朝向，单位度
    }
]
```

### 4.3 get_records

获取车辆经过探头的记录，可按探头、路口、车牌和时间筛选。默认查询最新一次仿真。

| 入参 | 类型 | 含义 |
|---|---|---|
| `probe_id` | `str` | 可选，指定探头编号 |
| `junction` | `str` | 可选，指定路口编号 |
| `plate` | `str` | 可选，完整车牌或车牌后缀 |
| `start`、`end` | `float` | 可选，仿真开始后的起止秒数 |
| `session` | `str` | 可选，指定返回的 `sessions` 中某次仿真 |
| `cursor` | `dict` | 可选，传入上次返回的 `cursor`，继续读取后续记录 |
| `limit` | `int` | 可选，本次最多返回的记录数，默认 200，最大 500 |

| 出参字段 | 类型 | 含义 |
|---|---|---|
| `records` | `list[dict]` | 按采集顺序排列的过车记录；无匹配时为 `[]` |
| `session`、`sessions` | `str`、`list[str]` | 当前查询的仿真及可选仿真列表；尚无记录文件时 `session` 为 `None` |
| `cursor` | `dict` 或 `None` | 下次继续读取时使用，原样传回即可 |
| `has_more` | `bool` | 是否还有记录可继续读取 |
| `reset` | `bool` | 是否应清空之前累计的结果；首次查询或切换仿真时为 `True` |

入参样例与调用：

```python
result = monitor.get_records(probe_id="P_001-1", start=0, end=600)
records = result["records"]

# 后续需要继续读取时，保持查询条件不变并传回 cursor
next_result = monitor.get_records(
    probe_id="P_001-1", start=0, end=600, cursor=result["cursor"],
)
```

`records` 中的单条记录样例（仅列主要字段）：

```python
{
    "t": 125.30,                     # 经过时的仿真秒数
    "probe": "P_001-1",              # 记录该车的探头
    "junction": "P_001",             # 所属路口
    "plate": "CTA-VC467",            # 识别到的车牌
    "read_ok": True,                 # 是否成功读到车牌
    "direction": "approaching",      # approaching 驶来，departing 驶离
    "speed_kmh": 35,                 # 车辆速度，单位 km/h
    "xy_m": [-128.3, -961.7],         # 探头位置，不是车辆位置
}
```

读牌失败时，`plate` 为空字符串、`read_ok` 为 `False`。更改筛选条件时重新查询，不传旧的 `cursor`。
