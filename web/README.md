# web 文件结构

提供城市地图、无人机实时监控，以及自然语言任务的规划与搜索界面。

```text
web/
├─ index.html                 页面布局与按钮入口
├─ serve.cjs                  启动网页服务，分发页面资源和接口请求
├─ js/                        浏览器端交互
│  ├─ map.js                  地图绘制、区域查询与图层控制
│  ├─ airsim.js               无人机位置、飞行轨迹与实时画面
│  └─ llm.js                  任务提交、模型回答与搜索进度展示
├─ css/
│  └─ styles.css              页面样式
├─ server/                    后端功能
│  ├─ plan-and-search.cjs     “规划并开始搜索”按钮对应的流程、任务队列与进度查询
│  ├─ python_bridge.py        调用 Python 中的规划、状态读取和单区域搜索能力
│  └─ airsim_bridge.py        从 AirSim 读取无人机状态和相机画面
└─ assets/
   └─ town12.png              地图实景底图
```

地图数据来自项目的 `Map/`。任务规划由 `llm/` 完成，巡查飞行与拍照由 `UAV/` 执行，`web/` 负责连接这些功能并展示结果。

`plan-and-search.cjs` 中的主要对象：

- `PlanAndSearchController`：接收网页任务，组织状态读取、规划与执行。
- `PythonRuntime`：管理 Python 进程及请求通信。
- `MissionExecutor`：同一无人机按序搜索，不同无人机并行，记录任务状态。