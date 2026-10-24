# 城市场景

本项目以 `Map/` 中的标定数据为核心；`web/` 提供地图与无人机实时监控；`UAV/`存储无人机函数库的具体实现；`llm/`负责自然语言任务规划和图片分析。
用户在网页输入任务后，系统通过大模型查询本地地图、确定搜索区域并分配无人机，随后自动执行沿道路巡查和定时拍照。

## 文件结构

```text

├─ README.md
├─ Map/
│  ├─ road_network.json         统一节点与边、车辆与无人机通行规则
│  ├─ objects/                  建筑与任务点
│  │  ├─ buildings.json         6594个建筑轮廓
│  │  └─ task_points.json       725个任务与参考点、道路接入候选
│  └─ zones/                    功能区与模型目录
│     ├─ functional_zones.json  375个功能区与设施、20类标签
│     ├─ zone_catalog.json      供规划模型读取的静态区域目录
│     └─ generate_catalog.py    将功能区数据转换为模型目录
├─ UAV/
│  ├─ API/                      存储无人机功能函数库(直接面向`airsim`仿真环境)
│  ├─ utils/                    无人机功能函数所有的子函数
│  └─ search_results/           本地巡查图片和结果
├─ llm/
│  ├─ planner.py              自然语言任务规划
│  ├─ vision.py               图片分析
│  ├─ utils/                  辅助功能
│  ├─ prompts/                规划与视觉提示词
│  └─ .env                    本地模型配置
└─ web/
   ├─ index.html                网页入口
   ├─ serve.cjs                 服务启动入口与接口路由
   ├─ js/                       浏览器脚本
   │  ├─ map.js                 交互地图面板
   │  ├─ airsim.js              无人机位置与实时画面面板
   │  └─ llm.js                 自然语言任务面板
   ├─ css/
   │  └─ styles.css             页面样式
   ├─ server/                   后端代码
   │  ├─ plan-and-search.cjs    "规划并开始搜索"按钮对应的全流程工作流
   │  ├─ python_bridge.py       Python 能力的通信适配
   │  └─ airsim_bridge.py       AirSim 状态与画面读取
   └─ assets/
      └─ town12.png             实景底图
```

## 0 项目统一管理注意事项

1. 一定注意，开发一个功能时不能直接在根目录下进行，至少要单独新建一个子文件夹开发
2. `git`提交时，统一的格式是`git commit -m "xxxx年x月x日：开发了什么功能"`
3. 开发新功能时，先`git fetch`查看有无新的提交再进行代码的改动，如果有则`git pull`
4. 一定要注意功能的解耦，严禁一个文件近千行来完成一个完备的流程，必须进行功能的拆分
5. 此说明文档的内容严禁使用`AI`覆写，最好可以手写

## 1 可视化`web`

网页服务需要 Node.js。执行无人机任务前，先启动 AirSim 仿真，并在 `llm/.env` 中填写模型配置。

在项目根目录安装统一的 Python 依赖，首次配置可运行：

```powershell
python -m pip install -r requirements.txt
```

在项目根目录运行：

```powershell
node web/serve.cjs
```

运行后自动打开浏览器展现可视化界面。

具体文件分工见[web/README.md](web/README.md)。

## 2 无人机功能函数库`UAV`

无人机功能函数库，具体细节见[UAV/README.md](UAV/README.md)。

## 3 地图数据`Map`

地图数据按以下路径保存：

```text
road_network.json:存储道路拓扑结构
zones/functional_zones.json:存储城市功能分区标签
zones/zone_catalog.json:存储供规划模型读取的区域目录
objects/buildings.json:存储建筑物的具体信息
objects/task_points.json:存储城市中手动打好的目标任务点
```

`zones/zone_catalog.json` 是供模型读取的静态目录，由同目录下的脚本生成：

```powershell
python Map/zones/generate_catalog.py
```

具体细节见[Map/README.md](Map/README.md)。

## 4 大模型`llm`

存储整个项目中关于大模型调用的所有内容，`planner.py` 将自然语言任务转换为搜索指令；`vision.py` 分析本地巡查图片。`utils/` 提供子工具，`prompts/` 保存提示词，模型配置放在 `.env`下。

具体细节参见[llm/README.md](llm/README.md)。
