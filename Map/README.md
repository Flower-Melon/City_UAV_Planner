# Map 数据说明

```text
Map/
├─ README.md
├─ roads/                     道路与飞行拓扑
│  ├─ road_network.json       原始车道级路网
│  ├─ uav_road_network.json   无人机二维道路导航与搜索路网
│  └─ uav_flight_network.json 无人机分层飞行导航路网
├─ objects/                   建筑与任务点
│  ├─ buildings.json
│  └─ task_points.json
└─ zones/                     功能区与模型目录
   ├─ functional_zones.json
   ├─ zone_catalog.json
   └─ generate_catalog.py     将功能区数据转换为模型目录
```

## roads/road_network.json

保存车道、节点及连接线，供车辆按车道方向、无人机沿拓扑边双向规划最短路径。

```json
{
  "coordinate_metadata": {
    "system": "UE_WORLD_METRES",
    "unit": "m",
    "bounds_m": [-1000, -3000, 3000, 1000]
  },
  "routing_rules": {
    "distance_metric": "xy",
    "vehicle": {
      "lane_types": ["driving"],
      "direction": "forward",
      "connection_requires_both_lanes": true
    },
    "uav": {
      "direction": "both"
    }
  },
  "lanes": [],
  "nodes": [],
  "edges": []
}
```

| 字段 | 含义 |
|---|---|
| `coordinate_metadata` | 坐标系、单位和地图范围 `[xmin, ymin, xmax, ymax]` |
| `routing_rules.distance_metric` | 按 XY 平面长度计算路径成本 |
| `routing_rules.vehicle` | 仅用机动车道；按起点到终点通行；连接两端须为机动车道 |
| `routing_rules.uav` | 沿图边双向通行 |
| `lanes[].id`、`type` | 车道编号及类型：机动车、人行、停车、自行车 |
| `nodes[].id`、`position_m` | 节点编号及 `[X, Y, Z]`；未知 Z 为 `null` |
| `edges[].id`、`kind` | 边编号及类型：`road` 道路、`connection` 连接 |
| `edges[].from_node_id`、`to_node_id` | 起止节点编号 |
| `edges[].lane_id` | 道路边所属车道，引用 `lanes[].id` |
| `edges[].polyline_xy_m` | 完整 XY 折线，反向通行时反转坐标顺序 |
| `edges[].length_m` | 折线长度，道路与连接均计入路径成本 |

## roads/uav_road_network.json

保存无人机可双向通行的二维拓扑，当前包含 574 个节点、880 条边。导航按边的 XY 长度寻路；搜索根据闭合道路识别目标关联的完整街区，覆盖其边界和内部道路，选中后保留完整原始边，不按距离范围截断。

```json
{
  "coordinate_system": "UE_WORLD_XY_METRES",
  "nodes": [
    {
      "id": "N1",
      "position_xy_m": [0, 0]
    },
    {
      "id": "N2",
      "position_xy_m": [10, 0]
    }
  ],
  "edges": [
    {
      "id": "E1",
      "from_node_id": "N1",
      "to_node_id": "N2",
      "polyline_xy_m": [
        [0, 0],
        [10, 0]
      ]
    }
  ]
}
```

| 字段 | 含义 |
|---|---|
| `coordinate_system` | UE 世界 XY 坐标，单位为米 |
| `nodes[].id` | 节点唯一编号 |
| `nodes[].position_xy_m` | 节点位置 `[X, Y]` |
| `edges[].id` | 边唯一编号，可用于标识巡查路段和记录搜索进度 |
| `edges[].from_node_id`、`to_node_id` | 起止节点编号，引用 `nodes[].id` |
| `edges[].polyline_xy_m` | 从起点到终点的 XY 路径；直路仅包含两个端点，实际弯道保留必要的转向点 |

## roads/uav_flight_network.json

保存无人机按离地高度选择航线的路网，包含 10,394 个节点、47,712 条双向路段。路段包括道路、楼间通道和跨街区航线，可用于绕过高楼或飞越低楼。

```json
{
  "coordinate_system": "UE_WORLD_METRES",
  "altitude_rules": {
    "layers_agl_m": [15, 20, 25, 30, 40, 50, 60, 90, 120],
    "vertical_margin_m": 5,
    "drone_radius_m": 0.5,
    "gap_margin_m": 1,
    "cruise_margin_m": 2
  },
  "nodes": [
    {
      "id": "N1",
      "position_m": [0, 0, 355]
    },
    {
      "id": "N2",
      "position_m": [10, 0, 356]
    }
  ],
  "edges": [
    {
      "id": "E1",
      "kind": "road",
      "from_node_id": "N1",
      "to_node_id": "N2",
      "polyline_xy_m": [
        [0, 0],
        [10, 0]
      ],
      "min_altitude_agl_m": 15,
      "min_passage_width_m": 8,
      "ground_profile_m": [
        [0, 355],
        [10, 356]
      ]
    }
  ]
}
```

