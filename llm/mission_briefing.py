"""Generate a cited briefing when a patrol event changes the situation."""

import json
from pathlib import Path


class MissionBriefing:
    def __init__(self, client, model):
        self.client = client
        self.model = model
        self.prompt = (Path(__file__).parent / "prompts" / "mission_briefing.txt").read_text(encoding="utf-8")

    def summarize(self, current, events, trigger_ids, previous_update=None):
        if not isinstance(current, dict) or not events or not trigger_ids:
            raise ValueError("本次巡查更新缺少有效事件。")
        evidence = {event["id"]: event for event in events}
        triggers = set(trigger_ids)
        if not triggers.issubset(evidence):
            raise ValueError("巡查更新的触发事件无效。")

        response = self.client.responses.create(
            model=self.model,
            instructions=self.prompt,
            input=[{"role": "user", "content": json.dumps(
                {"current": current, "events": events, "trigger_ids": trigger_ids,
                 "previous_update": previous_update},
                ensure_ascii=False
            )}],
            temperature=0,
            text={"format": {"type": "json_object"}},
            store=False,
        )
        if response.status != "completed":
            raise ValueError("模型未完成本次巡查研判。")
        try:
            result = json.loads(response.output_text)
            summary = result["summary"].strip()
            focus = result["next_focus"].strip()
            citations = result["evidence"]
            if not (0 < len(summary) <= 260 and 0 < len(focus) <= 140 and isinstance(citations, list)
                    and 1 <= len(citations) <= 4 and all(type(item) is int and item in evidence for item in citations)
                    and len(set(citations)) == len(citations) and triggers.intersection(citations)):
                raise ValueError
        except (ValueError, TypeError, KeyError, AttributeError):
            raise ValueError("模型返回的研判或引用无效。") from None

        subjects = {event.get("subject") for event in events if event["id"] in triggers}
        subjects.discard(None)
        case_notes = {}
        notes = result.get("case_notes")
        if isinstance(notes, list):
            for note in notes[:4]:
                if not isinstance(note, dict):
                    continue
                key, text = note.get("id"), note.get("text")
                if isinstance(key, str) and key in subjects and isinstance(text, str) and 0 < len(text.strip()) <= 120:
                    case_notes[key] = text.strip()
        return {"summary": summary, "next_focus": focus, "evidence": [evidence[item] for item in citations],
                "case_notes": case_notes, "model": self.model}
