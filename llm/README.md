# llm 功能说明

`planner.py` 将自然语言任务转换为区域搜索计划，`vision.py` 分析本地图片。`utils/` 提供模型连接和地图查询，`prompts/` 保存两类模型的判断规则。

## 1 主要接口

### 1.1 planner

根据任务描述、无人机状态和真实地图，确定搜索区域、分配无人机并生成搜索指令。

先创建 `Planner(client, model, catalog)`：

| 初始化入参 | 类型 | 含义 |
|---|---|---|
| `client` | 模型客户端 | 由 `create_client()` 创建，负责调用模型 |
| `model` | `str` | 模型名称，由 `create_client()` 返回 |
| `catalog` | `ZoneCatalog` | 地图查询对象，通常使用 `ZoneCatalog()` |

提示词固定读取 `llm/prompts/search_area.txt`。

区域目录固定读取 `Map/zones/zone_catalog.json`，按当前任务筛选后发送给模型；区域确认和空间查询由 `ZoneCatalog` 完成。

再调用 `plan(instruction, fleet, conversation=None)`：

| 入参 | 类型 | 含义 |
|---|---|---|
| `instruction` | `str` | 自然语言任务，1–4000 字，如“让 UAV_01 完整巡查第一学校所在街区” |
| `fleet` | `list[dict]` | 调用方提供的非空无人机列表，字段见下表 |
| `conversation` | `list[dict]` 或 `None` | 可选历史问答，默认 `None`；每条含 `role` 和 `content`，按 `user`、`assistant` 成对排列，最多 6 组 |

`fleet` 中每架无人机的字段：

| 字段 | 类型 | 含义 |
|---|---|---|
| `name` | `str` | 无人机名称，如 `"UAV_01"`，不能重复 |
| `available` | `bool` | 当前是否可接受任务；只有 `True` 的无人机会被分配 |
| `position_m` | `[x, y, z]` 或 `None` | UE 世界坐标，单位米；未知时填 `None` |

历史问答与本次任务的总长度不超过 16000 字。以无人机位置作为选区参照时，需提供有效的 `position_m`。

| 出参 | 类型 | 含义 |
|---|---|---|
| 返回值 | `dict` | 搜索规划，包含以下三个字段 |
| `status` | `str` | `ready` 表示已生成可执行计划；`needs_clarification` 表示需要补充信息或重新规划 |
| `commands` | `list[dict]` | 搜索指令列表；未生成有效计划时为空 |
| `message` | `str` | 规划说明或需要用户补充的信息 |

`commands` 中每条搜索指令的字段：

| 字段 | 类型 | 含义 |
|---|---|---|
| `drone_id` | `str` | 执行该区域任务的无人机名称 |
| `sequence` | `int` | 同一架无人机的任务顺序，从 `1` 开始 |
| `target` | `dict` | 搜索目标，仅包含区域 ID |
| `target.zone_id` | `str` | 地图中的真实区域 ID |

`target` 可直接传给 `UAV.API.search_area()`。实际搜索按目标所在完整街区的道路覆盖执行，不设置缓冲宽度；查询中的 `radius_m` 仍仅用于筛选目标区域，不用于裁剪巡查道路。

入参样例与调用：

```python
from llm.utils.catalog import ZoneCatalog
from llm.utils.client import create_client
from llm.planner import Planner

fleet = [
    {"name": "UAV_01", "available": True, "position_m": None},
]
client, model = create_client()
with client:
    result = Planner(client, model, ZoneCatalog()).plan(
        "让 UAV_01 完整巡查第一学校所在街区",
        fleet,
    )
```

出参样例：

```python
{
    "status": "ready",
    "message": "已规划第一学校所在街区的完整巡查任务。",
    "commands": [
        {
            "drone_id": "UAV_01",
            "sequence": 1,
            "target": {"zone_id": "F-SCHOOL-1"},
        },
    ],
}
```

### 1.2 vision

分析一张本地图片，判断是否存在需要紧急医疗救援的病人、疑似持刀危险人员或可疑车辆。

先创建 `VisionAnalyzer(client=None, model=None)`：

| 初始化入参 | 类型 | 含义 |
|---|---|---|
| `client` | 模型客户端或 `None` | 默认 `None`，自动读取配置创建客户端 |
| `model` | `str` 或 `None` | 默认读取视觉模型配置；传入外部 `client` 时必须指定模型名称 |

