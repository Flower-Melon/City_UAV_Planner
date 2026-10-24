"""将同目录的功能区地图转换为供规划模型读取的静态目录。"""

from collections import Counter
import json
from pathlib import Path


def generate_catalog():
    """读取 functional_zones.json，生成 zone_catalog.json 并返回输出路径。"""
    directory = Path(__file__).resolve().parent
    source = json.loads((directory / "functional_zones.json").read_text(encoding="utf-8-sig"))
    features = source["features"]
    counts = Counter(feature["properties"]["category"] for feature in features)
    metadata = {
        "zone_fields": ["zone_id", "name", "category", "subtype"],
        "categories": [
            {"code": category["code"], "name": category["name"],
             "subtypes": category.get("subtypes", []), "count": counts[category["code"]]}
            for category in source["categories"]
        ],
        "direction_convention": "地图右东(+X)、上北(-Y)；不是地理真北",
        "total_objects": len(features),
        "facility_ids": [feature["id"] for feature in features
                         if feature["properties"].get("object_type") != "region"],
    }
    rows = [[feature["id"], feature["properties"]["name"], feature["properties"]["category"],
             feature["properties"].get("subtype")] for feature in features]
    text = json.dumps(metadata, ensure_ascii=False, indent=2).removesuffix("\n}")
    text += ',\n  "zones": [\n'
    text += ",\n".join("    " + json.dumps(row, ensure_ascii=False) for row in rows)
    text += "\n  ]\n}\n"
    output = directory / "zone_catalog.json"
    with output.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(text)
    return output


if __name__ == "__main__":
    print(f"已生成目录：{generate_catalog()}")
