# UAV 功能说明

`API/` 提供直接面向仿真环境的无人机飞行接口，直接运行即可完成一类任务；`runtime/` 负责 AirSim 通信、状态读取与飞行执行；`utils/` 提供不依赖仿真环境的算法。

## 1 API

### 1.1 fly_to_point

根据分层飞行路网飞往指定世界XYZ，完成后在目标附近悬停。规划包含绕楼、跨楼、升降和速度调整；执行时连续向前跟随轨迹，已越过的采样航点直接跳过，不折返对位。

| 入参 | 类型 | 含义 |
|---|---|---|
| `drone_id` | `int` 或 `str` | 无人机编号，`1` 或 `"UAV_01"` |
| `target_position` | `(x, y, z)` 或 `[x, y, z]` | 目标UE世界坐标，单位米；Z为世界高度，横向轨迹偏差容许5米、高度偏差容许1.5米，沿前进方向过点不回拉 |
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

巡查目标所在完整街区的道路边界和内部道路，具体算法实现见 `utils.plan_search_path`。在真实拓扑节点附近（5米内）短暂制动后，用 `bottom_center` 拍摄节点下视照片；进入下一条巡查边前，用 `front_center` 沿该边的进入方向斜下45度拍摄街道照片。不要求精确到点或满足极低的停稳速度。

巡查途中按道路折线的累计前进里程，用 `bottom_center` 触发下视拍照，默认每20米一次，可通过 `capture_interval_m` 调整。弯道和跨节点继续累计；导航、转场与返航不计入。重复经过巡查边仍按新增里程采样，不受入口照片的边去重影响。取图、编码及写盘在独立线程串行执行，每条边结束时等待在途照片保存，再拍节点和入口照片。

里程触发使用飞行控制的遥测进度，最多同时处理一张路中照片，不积压过期拍照请求。单次遥测跨过多个间距时只尝试最新位置；相机忙、已错过采样位置或端点提前制动造成的缺口记录为 `events.json` 中的 `capture_skipped`（`trigger="distance"`）。这不是精确等距曝光的保证，需根据实际取图耗时、巡查高度和视场角调整间距与速度。

飞行执行每0.2秒根据实际位置沿前方折线更新速度，进度只向前推进。减速时计入实测约3秒的速度响应及惯性滑行距离。中间采样点不停车、不逐点校正；沿前进方向越过端点后继续接入后续路段，只在横向偏离轨迹超过5米时停止，不把沿线过冲当作必须回拉的端点误差。小于0.5米的高度变化不触发原地制动。已经错过的拍照节点记录为 `capture_skipped` 并继续，不折返补拍。返航在基点5米内直接降落，不再水平回拉对位。原地升降、碰撞/失控和降落仍保留必要的停止与检查。覆盖算法要求的重复路段及正常返航仍按规划执行。

发生飞行异常时，任务目录的 `events.json` 保存错误信息、最近一次位置、速度、前进里程及终点的横向/高度误差，便于定位仿真中的实际响应。

| 入参 | 类型 | 含义 |
|---|---|---|
| `drone_id` | `int` 或 `str` | 无人机编号，`1` 或 `"UAV_01"` |
| `target` | `dict` | 搜索目标，仅需区域 ID；可直接传入规划结果中的 `SearchArea.target` |
| `flight_height` | `float` | 相对起飞地面的固定巡查高度，单位米，大于 `0`；需满足巡查道路的建筑净空检查 |
| `flight_speed` | `float` | 巡航速度上限，单位米/秒，大于 `0`，默认 `30.0`；拐弯及短路段自动降速 |
| `max_height` | `float` | 去程和返航的最大当地离地高度，默认60米，至少15米；仅限关键字传入 |
| `capture_interval_m` | `float` | 巡查途中下视拍照的累计里程间距，单位米，大于0且有限，默认20.0；仅限关键字传入 |
| `output_dir` | `str` 或 `Path` | 输出根目录，默认是项目根目录下的 `output/`；每次任务新增连续编号目录，图片及 `frame.json`、`route.json` 保存在其 `capture/` 子目录 |
| `on_capture_directory` | 可调用对象或 `None` | 可选关键字参数；照片目录创建后回调一次，接收实际 `capture/` 目录的绝对 `Path`，用于网页实时识别接入；默认 `None` |

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
    flight_speed=30.0,
    max_height=60.0,
    capture_interval_m=20.0,
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
   ├─ route.json         导航/巡查航段、返航路径、起飞基点及拍照间距
   └─ frame.json         节点/街道入口/路中照片及拍摄位置
```

`frame.json` 是按照片编号排列的数组，结构如下：

| 字段 | 含义 |
|---|---|
| `image` | 与 `frame.json` 同目录的图片文件名，如 `"000001.jpg"` |
| `type` | `"node"` 为节点下视照，`"edge"` 为街道入口斜视照，`"distance"` 为巡查途中下视照 |
| `position_m` | UE 世界坐标 `[X, Y, Z]`，单位米；路中照使用图像响应自带的相机曝光位置，其余照片沿用取图后无人机遥测位置 |
| `node_xy_m` | 节点照和入口照包含，对应真实拓扑节点坐标 |
| `edge_id` | 入口照和路中照包含，对应 `Map/roads/uav_road_network.json` 的边 ID |
| `sample_xy_m` | 仅路中照包含，计划触发位置的道路XY坐标 |
| `patrol_distance_m` | 仅路中照包含，计划触发位置对应的任务累计巡查里程，不含转场与返航 |
| `edge_distance_m` | 仅路中照包含，从本次进入道路边的位置起沿折线计算的触发里程 |
| `timestamp_ns` | 仅路中照包含，图像响应的纳秒时间戳 |
| `camera_orientation` | 仅路中照包含，图像响应的相机NED姿态四元数 |

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
| `flight_speed` | `float` | 速度上限，单位米/秒，大于0，默认30.0 |

| 出参 | 类型 | 含义 |
|---|---|---|
| `segments` | `list[tuple[list[tuple[float, float, float, float]], bool]]` | 按执行顺序排列的 `(航点列表, 是否巡查)` |
| `segments[i][0]` | `list` | 世界XYZ和到达速度；巡查段保留完整原道路和真实节点 |
| `segments[i][1]` | `bool` | `True`按真实节点、道路入口及累计巡查里程拍照；`False`表示导航或转场，不拍照 |

```python
from UAV.utils.plan_search_path import plan_search_path

segments = plan_search_path(
    (2120.0, -2297.0, 360.0),
    {"zone_id": "F-SCHOOL-1"},
    search_height=399.0,
    max_height=60.0,
    flight_speed=30.0,
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

水平飞行使用短时速度指令连续跟踪前方路径，并检查建筑净空、API控制权和碰撞。原地升降仍使用高度接口；即使接口返回 `False`，也只在水平漂移不超过5米、高度误差不超过0.75米、控制权有效且没有新碰撞时继续，否则终止并报告实测误差。巡查和拍照不再执行精确位置回拉或严格停稳校验。
