# UAV 功能说明

`API/` 提供直接面向 AirSim 的功能函数；`utils/`提供需要的子算法（除去`airsim_client.py`外严格与仿真环境剥离，都只是算法）。

## 1 API

### 1.1 fly_to_point

飞往目标点，具体实现细节见 `utils.plan_flight_path` 

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

巡查目标所在完整街区的道路边界和内部道路，具体算法实现见 `utils.plan_search_path`。到达真实拓扑节点并停稳后，用 `bottom_center` 拍摄节点下视照片；进入下一条巡查边前，用 `front_center` 沿该边的进入方向斜下45度拍摄街道照片。

| 入参 | 类型 | 含义 |
|---|---|---|
| `drone_id` | `int` 或 `str` | 无人机编号，`1` 或 `"UAV_01"` |
| `target` | `dict` | 搜索目标，仅需区域 ID；可直接传入规划结果中的 `SearchArea.target` |
| `flight_height` | `float` | 相对起飞地面的飞行高度，单位米，大于 `0`；需满足沿途建筑净空检查 |
| `flight_speed` | `float` | 巡航速度上限，单位米/秒，大于 `0`，默认 `5.0`；拐弯及短路段自动降速 |
| `output_dir` | `str` 或 `Path` | 输出根目录，默认是项目根目录下的 `output/`；每次任务新增连续编号目录，图片及 `frame.json`、`route.json` 保存在其 `capture/` 子目录 |

`target` 字段：

| 字段 | 类型 | 含义 |
|---|---|---|
| `zone_id` | `str` | `Map/zones/functional_zones.json` 中真实的区域 ID，如 `"F-SCHOOL-1"` |

| 出参 | 类型 | 含义 |
|---|---|---|
| 返回值 | `None` | 同步完成巡查、停止拍照、返航与降落后返回，释放无人机任务锁 |

入参样例与调用：

```python
from UAV.API import search_area

target = {
    "zone_id": "F-SCHOOL-1",
}
result = search_area(
    "UAV_01",
    target,
    flight_height=40.0,
    flight_speed=5.0,
)
```

出参样例：

```python
None  # result；巡查结束，已返航并降落
```

输出目录示例：

```text
output/000001/
└─ capture/
   ├─ 000001.jpg
   ├─ 000002.jpg
   ├─ route.json         巡查及返航路径、起飞基点
   └─ frame.json         节点/街道照片及拍摄位置
```

`frame.json` 是按照片编号排列的数组，结构如下：

| 字段 | 含义 |
|---|---|
| `image` | 与 `frame.json` 同目录的图片文件名，如 `"000001.jpg"` |
| `type` | `"node"` 为节点下视照，`"edge"` 为街道照 |
| `position_m` | 实际拍照的 UE 世界坐标 `[X, Y, Z]`，单位米 |
| `node_xy_m` | 对应的真实拓扑节点坐标 |
| `edge_id` | 仅街道照包含，对应 `Map/roads/uav-road-network.json` 的边 ID |

## 2 utils

### 2.1 plan_flight_path.py

完成最短路径与速度分段规划。读取 `Map/roads/uav-road-network.json`。

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
    (432.39000022205096, -529.7028772612439),
    (433.016872697192, -533.5377733913367),
]
segments = [
    ([(439.42848, -528.55233), (435.9092401110255, -529.1276036306219)], 5),
    ([(435.9092401110255, -529.1276036306219), (432.39000022205096, -529.7028772612439)], 2),
    ([(432.39000022205096, -529.7028772612439), (433.016872697192, -533.5377733913367)], 1),
]
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
- `SearchFlight.land`：在起飞基点对位降落，核实落地状态，上锁并释放 API 控制。

### 2.3 plan_search_path.py

为 `search_area()` 计算完整街区巡查路线。读取 `Map/roads/uav-road-network.json` 和 `Map/zones/functional_zones.json`。

| 入参 | 类型 | 含义 |
|---|---|---|
| `current_position` | `(x, y)` 或 `[x, y]` | 无人机当前位置，UE 世界 XY 坐标，单位米；允许位于路网外，先接入最近拓扑边 |
| `target` | `dict` | 搜索目标，仅需 `{"zone_id": "真实区域ID"}`；按目标所在完整街区选择道路 |

| 出参 | 类型 | 含义 |
|---|---|---|
| `segments` | `list[tuple[list[tuple[float, float]], bool]]` | 按飞行顺序排列的 `(XY路点列表, 是否巡查)` |
| `segments[i][0]` | `list[tuple[float, float]]` | 第 `i` 段的世界 XY 路点；保留原始路口和实际弯道 |
| `segments[i][1]` | `bool` | `True` 表示巡查，调用方在真实节点拍照；`False` 表示接入或转场、不拍照 |

入参样例与调用：

```python
from UAV.utils.plan_search_path import plan_search_path

current_position = (2160.0, 20.0)
target = {"zone_id": "F-SCHOOL-1"}
segments = plan_search_path(current_position, target)
```

出参样例：

```python
segments = [
    (
        [
            (2160.0, 20.0),
            (2168.486184631315, 21.48710305451246),
            (2172.1857483712365, 0.37546532569096563),
        ],
        False,  # 接入街区道路，不拍照
    ),
    (
        [
            (2172.1857483712365, 0.37546532569096563),
            (2402.742619918021, 37.79429523021426),
            (2382.999723054658, 150.9552078200182),
            (2152.310450095543, 113.7942699325421),
            (2172.1857483712365, 0.37546532569096563),
        ],
        True,  # 巡查整片街区，在真实节点停稳拍照
    ),
]
```
