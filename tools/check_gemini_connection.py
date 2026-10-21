#!/usr/bin/env python3
"""Diagnose Gemini metadata, file upload and token counting. Never generate."""

import argparse
import os
from pathlib import Path
import re
import time


def diagnose(video, model, destination):
    from google import genai
    from google.genai import errors, types
    from clipforge.storage import write_json

    if os.environ.get("GITHUB_REPOSITORY") != "nyu1791-collab/ai-model-workers":
        raise RuntimeError("live diagnostics require the designated private repository")
    if not re.fullmatch(r"(?:models/)?[A-Za-z0-9._-]+", model):
        raise RuntimeError("invalid model identifier")
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("GEMINI_API_KEY is required")
    report = {
        "status": "checking",
        "requested_model": model,
        "generation_requests": 0,
        "operation": "model_metadata",
        "http_events": [],
        "remote_file_cleanup": "not_needed",
    }

    def observe(response):
        path = re.sub(r"/files/[^/]+", "/files/[id]", response.request.url.path)
        report["http_events"].append(
            {
                "method": response.request.method,
                "path": path,
                "http_status": response.status_code,
            }
        )

    client = genai.Client(
        api_key=key,
        http_options=types.HttpOptions(
            timeout=60000,
            retry_options=types.HttpRetryOptions(attempts=1),
            client_args={"event_hooks": {"response": [observe]}},
        ),
    )
    name = None
    try:
        metadata = client.models.get(model=model)
        report["model_available"] = bool(metadata.name)
        report["operation"] = "file_upload"
        write_json(destination, report)
        file = client.files.upload(
            file=video,
            config=types.UploadFileConfig(
                mime_type="video/mp4", display_name="clipforge-synthetic-diagnostic"
            ),
        )
        name = file.name
        report["operation"] = "file_processing"
        until = time.monotonic() + 120
        while file.state and file.state.name == "PROCESSING":
            if time.monotonic() > until:
                raise RuntimeError("file processing time limit exceeded")
            time.sleep(2)
            file = client.files.get(name=name)
        if not file.state or file.state.name != "ACTIVE":
            raise RuntimeError("file did not become active")
        report["operation"] = "token_count"
        count = client.models.count_tokens(
            model=model,
            contents=[
                types.Part(
                    file_data=types.FileData(file_uri=file.uri, mime_type="video/mp4"),
                    video_metadata=types.VideoMetadata(fps=2),
                )
            ],
        )
        report["total_tokens"] = count.total_tokens
        report["status"] = "complete"
    except errors.APIError as error:
        report.update(
            status="failed", error_type=type(error).__name__, http_status=error.code
        )
    except Exception as error:
        report.update(status="failed", error_type=type(error).__name__)
    finally:
        if name:
            try:
                client.files.delete(name=name)
                report["remote_file_cleanup"] = "complete"
            except Exception:
                report["remote_file_cleanup"] = "incomplete"
        try:
            client.close()
        except Exception:
            report["client_close"] = "failed"
        write_json(destination, report)
    print(
        "Gemini diagnostic: "
        + report["status"]
        + " at "
        + report["operation"]
        + "; generation_requests=0"
    )
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = diagnose(args.video, args.model, args.output)
    return 0 if result["status"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
