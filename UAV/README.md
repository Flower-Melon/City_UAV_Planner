# UAV 功能说明

分为`API`和`utils`两部分，其中`API`提供功能函数直接面向`airsim`仿真环境，调用直接对应仿真无人机的具体动作，`utils`为`API`的功能函数实现提供具体的算法细节（如`plan_flight_path.py`封装最短路径与飞行速度分段规划）

## 1 API

### 1.1 fly_to_point

调用 `utils.plan_flight_path` 获取路径与速度分段，自动起飞、升至指定高度并沿道路拓扑飞往目标；拐弯及接近终点时降速，完成后制动、校准位置并悬停。

| 入参 | 类型 | 含义 |
|---|---|---|
| `drone_id` | `int` 或 `str` | 无人机编号，`1` 或 `"UAV_01"` |
| `target_position` | `(x, y)` 或 `[x, y]` | 目标位置，UE 世界坐标，单位米；不在拓扑边上时飞往其最近投影点 |
| `flight_height` | `float` | 相对起飞地面的飞行高度，单位米，大于 `0`；航程中保持此高度平面 |
| `flight_speed` | `float` | 巡航速度上限，单位米/秒，大于 `0`，默认 `25.0`；拐弯及接近终点时自动降速 |

| 出参 | 类型 | 含义 |
|---|---|---|
| 返回值 | `None` | 同步完成飞行并悬停后返回，无路径或位置数据返回 |

入参样例与调用：

```python
from UAV.API import fly_to_point

drone_id = "UAV_01"
target_position = (2123.25, -2288.2970827809468)
flight_height = 20.0
flight_speed = 8.0
result = fly_to_point(drone_id, target_position, flight_height, flight_speed)
```

出参样例：

```python
None  # result
```

### 1.2 reset_uav

停止飞行并将无人机恢复到初始位置与姿态，上锁并释放 API 控制权，同时清理起飞地面基准。指定编号时从 AirSim 配置读取初始状态，仅复位该无人机；不传编号时使用 AirSim 内置方法复位全部无人机。

| 入参 | 类型 | 含义 |
|---|---|---|
| `drone_id` | `int`、`str` 或 `None` | 无人机编号，如 `1` 或 `"UAV_01"`；默认 `None`，表示全部无人机 |

| 出参 | 类型 | 含义 |
|---|---|---|
| 返回值 | `None` | 同步完成复位后返回，无返回数据 |

入参样例与调用：

```python
from UAV.API import reset_uav

reset_uav(1)           # 复位 UAV_01
reset_uav("UAV_02")    # 复位 UAV_02
reset_uav()            # 复位全部无人机
```

出参样例：

```python
None
```

### 1.3 search_area

巡查指定区域边界外环带内的道路，默认宽度 50 米。先计算完整巡查路线并检查建筑净空，再起飞、沿路飞行并定时拍照，完成后悬停。

| 入参 | 类型 | 含义 |
|---|---|---|
| `drone_id` | `int` 或 `str` | 无人机编号，`1` 或 `"UAV_01"` |
| `target` | `dict` | 搜索目标，包含区域 ID 和搜索范围，字段见下表；可直接传入规划结果中的 `SearchArea.target` |
| `flight_height` | `float` | 相对起飞地面的飞行高度，单位米，大于 `0`；需满足沿途建筑净空检查 |
| `flight_speed` | `float` | 巡航速度上限，单位米/秒，大于 `0`，默认 `5.0`；拐弯及短路段自动降速 |
| `capture_interval` | `float` | 拍照间隔，单位秒，大于 `0`，默认 `3.0`；实际间隔还受取图和保存耗时影响 |
| `output_dir` | `str` 或 `Path` | 结果保存的父目录，默认是 `UAV/` 下的 `search_results/` |

`target` 字段：

| 字段 | 类型 | 含义 |
|---|---|---|
| `zone_id` | `str` | `Map/zones/functional_zones.json` 中真实的区域 ID，如 `"F-SCHOOL-1"` |
| `buffer_m` | `float` | 区域边界外环带宽度，单位米，大于 `0`；省略时默认 `50` |

| 出参 | 类型 | 含义 |
|---|---|---|
| 返回值 | `None` | 同步完成飞行、悬停和停止拍照后返回，释放无人机任务锁 |

入参样例与调用：

```python
from UAV.API import search_area

target = {
    "zone_id": "F-SCHOOL-1",
    "buffer_m": 50,
}
result = search_area(
    "UAV_01",
    target,
    flight_height=40.0,
    flight_speed=5.0,
    capture_interval=3.0,
)
```

出参样例：

```python
None  # result；飞行与拍照已结束
```

