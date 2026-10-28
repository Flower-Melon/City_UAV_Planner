# UAV 功能说明

`API/` 提供直接面向仿真环境的无人机飞行接口，直接运行即可完成一类任务；`runtime/` 负责 AirSim 通信、状态读取与飞行执行；`utils/` 提供不依赖仿真环境的算法。

## 1 API

### 1.1 fly_to_point

根据分层飞行路网飞往指定世界XYZ，完成后悬停。规划包含绕楼、跨楼、升降和速度调整。

| 入参 | 类型 | 含义 |
|---|---|---|
| `drone_id` | `int` 或 `str` | 无人机编号，`1` 或 `"UAV_01"` |
| `target_position` | `(x, y, z)` 或 `[x, y, z]` | 目标UE世界坐标，单位米；Z为世界高度，终点为指定位置 |
| `max_height` | `float` | 导航最大离地高度，单位米，至少15米；相对沿途当地地面 |
| `flight_speed` | `float` | 巡航速度上限，单位米/秒，大于 `0`，默认 `25.0`；拐弯及接近终点时自动降速 |

| 出参 | 类型 | 含义 |
|---|---|---|
| 返回值 | `None` | 同步完成飞行并悬停后返回，无路径或位置数据返回 |

入参样例与调用：

```python
from UAV.API import fly_to_point

drone_id = "UAV_01"
target_position = (2123.25, -2288.2970827809468, 380.0)
max_height = 60.0
flight_speed = 8.0
result = fly_to_point(drone_id, target_position, max_height, flight_speed)
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
| `flight_height` | `float` | 相对起飞地面的固定巡查高度，单位米，大于 `0`；需满足巡查道路的建筑净空检查 |
| `flight_speed` | `float` | 巡航速度上限，单位米/秒，大于 `0`，默认 `5.0`；拐弯及短路段自动降速 |
| `max_height` | `float` | 去程和返航的最大当地离地高度，默认60米，至少15米；仅限关键字传入 |
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
    max_height=60.0,
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
   ├─ route.json         导航/巡查航段、返航路径及起飞基点
   └─ frame.json         节点/街道照片及拍摄位置
```

`frame.json` 是按照片编号排列的数组，结构如下：

| 字段 | 含义 |
|---|---|
| `image` | 与 `frame.json` 同目录的图片文件名，如 `"000001.jpg"` |
| `type` | `"node"` 为节点下视照，`"edge"` 为街道照 |
| `position_m` | 实际拍照的 UE 世界坐标 `[X, Y, Z]`，单位米 |
| `node_xy_m` | 对应的真实拓扑节点坐标 |
| `edge_id` | 仅街道照包含，对应 `Map/roads/uav_road_network.json` 的边 ID |

## 2 utils

### 2.1 plan_flight_path.py

读取 `Map/roads/uav_flight_network.json`，按最大离地高度计算导航路线，并标注升降、转弯、窄通道和到站速度。起终点为精确世界XYZ；航段长度由XY折线计算，地面高度按沿边采样插值。寻路代价由水平距离和升降代价组成，不承诺飞行时间最短。

| 入参 | 类型 | 含义 |
|---|---|---|
| `current_position` | `(x, y, z)` | 实际UE世界位置，单位米 |
| `target_position` | `(x, y, z)` | 目标UE世界位置，单位米 |
| `max_height` | `float` | 沿途最大离地高度，至少15米；导航在不超过该值的高度层中选择 |
| `flight_speed` | `float` | 速度上限，单位米/秒，大于0，默认25.0 |

| 出参 | 类型 | 含义 |
|---|---|---|
| `route` | `list[tuple[float, float, float, float]]` | 按飞行顺序排列的 `(X, Y, Z, 到达该点的速度)`，包含精确起终点 |

```python
from UAV.utils import plan_flight_path

route = plan_flight_path(
    (2120.0, -2297.0, 360.0),
    (1727.7, -1940.2, 383.0),
    max_height=60.0,
    flight_speed=8.0,
)
```

输出格式示例（坐标仅作示意）：

```python
[
    (100.0, 200.0, 355.0, 2.0),
    (100.0, 200.0, 395.0, 2.0),
    (150.0, 200.0, 395.0, 3.0),
    (150.0, 200.0, 375.0, 1.0),
]
```

### 2.2 plan_search_path.py

为 `search_area()` 生成导航接入与完整街区巡查路线。导航读取飞行路网，巡查读取 `Map/roads/uav_road_network.json` 和 `Map/zones/functional_zones.json`；不连接AirSim或保存文件。

| 入参 | 类型 | 含义 |
|---|---|---|
| `current_position` | `(x, y, z)` | 无人机实际UE世界位置，单位米 |
| `target` | `dict` | `{"zone_id": "真实区域ID"}`，选择目标所在完整街区的道路 |
| `search_height` | `float` | 巡查的固定世界Z，单位米，由调用方按起飞地面和巡查高度计算 |
| `max_height` | `float` | 首次导航的最大当地离地高度，单位米，至少15米 |
| `flight_speed` | `float` | 速度上限，单位米/秒，大于0，默认5.0 |

| 出参 | 类型 | 含义 |
|---|---|---|
| `segments` | `list[tuple[list[tuple[float, float, float, float]], bool]]` | 按执行顺序排列的 `(航点列表, 是否巡查)` |
| `segments[i][0]` | `list` | 世界XYZ和到达速度；巡查段保留完整原道路和真实节点 |
| `segments[i][1]` | `bool` | `True`按真实节点和道路边拍照；`False`表示导航或转场，不拍照 |

```python
from UAV.utils.plan_search_path import plan_search_path

segments = plan_search_path(
    (2120.0, -2297.0, 360.0),
    {"zone_id": "F-SCHOOL-1"},
    search_height=399.0,
    max_height=60.0,
    flight_speed=5.0,
)
```

输出格式示例（坐标仅作示意）：

```python
[
    ([(100.0, 200.0, 355.0, 2.0), (100.0, 200.0, 375.0, 1.0)], False),
    ([(100.0, 200.0, 375.0, 2.0), (150.0, 200.0, 375.0, 1.0)], True),
]
```

## 3 runtime

供无人机 API 和网页后端使用的仿真运行支持。

| 文件 | 大体功能 |
|---|---|
| `airsim_client.py` | 连接 AirSim，发送请求并接收结果 |
| `airsim_state.py` | 读取机队状态、解析无人机名称，完成坐标标定与地面测量 |
| `flight_control.py` | 按航点速度执行三维路径，原地升降到高后继续飞行，核对位置和碰撞，管理任务锁与降落 |
