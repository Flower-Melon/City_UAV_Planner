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