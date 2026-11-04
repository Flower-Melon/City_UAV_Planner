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

巡查目标所在完整街区的道路边界和内部道路，路径规划的算法实现见 `utils.plan_search_path`。

具体选择哪架或者哪几架无人机执行搜索的逻辑见 `utils.select_UAV`，根据区域和 AirSim 实时状态自动选择并占用一架无人机。

巡查途中按道路折线的累计前进里程，用 `bottom_center` 触发下视拍照，默认每15米一次，可通过 `capture_interval_m` 调整。搜索期间，下视 RGB 相机默认不渲染树木。

| 入参 | 类型 | 含义 |
|---|---|---|
| `target` | `dict` | 搜索目标，仅需区域 ID；可直接传入规划结果中的 `SearchArea.target` |
| `flight_height` | `float` | 相对起飞地面的固定巡查高度，单位米，大于 `0`；需满足巡查道路的建筑净空检查 |
| `flight_speed` | `float` | 巡航速度上限，单位米/秒，大于 `0`，默认 `30.0`；拐弯及短路段自动降速 |
| `max_height` | `float` | 去程和返航的最大当地离地高度，默认60米，至少15米；仅限关键字传入 |
| `capture_interval_m` | `float` | 巡查途中下视拍照的累计里程间距，单位米，大于0且有限，默认15.0；仅限关键字传入 |
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
   └─ frame.json         道路巡查照片及拍摄位置
```

单独运行 `python UAV/API/search_area.py` 时，输出到 `output/temp/编号/capture/`，如 `output/temp/000001/capture/`。

`frame.json` 是按照片编号排列的数组，结构如下：

| 字段 | 含义 |
|---|---|
| `image` | 与 `frame.json` 同目录的图片文件名，如 `"000001.jpg"` |
| `position_m` | UE 世界坐标 `[X, Y, Z]`，单位米 |
| `edge_id` | 拍摄位置所在道路，对应 `Map/roads/uav_road_network.json` 的边 ID |

### 1.4 deliver_medicine

在当前位置上方挂载药箱，调用 `utils.plan_flight_path` 按分层飞行路网规划最短代价路线，飞至地面投递点上方15米。低速对位并悬停后，下放药箱、着地脱钩并收绳，全部完成后返回，无人机留在落点上空悬停。

| 入参 | 类型 | 含义 |
|---|---|---|
| `drone_id` | `int` 或 `str` | 无人机编号，`1` 或 `"UAV_01"` |
| `target_position` | `(x, y, z)` 或 `[x, y, z]` | 药箱落点的UE世界坐标，单位米；**Z为地面世界高度**，脚本自动加15米作为无人机悬停高度；落点应选择开阔地面 |
| `max_height` | `float` | 导航最大离地高度，单位米，至少15米；相对沿途当地地面 |
| `flight_speed` | `float` | 巡航速度上限，单位米/秒，大于 `0`，默认 `25.0`；拐弯及接近终点时自动降速 |

| 出参 | 类型 | 含义 |
|---|---|---|
| 返回值 | `None` | 同步完成投递及收绳后返回，保留API控制权，不自动返航或降落 |

入参样例与调用：

```python
from UAV.API import deliver_medicine

drone_id = "UAV_01"
target_position = (2270.4, 121.3, 368.0)
max_height = 60.0
flight_speed = 8.0
result = deliver_medicine(drone_id, target_position, max_height, flight_speed)
```

出参样例：

```python
None  # result；药箱已着地并收绳，无人机在落点上空悬停
```

### 1.5 monitor_area

下视 RGB 相机通过 `runtime.kill_trees` 隐藏树木；到位悬停并返回后仍保留设置，供后续监控拍照使用。

飞往目标区域几何中心的正上方并悬停。

| 入参 | 类型 | 含义 |
|---|---|---|
| `zone_id` | `str` | `Map/zones/functional_zones.json` 中真实的区域ID，如 `"F-SCHOOL-1"` |
| `drone_id` | `int`、`str` 或 `None` | 无人机编号或名称，默认 `None`，从当前可用无人机中自动选取一架；指定ID时可将已悬停的监控机转往下一区域 |
| `flight_height` | `float` | 相对无人机起飞原点的高度，单位米，大于0，默认 `200.0`，默认值在 `monitor_area.py` 中设置 |

| 出参 | 类型 | 含义 |
|---|---|---|
| 返回值 | `None` | 同步完成飞行和悬停后返回，不自动返航或降落 |

入参样例与调用：

```python
from UAV.API import monitor_area

monitor_area("F-SCHOOL-1")
monitor_area("F-SCHOOL-2", drone_id="UAV_02", flight_height=200.0)
```

出参样例：

```python
None  # 无人机已到区域中心上空悬停
```

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
| `segments[i][1]` | `bool` | `True`按累计巡查里程拍照；`False`表示导航或转场，不拍照 |

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

### 2.3 select_UAV.py

根据目标区域选择一架可用无人机。读取 `Map/zones/functional_zones.json` 中的区域边界，以及 `AirSim` 中无人机的状态。

| 入参 | 类型 | 含义 |
|---|---|---|
| `zone_id` | `str` | `Map/zones/functional_zones.json` 中真实的区域ID，如 `"F-SCHOOL-1"` |

| 出参 | 类型 | 含义 |
|---|---|---|
| `drone_id` | `str` | 选中的无人机名称，如 `"UAV_02"`；每次返回一架 |

```python
from UAV.utils.select_UAV import select_UAV

drone_id = select_UAV("F-SCHOOL-1")
```

输出格式示例（实际结果由当前无人机状态决定）：

```python
"UAV_02"
```

## 3 runtime

供无人机 API 和网页后端使用的仿真运行支持。

| 文件 | 大体功能 |
|---|---|
| `airsim_client.py` | 连接 AirSim，发送请求并接收结果 |
| `airsim_state.py` | 读取机队状态、解析无人机名称，完成坐标标定与地面测量 |
| `kill_trees.py` | 巡查和监控无人机的下视 RGB 相机隐藏树木组件 |
| `flight_control.py` | 按航点速度执行三维路径，原地升降到高后继续飞行，核对位置和碰撞，管理跨线程/进程的任务锁与降落；进程退出后自动释放占用 |
