# Map 数据说明

```text
Map/
├─ README.md
├─ road_network.json          道路拓扑
├─ objects/                   建筑与任务点
│  ├─ buildings.json
│  └─ task_points.json
└─ zones/                     功能区与模型目录
   ├─ functional_zones.json
   ├─ zone_catalog.json
   └─ generate_catalog.py     将功能区数据转换为模型目录
```

## road_network.json

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

