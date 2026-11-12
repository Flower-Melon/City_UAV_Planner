# web 项目结构

`web/` 是城市仿真的操作界面。在项目根目录运行 `node web/serve.cjs` 启动。

```text
web/
├─ README.md                  目录与文件职责说明
├─ index.html                 页面结构
├─ serve.cjs                  网页服务入口
├─ js/                        浏览器脚本
│  ├─ map.js                  地图交互
│  ├─ airsim.js               无人机实时监控
│  ├─ llm.js                  自然语言任务、异常照片和红框展示
│  └─ abnormal.js             异常场景交互
├─ css/                       页面样式
│  └─ styles.css              页面外观
├─ server/                    Node.js 后端
│  ├─ airsim.cjs              无人机监控接口
│  ├─ llm.cjs                 任务规划、搜索调度、图像处理进度与告警图片接口
│  ├─ abnormal.cjs            异常场景接口
│  └─ python.cjs              启动 Python 进程并收发请求
├─ utils/                     Python 桥接工具
│  ├─ python_bridge.py        Python 功能调用入口、网页模型后端的读取与保存
│  ├─ vision_pipeline.py      巡查人员识别、异常标注与地面定位
│  ├─ people_state.py         人物实时位置与演示目标查询
│  ├─ traffic_pipeline.py     高空车辆取图、跟踪测速与超速证据
│  ├─ airsim_bridge.py        从 AirSim 采集无人机状态和画面
│  └─ unreal_bridge.py        UE 内的异常目标观测与跟随取景
└─ assets/                    静态素材
   └─ town12.png              地图实景底图
```

## 子文件夹分工

| 目录 | 功能划分 |
|---|---|
| `js/` | 前端部分，实现页面交互。 |
| `css/` | 定义页面外观。 |
| `server/` | 后端部分，处理网页请求。 |
| `utils/` | 脚本工具，连接 Python 功能与仿真环境。 |
| `assets/` | 存放页面素材。 |

## 区域搜索与高空监控

网页确认搜索区域后，由 `server/llm.cjs` 同时调度 `UAV/API/search_area.py` 和 `UAV/API/monitor_area.py`。每组分别使用一架巡查机与一架监控机，至少需要两架可用无人机；监控机占用确认后再启动巡查选机，两机并行飞行。

## 输出目录

每个区域分配一个连续编号目录，由网页调度端在启动无人机前统一创建。同一区域的低空巡查和高空监控共用这个编号；一次确认多个区域会创建多个编号目录，不再增加 `task_1/` 层级。已有历史目录保持原样。

```text
output/000001/                  一个区域的高低空联合任务
├─ task.json                    所属网页任务、区域ID、飞行/测速参数及两架无人机ID
├─ search/                      低空巡查
│  ├─ events.json               异常与漏拍记录（发生时生成）
│  ├─ capture/
│  │  ├─ 000001.jpg             全部巡查原图，按拍摄顺序编号
│  │  ├─ route.json             巡查航线及返航路径
│  │  └─ frame.json             照片文件名、拍摄位置与道路编号
│  ├─ yolo_detection/           仅检出人车时生成
│  │  ├─ 000001.json            原图像素框、置信度及裁剪路径
│  │  ├─ 000001.annotated.jpg   YOLO标注图
│  │  └─ crops/                 人车目标裁剪放大图
│  └─ llm_analysis/
│     ├─ 000001.json            单张原图的各目标判断与YOLO框
│     └─ abnormal/              异常红框图片，与巡查原图同名
└─ monitor/                     高空车辆测速
   ├─ latest.json               最新测速状态（持续更新至停止监测）
   ├─ capture/                  仅保存触发超速证据的原图
   │  └─ 000123.jpg
   ├─ 000123.json               对应帧的超速记录
   └─ 000123.annotated.jpg      超速标注图
```

`task.json` 保存任务关联信息，不作为完成标记；低空巡查结束后，高空监控仍可继续更新。`search/` 和 `monitor/` 的产物随对应流程运行生成。ck
