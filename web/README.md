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
│  └─ python-runtime.cjs      启动 Python 进程并收发请求
├─ utils/                     Python 桥接工具
│  ├─ python_bridge.py        Python 功能调用入口
│  ├─ vision_pipeline.py      串联YOLO与大模型，汇总意图结果并绘制异常框
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

网页搜索结构如下：

```text
output/000001/
├─ events.json         异常与漏拍记录（发生时生成）
├─ capture/
│  ├─ 000001.jpg       全部巡查原图，按拍摄顺序编号
│  ├─ 000002.jpg
│  ├─ route.json       巡查航线及返航路径
│  └─ frame.json       照片文件名、拍摄位置与道路编号
├─ yolo_detection/     仅保存检出人车的图片、检测记录和目标裁剪图
│  ├─ 000001.json      原图像素框、置信度及裁剪路径
│  ├─ 000001.annotated.jpg   YOLO标注图
│  └─ crops/           人车目标裁剪放大图，每个目标单独保存
└─ llm_analysis/       大模型意图分析结果
   ├─ 000001.json      单张原图的各目标判断与YOLO框
   └─ abnormal/        使用YOLO框生成的异常红框图片（发现异常时生成）
      └─ 000029.jpg    与capture中的原图同名
```
