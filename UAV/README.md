# UAV 功能说明

分为`API`和`utils`两部分，其中`API`提供功能函数直接面向`airsim`仿真环境，调用直接对应仿真无人机的具体动作，`utils`为`API`的功能函数实现提供具体的算法细节（如`shortest_path.py`为封装好的计算最短路径的函数）

## 1 API

### 1.1 fly_to_point

调用 `utils.find_shortest_path`，自动起飞、升至指定高度并沿道路拓扑飞往目标，完成后悬停。

| 入参 | 类型 | 含义 |
|---|---|---|
| `drone_id` | `int` 或 `str` | 无人机编号，`1` 或 `"UAV_01"` |
| `target_position` | `(x, y)` 或 `[x, y]` | 道路图边上的目标位置，UE 世界坐标，单位米 |
| `flight_height` | `float` | 相对起飞地面的飞行高度，单位米，大于 `0`；航程中保持此高度平面 |
| `flight_speed` | `float` | 沿路径飞行的速度，单位米/秒，大于 `0`，默认 `25.0` |

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

停止飞行并将无人机恢复到初始位置与姿态，上锁并释放 API 控制权。指定编号时仅复位该无人机，不传编号时使用 AirSim 内置方法复位全部无人机。

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

### shortest_path.py

计算无人机当前位置到道路拓扑图边上目标点的最短路径。起点不在图上时，先投影到最近边，再使用 Dijkstra 算法沿拓扑图双向规划。

| 入参 | 类型 | 含义 |
|---|---|---|
| `current_position` | `(x, y)` 或 `[x, y]` | 无人机当前位置，允许不在拓扑图上 |
| `target_position` | `(x, y)` 或 `[x, y]` | 道路拓扑图任意边上的目标位置，不限于节点 |

坐标统一使用 `Map/road_network.json` 的 UE 世界 XY 坐标，单位为米。

| 出参 | 类型 | 含义 |
|---|---|---|
| `path` | `list[tuple[float, float]]` | 按飞行顺序排列的路径点；首点为当前位置，末点为目标位置，包含接入点及沿途道路折线点 |

入参样例与调用：

```python
from UAV.utils import find_shortest_path

current_position = (439.42848, -528.55233)
target_position = (440.05435, -532.38739)
path = find_shortest_path(current_position, target_position)
```

出参样例：

```python
[
    (439.42848, -528.55233),
    (440.05435, -532.38739),
]
```

函数内部自动读取道路拓扑图，路径直接作为运行时变量返回，不保存文件。仅输出 XY 路径，飞行高度由调用方设置。

### airsim_client.py

提供 AirSim 通信、UE 世界坐标转换与标定、起飞地面高度测量，以及共用的无人机编号解析和任务锁。

### reset_state.py

读取 AirSim 配置中的初始位置与姿态，并清理起飞地面基准，供复位 API 使用。
