# 城市场景

本项目以 `Map/` 中的标定数据为核心；`web/` 提供地图与无人机实时监控；`UAV/`存储无人机函数库的具体实现

## 文件结构

```text
标定/
├─ README.md
├─ Map/
│  ├─ README.md                 字段、坐标和数据用途
│  ├─ functional_zones.json     375个功能区与设施、20类标签
│  ├─ task_points.json          725个任务与参考点、道路接入候选
│  ├─ buildings.json            6594个建筑轮廓
│  └─ road_network.json         统一节点与边、车辆与无人机通行规则
├─ UAV/
│  ├─ API/                      存储无人机功能函数库(直接面向`airsim`仿真环境)
│  └─ utils/                    无人机功能函数所有的子函数
└─ web/
   ├─ serve.cjs                 本地HTTP服务与AirSim数据接口
   ├─ airsim_bridge.py          实时位置读取、坐标校准与第三人称画面
   ├─ airsim.js                 无人机位置标注与单路画面切换
   ├─ requirements.txt          Python通信与图像依赖
   ├─ index.html                地图页面
   ├─ styles.css                页面样式
   ├─ map.js                    自动读取Map并绘制交互地图
   └─ assets/town12.png          实景底图
```

## 0 项目统一管理注意事项

1. 一定注意，开发一个功能时不能直接在根目录下进行，至少要单独新建一个子文件夹开发
2. `git`提交时，统一的格式是`git commit -m "xxxx年x月x日：开发了什么功能"`
3. 开发新功能时，先`git fecth`查看有无新的提交再进行代码的改动，如果有则`git pull`
4. 一定要注意功能的解耦，严禁一个文件近千行来完成一个完备的流程，必须进行功能的拆分
5. 此说明文档的内容严禁使用`AI`覆写，最好可以手写

## 1 可视化`web`

Python 需安装 `msgpack` 和 `Pillow`，首次配置可运行：

```powershell
python -m pip install -r web/requirements.txt
```

在项目根目录运行：

```powershell
node web/serve.cjs
```

运行后自动打开浏览器展现可视化界面。

## 2 无人机功能函数库`UAV`

提供仿真中无人机运行的所有函数，具体细节见[UAV/README.md](UAV/README.md)。

## 3 地图数据`Map`

四个json文件存储的地图数据为：

```text
road_network.json:存储道路拓扑结构
functional_zones.json:存储城市功能分区标签
buildings.json:存储建筑物的具体信息
task_points.json:存储城市中手动打好的目标任务点
```

具体细节见[Map/README.md](Map/README.md)。