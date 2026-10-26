"""One explicitly authorized repeat of a failed request, using its saved prompt/audio."""

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
import urllib.error
import urllib.request

from clipforge.gemini_audio_editor import (
    PROJECT,
    audio_part,
    collect_sse_response,
    extract_json_object,
)
from clipforge.http_diagnostics import capture_http_error
from clipforge.storage import write_json


def main():
    parent, phase = os.environ["PARENT_RELEASE"], os.environ["FAILED_PHASE"]
    if not re.fullmatch(r"clipforge-kick-editorial-[0-9]+", parent):
        raise ValueError("Invalid parent release")
    if not re.fullmatch(r"transcript-[0-9]+-[0-9]{3}-[a-f0-9]{10}", phase):
        raise ValueError("Invalid failed phase")
    output = Path(os.environ["DIAGNOSTIC_OUTPUT"])
    output.mkdir(mode=0o700)
    saved = output / "saved"
    saved.mkdir(mode=0o700)

    def fetch(name):
        subprocess.run(
            [
                "gh",
                "release",
                "download",
                parent,
                "--repo",
                "nyu1791-collab/ai-model-workers",
                "--pattern",
                name,
                "--dir",
                str(saved),
            ],
            check=True,
            capture_output=True,
            timeout=120,
        )

    fetch("audio-editor-receipt.json")
    fetch(phase + ".prompt.txt")
    receipt = json.loads((saved / "audio-editor-receipt.json").read_text())
    rows = [r for r in receipt["requests"] if r["phase"] == phase]
    if len(rows) != 1 or rows[0]["status"] != "failed":
        raise ValueError("Exactly one failed saved phase is required")
    row = rows[0]
    if row["model"] != "gemini-3.8-flash":
        raise ValueError("Only the designated model is allowed")
    prompt = (saved / (phase + ".prompt.txt")).read_text()
    if hashlib.sha256(prompt.encode()).hexdigest() != row["prompt_sha256"]:
        raise ValueError("Saved prompt identity differs")
    system, user = prompt.split("\n\n【Gemini user content】\n", 1)
    system = system.removeprefix("【Gemini systemInstruction】\n")
    audio = re.findall(r"^音声パート\d+: (.+)（([^（）\n]+)）$", user, re.M)
    if not audio:
        raise ValueError("Saved audio manifest is missing")
    parts = [{"text": user}]
    for label, name in audio:
        if not re.fullmatch(
            r"(?:transcript-audio-[0-9]+-[0-9]{3}|voice-speaker_[1-6])\.ogg", name
        ):
            raise ValueError("Unexpected audio file")
        fetch(name)
        parts.extend(
            [
                {"text": f"\n\n音声パート「{label}」:"},
                audio_part(saved / name),
            ]
        )
    body = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {
            "thinkingConfig": {"thinkingLevel": "MEDIUM"},
            "maxOutputTokens": 65536,
        },
        "systemInstruction": {"role": "system", "parts": [{"text": system}]},
    }
    record = {
        "parent_release": parent,
        "phase": phase,
        "model": row["model"],
        "project": PROJECT,
        "prompt_sha256": row["prompt_sha256"],
        "status": "submitted_or_unknown",
        "requests_submitted": 1,
        "started_at": time.time(),
        "automatic_retries": 0,
    }

    def save():
        write_json(output / "diagnostic-receipt.json", record)

    request = urllib.request.Request(
        f"https://aiplatform.googleapis.com/v1/projects/{PROJECT}/locations/global/publishers/google/models/{row['model']}:streamGenerateContent?alt=sse",
        data=json.dumps(body, ensure_ascii=False).encode(),
        headers={
            "Authorization": "Bearer " + os.environ["CLIPFORGE_VERTEX_ACCESS_TOKEN"],
            "Content-Type": "application/json",
        },
    )
    save()
    try:
        with urllib.request.urlopen(request, timeout=1800) as response:
            result, text, _, _ = collect_sse_response(
                response,
                output / (phase + ".sse.txt"),
                record,
                save,
            )
        write_json(output / (phase + ".response.json"), result)
        (output / (phase + ".raw.txt")).write_text(text)
        value = extract_json_object(text)
        write_json(output / (phase + ".json"), value)
        record.update(status="received", usage=result.get("usageMetadata", {}))
    except urllib.error.HTTPError as error:
        record.update(
            status="http_error",
            **capture_http_error(
                error,
                output / (phase + ".http-error.json"),
            ),
        )
    except Exception as error:
        record.update(status="failed_or_unknown", error_type=type(error).__name__)
    finally:
        record["finished_at"] = time.time()
        save()
    print(
        json.dumps(
            {
                k: record.get(k)
                for k in (
                    "phase",
                    "status",
                    "http_status",
                    "requests_submitted",
                    "automatic_retries",
                )
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
