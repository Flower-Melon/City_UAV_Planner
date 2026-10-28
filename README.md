# 城市无人机巡查平台

本项目基于 AirSim 城市场景，提供无人机任务规划、三维导航、道路巡查和实时监控，并支持生成异常车辆、持刀人员和病人。
用户在网页输入任务后，系统通过大模型确定搜索区域并分配无人机，按高度规划绕楼或跨楼航线，到达后沿完整街区道路巡查、拍摄节点和街道照片，完成任务后返航降落。

## 文件结构

```text
├─ .gitignore
├─ README.md
├─ requirements.txt
├─ Map/
│  ├─ roads/                  车道、巡查道路与分层飞行路网
│  ├─ objects/                建筑与任务点
│  └─ zones/                  功能区与模型目录
├─ UAV/
│  ├─ API/                    无人机飞行、巡查与复位接口
│  ├─ runtime/                AirSim 通信、状态读取与飞行执行
│  └─ utils/                  路径与速度规划
├─ Abnormal/
│  ├─ __init__.py             异常场景接口导出
│  ├─ vehicle/                异常车辆生成与行为控制
│  ├─ armed_person/           持刀人员生成
│  └─ patient/                病人生成
├─ llm/
│  ├─ __init__.py             Python 包入口
│  ├─ .env                    本地模型配置
│  ├─ planner.py              自然语言任务规划
│  ├─ vision.py               图片分析
│  ├─ prompts/                规划与视觉提示词
│  └─ utils/                  模型连接与地图查询
├─ web/
│  ├─ index.html              网页入口
│  ├─ serve.cjs               网页服务入口
│  ├─ js/                     浏览器脚本
│  ├─ css/                    页面样式
│  ├─ server/                 网页后端
│  ├─ utils/                  Python 与仿真桥接工具
│  └─ assets/                 地图底图等静态素材
└─ output/                    输出文件夹
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

具体文件分工见[web/README.md](web/README.md)。

## 2 无人机功能函数库`UAV`

接口参数与调用示例见[UAV/README.md](UAV/README.md)。

## 3 地图数据`Map`

地图数据按以下路径保存：

```text
roads/road_network.json        原始车道级道路拓扑及通行规则
roads/uav_road_network.json    完整街区道路巡查使用的二维路网
roads/uav_flight_network.json  支持绕楼、穿楼间通道和跨楼的分层飞行路网
zones/functional_zones.json    城市功能区与设施边界
zones/zone_catalog.json        供模型读取的区域目录
objects/buildings.json        建筑轮廓与高度
objects/task_points.json      任务点与道路接入信息
```

`zones/zone_catalog.json` 是供模型读取的静态目录，由同目录下的脚本生成：

```powershell
python Map/zones/generate_catalog.py
```

具体细节见[Map/README.md](Map/README.md)。

## 4 大模型`llm`

`planner.py` 将自然语言任务转换为区域搜索指令，`vision.py` 分析本地巡查图片。`utils/` 提供模型连接和地图查询，`prompts/` 保存提示词，模型配置位于 `llm/.env`。

具体细节参见[llm/README.md](llm/README.md)。

## 5 异常场景`Abnormal`

提供异常行为的生成和操控接口。

具体情况见[Abnormal/README.md](Abnormal/README.md)。
