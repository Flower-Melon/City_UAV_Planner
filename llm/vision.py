"""将本地目标图片分为正常、病人、持刀人员，返回类别和中文说明。"""

import base64
import json
from pathlib import Path
import sys

from PIL import Image

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "llm"

from .utils.client import create_client, error_message

PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "search_vision.txt"


class VisionAnalyzer:
    @staticmethod
    def _validate_analysis(value):
        """校验大模型返回的三类目标标识和中文说明。"""
        if (not isinstance(value, dict) or set(value) != {"status", "description"}
                or value["status"] not in ("normal", "patient", "armed_person")
                or not isinstance(value["description"], str) or not value["description"].strip()
                or len(value["description"]) > 2000):
            raise ValueError("视觉结果需要status（normal、patient或armed_person）和非空description")
        return value

    # 加载视觉提示词并准备可复用的模型连接，可注入已有客户端和模型。
    # 自行创建的连接由本实例负责关闭；此处不上传图片。
    def __init__(self, client=None, model=None):
        self.prompt = PROMPT_PATH.read_text(encoding="utf-8")
        self.owns_client = client is None
        if client is None:
            client, default_model = create_client(timeout=25.0, max_retries=0, vision=True)
            # 超时分别应用于请求的各个网络阶段。
            model = model or default_model
        if not model:
            raise ValueError("视觉模型不能为空")
        self.client, self.model = client, model

    # 将本地 JPEG/PNG 编码后发送给视觉模型，校验其 JSON 结果。
    # 输入为图片路径和可选目标说明；同步等待并返回 status、description。
    def __call__(self, image_path, *, context=None):
        path = Path(image_path)
        suffix = path.suffix.lower()
        if suffix not in (".jpg", ".jpeg", ".png"):
            raise ValueError("视觉分析仅接收JPEG/PNG")
        mime = "image/png" if suffix == ".png" else "image/jpeg"
        with Image.open(path) as image:
            width, height = image.size
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        response = self.client.responses.create(
            model=self.model, store=False, instructions=self.prompt, temperature=0,
            reasoning={"effort": "minimal"}, text={"format": {"type": "json_object"}},
            input=[{"role": "user", "content": [
                {"type": "input_text", "text": (
                    f"图片尺寸{width}×{height}像素。请将主要目标标识为normal、patient或armed_person，返回status和description，不返回坐标。"
                    + (f"\n{context}" if context else ""))},
                {"type": "input_image", "image_url": f"data:{mime};base64,{encoded}", "detail": "high"}]}])
        if response.status != "completed":
            raise RuntimeError("视觉模型响应未完成")
        text = response.output_text.strip()
        if text.startswith("```json\n") and text.endswith("\n```"):
            text = text[8:-4]
        return self._validate_analysis(json.loads(text))

    # 释放本实例创建的模型连接，保留外部注入连接的所有权。
    # 无返回值；应在该实例的图片请求全部结束后调用。
    def close(self):
        if self.owns_client:
            self.client.close()


if __name__ == "__main__":
    # 修改下面的图片路径即可调试。
    image_path = Path(__file__).resolve().parents[1] / "巡查照片.jpg"
    output_dir = Path(__file__).resolve().parents[1] / "output" / "temp"
    try:
        if not image_path.is_file():
            raise ValueError(f"图片文件不存在：{image_path}")
        if image_path.suffix.lower() not in (".jpg", ".jpeg", ".png"):
            raise ValueError("视觉分析仅接收JPEG/PNG")
        analyzer = VisionAnalyzer()
        try:
            result = analyzer(image_path)
        finally:
            analyzer.close()
        output_dir.mkdir(parents=True, exist_ok=True)
        output_file = output_dir / "analysis.json"
        temporary = output_file.with_suffix(".part")
        temporary.write_text(
            json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2) + "\n", encoding="utf-8"
        )
        temporary.replace(output_file)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as error:
        print(error_message(error), file=sys.stderr)
        raise SystemExit(1)
