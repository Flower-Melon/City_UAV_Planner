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

提供 AirSim 通信、UE 世界坐标转换与标定、起飞地面高度测量，以及共用的无人机编号解析和任务锁。

## 3 沿路搜索 search

新增`from UAV.API import search`，同步完成指定区域或周边的道路巡查和定时拍照，完成后悬停并释放任务锁；图片识别与报告在后台继续，不阻塞下一次飞行。接口参数、目录结构、提示词及测试说明见[SEARCH.md](SEARCH.md)。网页目前仍只规划，没有自动执行。