提示词固定读取 `llm/prompts/search_vision.txt`。

再调用 `analyzer(image_path)`：

| 入参 | 类型 | 含义 |
|---|---|---|
| `image_path` | `str` 或 `Path` | 本地 JPEG 或 PNG 图片路径 |

| 出参 | 类型 | 含义 |
|---|---|---|
| 返回值 | `dict` | 单张图片的判断结果 |
| `status` | `str` | `normal` 正常、`abnormal` 发现异常、`uncertain` 无法确定 |
| `description` | `str` | 中文判断说明 |

入参样例与调用：

```python
from llm.vision import VisionAnalyzer

analyzer = VisionAnalyzer()
try:
    result = analyzer("巡查照片.jpg")
finally:
    analyzer.close()
```

出参样例：

```python
{"status": "uncertain", "description": "图片较模糊，无法确认人员是否持有刀具。"}
```

## 2 utils

### 2.1 catalog.py

固定读取 `Map/zones/functional_zones.json`。

| 入参 | 类型 | 含义 |
|---|---|---|
| `target` | `ZoneQuery` | 查询条件对象，从 `llm.utils.catalog` 导入，主要字段见下表 |
| `fleet` | `list[dict]` 或 `None` | 默认 `None`；以无人机为参照时提供，每架只需 `name` 和 `position_m`（UE 世界坐标 `[x, y, z]`，单位米） |

`ZoneQuery` 的主要字段：

| 字段 | 类型 | 含义 |
|---|---|---|
| `zone_id`、`name`、`category`、`subtype` | `str` 或 `None` | 区域 ID、名称关键词、类别、子类；至少填写一项，多个条件同时填写时取交集 |
| `direction` | `str` | 方向：`north`、`south`、`east`、`west`、`northeast`、`northwest`、`southeast`、`southwest`；默认 `none` |
| `reference` | `str` 或 `None` | 参照对象：`map` 表示地图中心，也可填区域 ID、区域完整名称或无人机名称 |
| `radius_m` | `float` 或 `None` | 筛选距参照对象指定米数内的区域，大于 `0` |
| `selection` | `str` | `single` 唯一目标、`nearest` 最近目标、`all` 全部目标，默认 `single` |

方向、半径或最近查询需填写 `reference`。地图右为东（+X），上为北（-Y）。

| 出参 | 类型 | 含义 |
|---|---|---|
| 返回值 | `list[dict]` | 匹配区域的摘要列表 |
| `zone_id`、`name` | `str` | 区域 ID 与名称 |
| `category`、`subtype` | `str` 或 `None` | 区域类别与子类 |
| `distance_m` | `float` | 距离查询时返回的距离，单位米 |

入参样例与调用：

```python
from llm.utils.catalog import ZoneCatalog, ZoneQuery

result = ZoneCatalog().query(ZoneQuery(name="第一学校"))
```

出参样例：

```python
[{"zone_id": "F-SCHOOL-1", "name": "第一学校", "category": "education", "subtype": "中学"}]
```

没有匹配区域、目标不唯一或最近目标并列时，抛出 `Clarification`。
### 2.2 client.py

`create_client()` 读取模型配置，返回模型客户端和模型名称，供 `Planner` 或 `VisionAnalyzer` 使用。

| 入参 | 类型 | 含义 |
|---|---|---|
| `timeout` | `float` | 请求超时设置，单位秒，默认 `60.0` |
| `max_retries` | `int` | 请求失败后的重试次数，默认 `1` |

| 出参 | 类型 | 含义 |
|---|---|---|
| `(client, model)` | `tuple` | 模型客户端和模型名称 |

入参样例与调用：

```python
from llm.utils.client import create_client

client, model = create_client(timeout=60.0, max_retries=1)
```

## 3 配置文件

配置读取 `llm/.env`，已有环境变量优先。

| 配置项 | 含义 |
|---|---|
| `OPENAI_API_KEY` | 模型服务密钥，必填 |
| `OPENAI_MODEL` | 规划模型，默认 `gpt-4.1-mini` |
| `OPENAI_BASE_URL` | 模型服务地址，可选，留空使用官方服务 |
| `OPENAI_VISION_MODEL` | 视觉模型，可选，未设置时使用 `OPENAI_MODEL` |
