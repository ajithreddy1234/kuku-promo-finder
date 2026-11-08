"""Tiny synthetic API checks. Never print keys or raw provider exceptions."""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict
import yaml

ROOT = Path(__file__).resolve().parents[1]


class Probe(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str
    count: int


def main():
    keys = dotenv_values(ROOT / ".env")
    config = yaml.safe_load((ROOT / "config.yaml").read_text())
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--include-gemini',action='store_true',help='Also run a synthetic Gemini check; never sends screenplay text')
    args=parser.parse_args()
    checks=[(role,config['models'][role]) for role in ('analysis','blind')]
    if args.include_gemini:checks.append(('synthetic_gemini',{'provider':'gemini','model':config['diagnostics']['gemini_model']}))
    prompt = 'Return JSON with status "ok" and count 2. This is a synthetic connection check.'
    results = []
    for role,model_config in checks:
        provider, model = (model_config[k] for k in ("provider", "model"))
        row = {"role": role, "provider": provider, "model": model,
               "checked_at": datetime.now(timezone.utc).isoformat(), "synthetic_only": True}
        try:
            if provider == "gemini":
                from google import genai
                with genai.Client(api_key=keys.get("GEMINI_API_KEY"),
                                  http_options={"timeout": 30000}) as client:
                    response = client.models.generate_content(
                        model=model, contents=prompt,
                        config={"response_mime_type": "application/json",
                                "response_json_schema": Probe.model_json_schema(),
                                "max_output_tokens": 128, "temperature": 0})
                    raw = response.text
            elif provider == "groq":
                from groq import Groq
                with Groq(api_key=keys.get("GROQ_API_KEY"), timeout=30, max_retries=0) as client:
                    response = client.chat.completions.create(
                        model=model, messages=[{"role": "user", "content": prompt}],
                        response_format={"type": "json_schema", "json_schema": {
                            "name": "probe", "strict": True, "schema": Probe.model_json_schema()}},
                        max_completion_tokens=512, temperature=0)
                    raw = response.choices[0].message.content
            else:
                raise ValueError("Unsupported provider")
            value = Probe.model_validate_json(raw)
            row["passed"] = value.status == "ok" and value.count == 2
        except Exception as exc:
            row.update(passed=False, error_type=type(exc).__name__,
                       status_code=getattr(exc, "status_code", getattr(exc, "code", None)))
        results.append(row)
        print(json.dumps(row))
    destination = ROOT / "runs" / "preflight.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(results, indent=2) + "\n")
    return 0 if all(row["passed"] for row in results) else 1


if __name__ == "__main__":
    sys.exit(main())
