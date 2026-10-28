# 异常场景操作说明

车辆、持刀人员和病人均由 API 创建。已取消原配置的默认自动生成，修改生效需停止并重新开始 Play。

进入 GroundFix3 地图的 Play 后，在项目根目录使用以下接口；也可统一从 `Abnormal` 或 `Abnormal.API` 导入。

## 1 异常车辆 API

### 1.1 spawn_vehicle

在指定道路位置新增一辆异常车，自动生成经过出生点的固定循环路线。生成后车辆默认停车，等待行为指令。

| 入参 | 类型 | 含义 |
|---|---|---|
| `position` | `(x, y)` 或 `[x, y]` | UE 世界 XY 坐标，单位米；自动确定高度和朝向，默认允许向最近机动车道中心线贴合最多 5 米 |

| 出参 | 类型 | 含义 |
|---|---|---|
| 返回值 | `str` | 新车辆的唯一 ID，后续控制该车时使用 |

入参样例与调用：

```python
from Abnormal.vehicle import spawn_vehicle

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
from Abnormal.vehicle import spawn_vehicle, set_vehicle_behavior

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

指定一个地点，在其附近马路旁的人行道上新增一名持刀人员。生成后沿附近同一人行道短段往返，形成闭环巡游，保留原场景的站立、行走、奔跑和威胁动作。

| 入参 | 类型 | 含义 |
|---|---|---|
| `position` | `(x, y)` 或 `[x, y]` | 参考地点的 UE 世界 XY 坐标，单位米；实际出生点选择该地点附近可用的人行道 |

| 出参 | 类型 | 含义 |
|---|---|---|
| 返回值 | `str` | 新人员的唯一 ID，例如 `"armed_person_01"`，实际编号以返回值为准 |

入参样例与调用：

```python
from Abnormal.armed_person import spawn_armed_person

person_id = spawn_armed_person((2277.93, 73.82))
```

## 3 病人 API

### 3.1 spawn_patient

指定一个地点，在其附近马路旁的人行道上新增一名病人。生成后按原场景逻辑执行：不适约 6 秒 → 倒地 → 卧地等待救援。

| 入参 | 类型 | 含义 |
|---|---|---|
| `position` | `(x, y)` 或 `[x, y]` | 参考地点的 UE 世界 XY 坐标，单位米；实际出生点选择该地点附近可用的人行道 |

| 出参 | 类型 | 含义 |
|---|---|---|
| 返回值 | `str` | 新病人的唯一 ID，例如 `"patient_01"`，实际编号以返回值为准 |

入参样例与调用：

```python
from Abnormal.patient import spawn_patient

patient_id = spawn_patient((2277.93, 73.82))
```