| 字段 | 含义 |
|---|---|
| `coordinate_system` | UE 世界坐标，单位米，Z 轴向上 |
| `altitude_rules.layers_agl_m` | 可选的离地高度，单位米；离地高度指无人机与当前位置地面的高度差 |
| `altitude_rules.vertical_margin_m` | 生成路网时，航线在楼顶上方预留的高度，单位米 |
| `altitude_rules.drone_radius_m` | 生成路网时使用的无人机半径，单位米 |
| `altitude_rules.gap_margin_m` | 穿过楼间通道时，机身与每侧建筑之间预留的距离，单位米 |
| `altitude_rules.cruise_margin_m` | 普通飞行时，机身与两侧建筑之间各预留的距离，单位米 |
| `nodes[].id` | 节点唯一编号 |
| `nodes[].position_m` | 节点位置 `[X, Y, 地面Z]`，单位米；地面Z是该位置地面的世界高度 |
| `edges[].id` | 边唯一编号 |
| `edges[].kind` | 路段类型，取值见下表 |
| `edges[].from_node_id`、`to_node_id` | 起止节点编号，引用 `nodes[].id` |
| `edges[].polyline_xy_m` | 从起点到终点的 XY 路径，单位米；路段长度由相邻坐标间的距离累加得到 |
| `edges[].min_altitude_agl_m` | 通过该路段所需的最低离地高度，单位米 |
| `edges[].min_passage_width_m` | 在最低离地高度下，该路段最窄处的通道宽度，单位米 |
| `edges[].ground_profile_m` | 沿路段采样的 `[距起点距离, 地面Z]` 数组，单位米，按距离递增排列 |

`kind` 取值：

| 值 | 含义 |
|---|---|
| `road` | 沿道路飞行 |
| `door_access` | 从道路进入楼间通道 |
| `door_pass` | 穿过楼间通道 |
| `interior` | 在街区内部飞行 |
| `cross_block` | 跨街区飞行 |

## zones/functional_zones.json

保存功能区和设施的边界、用途及关联对象，用于区域查询和地图展示。

```json
{
  "coordinate_metadata": {
    "system": "UE_WORLD_METRES",
    "unit": "m",
    "bounds_m": [-1000, -3000, 3000, 1000]
  },
  "categories": [],
  "features": []
}
```

| 字段 | 含义 |
|---|---|
| `categories` | 用途类别的代码、名称、子类及显示颜色 |
| `features[].id`、`geometry` | 区域或设施编号及边界 |
| `properties.name` | 名称 |
| `properties.object_type` | `region` 功能区、`facility` 设施 |
| `properties.category`、`subtype`、`environment` | 用途类别、子类及环境 |
| `properties.area_m2`、`label_xy_m` | 面积及标签位置 |
| `properties.parent_id`、`host_region_id` | 父区域及设施所在区域 |
| `properties.building_ids`、`point_ids`、`facility_ids` | 关联建筑、点位和设施编号 |
| `properties.nearby_graph_edge_ids` | 15 米内的道路边编号，仅表示空间邻近 |

## zones/zone_catalog.json

保存供规划模型读取的静态区域目录，包含全部功能区和设施的 ID、名称、类别及子类，不包含几何坐标。

| 字段 | 含义 |
|---|---|
| `zone_fields` | `zones` 每行的字段顺序：`zone_id`、`name`、`category`、`subtype` |
| `zones` | 全部区域及设施的目录，如 `["F-SCHOOL-1", "第一学校", "education", "中学"]` |
| `categories` | 类别代码、名称、子类及对象数量 |
| `facility_ids` | 默认发送给模型的设施 ID；其他区域按任务文字筛选 |
| `total_objects` | 目录对象总数 |
| `direction_convention` | 地图方向约定 |

`planner.py` 直接读取此文件；真实边界及空间查询仍使用 `functional_zones.json`。

同目录的 `generate_catalog.py` 根据 `functional_zones.json` 生成此目录，在项目根目录运行：

```powershell
python Map/zones/generate_catalog.py
```

## objects/task_points.json

保存任务点的位置、用途和道路接入候选，用于指定目标点及接入道路图。

```json
{
  "coordinate_metadata": {
    "system": "UE_WORLD_METRES",
    "unit": "m",
    "bounds_m": [-1000, -3000, 3000, 1000]
  },
  "features": []
}
```

| 字段 | 含义 |
|---|---|
| `features[].id`、`geometry` | 点位编号及 XY 位置 |
| `properties.name`、`point_type` | 名称及用途 |
| `properties.region_id` | 所属区域或设施编号 |
| `properties.xyz_m` | 世界位置 `[X, Y, Z]` |
| `properties.position_status`、`execution_enabled` | 静态核验状态及启用标记 |
| `properties.clearance_radius_m`、`required_clearance_height_m` | 净空半径及高度要求 |
| `properties.primary_point_id` | 备用点对应的主点编号 |
| `properties.road_access_candidates` | `vehicle`、`uav` 各 3 个接入候选，不保证可达 |
| `road_access_candidates.*[].edge_id`、`offset_m` | 道路边编号及沿边起点累计的投影距离 |
| `road_access_candidates.*[].xy_m`、`distance_m` | 投影位置及其到原点位的平面距离 |

## objects/buildings.json

保存建筑轮廓、用途及高度范围，用于建筑展示和空间位置参考。

```json
{
  "coordinate_system": "UE_WORLD_XY_METRES",
  "features": []
}
```

| 字段 | 含义 |
|---|---|
| `coordinate_system` | UE 世界 XY 坐标，单位为米 |
| `features[].id`、`geometry` | 建筑编号及轮廓 |
| `properties.category`、`parent_id` | 用途分类及所属区域 |
| `properties.z_min_m`、`z_max_m` | 最低和最高世界 Z 值 |

