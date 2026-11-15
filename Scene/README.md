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

同一街区最多一辆异常车；重复生成会报错，移除原目标后可重新生成。道路选点归最近街区，车辆始终保留生成时的街区归属。

### 1.2 set_vehicle_behavior

设置指定车辆的驾驶模式，仅支持超速、逆向超速和混合。三种模式的目标车速相同；混合从顺向超速开始，间隔18至30秒切换为逆向超速4至7秒，再恢复顺向超速。

| 入参 | 类型 | 含义 |
|---|---|---|
| `vehicle_id` | `str` | `spawn_vehicle()` 返回的车辆 ID |
| `behavior` | `str` | 行为名称，取值见下表 |

| `behavior` | 行为 | 默认目标 |
|---|---|---|
| `"speeding"` | 超速行驶 | 104.4 km/h |
| `"wrong_way"` | 占用对向车道超速行驶 | 104.4 km/h，无可行对向路面时等待 |
| `"mixed"` | 持续超速，偶尔逆行 | 全程以104.4 km/h为目标，只切换顺向与逆向行驶 |

车辆生成后默认超速行驶。

| 出参 | 类型 | 含义 |
|---|---|---|
| 返回值 | `None` | 确认指令已应用后返回；加减速过程继续执行 |

入参样例与调用：

```python
from Scene.vehicle import spawn_vehicle, set_vehicle_behavior

vehicle_id = spawn_vehicle((2286.37, 18.91))
result = set_vehicle_behavior(vehicle_id, "mixed")

```

出参样例：

```python
None  # result
```

### 1.3 start_escape

让指定异常车离开原来的闭环路线，沿连通道路随机驶向远处街区。默认逃逸120个仿真秒，计时结束后制动。

逃逸期间不跟随普通车流排队，允许穿过普通交通车；保留路面和障碍检查，弯道会减速。本函数只控制车辆，不派出无人机。需要同时追踪时，直接调用 `pursue_vehicle()`，无需先调用本函数。

| 入参 | 类型 | 含义 |
|---|---|---|
| `vehicle_id` | `str` | `spawn_vehicle()` 返回的车辆 ID |
| `duration_s` | `float` | 逃逸时长，单位为仿真秒；默认 `120.0`，必须大于0且有限，不包含结束后的制动时间 |
| `seed` | `int` 或 `None` | 随机选路种子；默认 `None`，可指定整数，在起点、朝向和道路条件相同时复现选路 |

| 出参 | 类型 | 含义 |
|---|---|---|
| 返回值 | `dict` | 车辆逃逸状态；确认启动后即返回，不等待逃逸结束 |

入参样例与调用：

```python
from Scene.vehicle import spawn_vehicle
from Scene.vehicle.escape_vehicle import start_escape

vehicle_id = spawn_vehicle((2286.37, 18.91))
result = start_escape(vehicle_id, duration_s=120.0, seed=42)
```

出参样例（仅列主要字段）：

```python
{
    "vehicle_id": "abnormal_vehicle_02",
    "phase": "escaping",
    "duration_s": 120.0,
    "elapsed_s": 0.0,
    "travelled_m": 0.0,
}
```

车辆不存在、已有未完成的逃逸任务或附近道路无法连通远处街区时抛出异常。

### 1.4 escape_status

查询指定车辆的逃逸状态，也可提前结束逃逸并请求制动。

| 入参 | 类型 | 含义 |
|---|---|---|
| `vehicle_id` | `str` | 已通过 `start_escape()` 或 `pursue_vehicle()` 启动逃逸的车辆 ID |
| `stop` | `bool` | 默认 `False`，仅查询；设为 `True` 时请求提前制动；仅限关键字传入 |

| 出参 | 类型 | 含义 |
|---|---|---|
| 返回值 | `dict` | 当前逃逸状态；任务已结束时返回该次逃逸的完成状态 |

主要返回字段：

| 字段 | 含义 |
|---|---|
| `phase` | `"escaping"` 正在逃逸、`"stopping"` 正在制动、`"complete"` 制动完成 |
| `duration_s` | 设定的逃逸时长，单位为仿真秒 |
| `elapsed_s` | 从逃逸开始经过的仿真秒数，包含制动时间 |
| `travelled_m` | 逃逸开始后的累计行驶距离，单位米 |

入参样例与调用：

```python
from Scene.vehicle.escape_vehicle import escape_status

vehicle_id = "abnormal_vehicle_02"
state = escape_status(vehicle_id)
# 需要提前结束时：
state = escape_status(vehicle_id, stop=True)
```

出参样例（仅列主要字段，数值仅作示意）：

```python
{
    "vehicle_id": "abnormal_vehicle_02",
    "phase": "stopping",
    "duration_s": 120.0,
    "elapsed_s": 45.3,
    "travelled_m": 620.5,
}
```

`stop=True` 不等待制动完成，可再次查询，直到 `phase` 为 `"complete"`。车辆不存在或尚未启动逃逸时抛出异常。

## 2 持刀人员 API

### 2.1 spawn_armed_person

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

### 4.1 get_probe_records

获取车辆经过路口探头时的车牌、车速、行驶方向和采集时间。默认查询最新一次仿真的全部过车记录，可按探头、车牌或时间范围筛选。

| 入参 | 类型 | 含义 |
|---|---|---|
| `probe_id` | `str` | 可选，探头编号，例如 `"P_096-1"` |
| `plate` | `str` | 可选，完整车牌或车牌后缀 |
| `time_range` | `(start, end)` | 可选，起止仿真时间，单位秒，例如 `(120, 180)` |

| 出参 | 类型 | 含义 |
|---|---|---|
| 返回值 | `list[dict]` | 按采集顺序排列的过车记录；无匹配时返回 `[]` |

入参样例与调用：

```python
from Scene.probe import get_probe_records

records = get_probe_records(probe_id="P_096-1", time_range=(120, 180))
```

出参样例（仅列主要字段）：

```python
[
    {
        "t": 125.30,                  # 采集时间，单位为仿真秒
        "probe": "P_096-1",           # 探头编号
        "junction": "P_096",          # 路口编号
        "plate": "CTQ-9D475",         # 识别到的车牌
        "read_ok": True,              # 是否成功读牌
        "speed_kmh": 82,              # 记录车速，单位 km/h
        "direction": "approaching",   # approaching 驶向探头，departing 驶离探头
        "heading_deg": -171,          # 车辆行驶朝向，单位度
        "xy_m": [2163.4, 115.4],      # 探头位置，UE 世界坐标，单位米
    }
]
```