## 2 utils

### 2.1 plan_flight_path.py

`plan_flight_path` 一次完成最短路径与速度分段规划。使用 Dijkstra 算法沿拓扑图双向寻路，再根据转弯及加减速距离安排速度；起点不在图上时先接入最近边，目标不在图上时仅到达最近投影点。返回运行时变量，不保存文件，飞行高度由调用方设置。

| 入参 | 类型 | 含义 |
|---|---|---|
| `current_position` | `(x, y)` 或 `[x, y]` | 无人机当前位置，UE 世界 XY 坐标，单位米；允许位于图外 |
| `target_position` | `(x, y)` 或 `[x, y]` | 目标位置，同一坐标系；图外目标以最近拓扑边投影点为终点 |
| `flight_speed` | `float` | 巡航速度上限，单位米/秒，大于 `0`，默认 `25.0` |

| 出参 | 类型 | 含义 |
|---|---|---|
| `(path, segments)` | `tuple` | 两项分别为最短路径和全部飞行分段 |
| `path` | `list[tuple[float, float]]` | 原始最短 XY 路径，首点为当前位置，末点为目标投影点 |
| `segments` | `list[tuple[list[tuple[float, float]], float]]` | 按飞行顺序排列的 `(XY路径点列表, 速度)`；速度单位米/秒，无需水平移动时为空列表 |

入参样例与调用：

```python
from UAV.utils import plan_flight_path

path, segments = plan_flight_path(
    (439.42848, -528.55233),
    (440.05435, -532.38739),
    flight_speed=25.0,
)
```

出参样例：

```python
path = [
    (439.42848, -528.55233),
    (439.4284816220775, -528.5523297352812),
    (440.05435324415504, -532.3873894705624),
]
segments = [([(439.42848, -528.55233), (440.05435324415504, -532.3873894705624)], 1)]
```

### 2.2 airsim_client.py

封装与 AirSim 仿真环境的通信、位置转换和飞行控制，负责让无人机实际执行规划好的路线。主要功能如下：

- `AirSimClient`：连接 AirSim，发送控制指令并读取仿真状态。
- `AirSimFleet.read`：读取各架无人机的位置和可用状态。
- `vehicle_name`：根据无人机编号或名称，确定要控制的无人机。
- `vehicle_lock`：防止同一架无人机同时执行多个任务。
- `vector`：读取并检查仿真返回的三维坐标。
- `editor_log`、`calibrate`：获取仿真中的参考位置，建立地图坐标与仿真坐标的对应关系。
- `WorldFrame.vehicle`：获取无人机在地图和仿真中的位置。
- `WorldFrame.ground_z`：测量起飞地面高度，为飞行高度提供基准。
- `SearchFlight.prepare`：检查飞行高度，接管无人机并升至指定高度。
- `SearchFlight.fly`：沿规划路线飞行，检查到达位置与碰撞情况。
- `SearchFlight.stop`：停止当前飞行，使无人机减速并悬停。

### 2.3 plan_search_path.py

为 `search_area()` 计算区域巡查路线。读取区域边界和道路拓扑，裁剪搜索范围内的道路，为同一道路同一分段选择一条代表车道，再安排巡查顺序和路段间的连接路线。连接路线可以经过搜索范围外，结果覆盖全部必巡路段，允许必要的重复通行。

| 入参 | 类型 | 含义 |
|---|---|---|
| `current_position` | `(x, y)` 或 `[x, y]` | 无人机当前位置，UE 世界 XY 坐标，单位米 |
| `target` | `dict` | 与 `search_area()` 的 `target` 相同，指定区域 ID 和周边宽度 |

| 出参 | 类型 | 含义 |
|---|---|---|
| 返回值 | `dict` | 巡查计划，主要字段如下 |
| `target` | `dict` | 规范化后的区域 ID 和周边宽度 |
| `legs` | `list[dict]` | 按执行顺序排列的航段，包含路点 `points`、类型 `kind`、路段 ID 和长度；`patrol` 为巡查段，`connection` 为连接段 |
| `target_piece_ids` | `list[str]` | 必须完成的巡查路段 ID |
| `distance_m` | `float` | 全部航段总长度，包含接入、连接和重复通行，单位米 |
| `target_distance_m` | `float` | 必巡路段去重后的总长度，单位米 |

入参样例与调用：

```python
from UAV.utils.plan_search_path import plan_search_path

plan = plan_search_path(
    (439.42848, -528.55233),
    {"zone_id": "F-SCHOOL-1", "buffer_m": 50},
)
path_legs = plan["legs"]
distance_m = plan["distance_m"]
```