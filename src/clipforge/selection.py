"""One explicit multimodal Gemini generation; no fallback or inference retries."""

import os
from pathlib import Path
import time

from .analysis_budget import check_previews, prepare_text
from .models import PickEnvelope, SelectedClip, validate_picks
from .storage import PipelineError, digest


def response_schema(candidates, settings):
    # Google's Schema dialect does not accept JSON Schema's exclusiveMinimum.
    # Keep the wire contract portable; Pydantic enforces strict ordering and
    # duration bounds again after the response arrives.
    return {
        "type": "OBJECT",
        "required": ["clips"],
        "properties": {
            "clips": {
                "type": "ARRAY",
                "minItems": 1,
                "maxItems": settings.clips,
                "items": {
                    "type": "OBJECT",
                    "required": [
                        "candidate_id",
                        "start",
                        "end",
                        "title",
                        "reason",
                        "confidence",
                    ],
                    "properties": {
                        "candidate_id": {
                            "type": "STRING",
                            "enum": [c.id for c in candidates],
                        },
                        "start": {
                            "type": "NUMBER",
                            "minimum": 0,
                            "maximum": settings.max_seconds,
                        },
                        "end": {
                            "type": "NUMBER",
                            "minimum": 0,
                            "maximum": settings.max_seconds,
                        },
                        "title": {"type": "STRING"},
                        "reason": {"type": "STRING"},
                        "confidence": {"type": "NUMBER", "minimum": 0, "maximum": 1},
                    },
                },
            }
        },
    }


def choose(source, previews, candidates, transcript, info, settings):
    # Validate ALL inputs before importing the SDK, creating a Client or uploading
    # the first file. A later oversized candidate must never cause partial upload.
    texts, budget = prepare_text(candidates, transcript, info["duration"], settings)
    previews = [Path(path) for path in previews]
    if len(candidates) != len(previews):
        raise PipelineError(
            "analysis candidates and previews must have matching counts"
        )
    budget.update(check_previews(previews, settings))
    visual_inputs = [
        {"id": c.id, "sha256": digest(p), "bytes": size}
        for c, p, size in zip(
            candidates, previews, budget["preview_bytes"], strict=True
        )
    ]
    from google import genai
    from google.genai import errors, types

    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise PipelineError("GEMINI_API_KEY is required for visual selection")
    client = genai.Client(
        api_key=key,
        http_options=types.HttpOptions(
            timeout=180000, retry_options=types.HttpRetryOptions(attempts=1)
        ),
    )
    uploaded = []
    result = None
    operation = "initialization"
    started = time.monotonic()
    try:
        parts = [types.Part.from_text(text=texts[0])]
        for candidate, path, text, snapshot in zip(
            candidates, previews, texts[1:], visual_inputs, strict=True
        ):
            if time.monotonic() - started > 720:
                raise PipelineError("analysis upload time budget exceeded")
            if (
                path.stat().st_size != snapshot["bytes"]
                or digest(path) != snapshot["sha256"]
            ):
                raise PipelineError("analysis preview changed before upload")
            operation = "file_upload"
            file = client.files.upload(
                file=path,
                config=types.UploadFileConfig(
                    mime_type="video/mp4", display_name=candidate.id
                ),
            )
            uploaded.append(file.name)
            deadline = time.monotonic() + 180
            operation = "file_processing"
            while file.state and file.state.name == "PROCESSING":
                if time.monotonic() > deadline or time.monotonic() - started > 720:
                    raise PipelineError("Gemini video processing timed out")
                time.sleep(2)
                file = client.files.get(name=file.name)
            if not file.state or file.state.name != "ACTIVE" or not file.uri:
                raise PipelineError("Gemini could not process a candidate video")
            parts += [
                types.Part.from_text(text=text),
                types.Part(
                    file_data=types.FileData(file_uri=file.uri, mime_type="video/mp4"),
                    video_metadata=types.VideoMetadata(fps=settings.analysis_fps),
                ),
            ]
        operation = "generation"
        response = client.models.generate_content(
            model=settings.gemini_model,
            contents=parts,
            config=types.GenerateContentConfig(
                temperature=0.2,
                max_output_tokens=6000,
                response_mime_type="application/json",
                response_schema=response_schema(candidates, settings),
                tools=[],
            ),
        )
        if (
            not response.candidates
            or response.candidates[0].finish_reason != types.FinishReason.STOP
            or not response.text
        ):
            raise PipelineError("Gemini returned no selection")
        envelope = PickEnvelope.model_validate_json(response.text)
        by_id = {c.id: c for c in candidates}
        selected = []
        for pick in envelope.clips:
            candidate = by_id.get(pick.candidate_id)
            if candidate is None or pick.end > candidate.end - candidate.start + 0.05:
                raise PipelineError(
                    "Gemini returned an unknown or out-of-range candidate"
                )
            selected.append(
                SelectedClip(
                    candidate_id=candidate.id,
                    start=round(candidate.start + pick.start, 6),
                    end=round(candidate.start + pick.end, 6),
                    title=pick.title,
                    reason=pick.reason,
                    confidence=pick.confidence,
                )
            )
        validate_picks(selected, info["duration"], settings)
        usage = response.usage_metadata
        result = {
            "clips": [c.model_dump() for c in selected],
            "receipt": {
                "provider": "gemini",
                "requested_model": settings.gemini_model,
                "returned_model": getattr(response, "model_version", None),
                "generation_requests": 1,
                "automatic_fallback": False,
                "analysis_budget": budget,
                "visual_inputs": visual_inputs,
                "usage": usage.model_dump(mode="json") if usage else None,
                "remote_file_cleanup": None,
            },
        }
        return result
    except PipelineError:
        raise
    except errors.APIError as error:
        status = (
            error.code
            if isinstance(error.code, int) and 100 <= error.code <= 599
            else "unknown"
        )
        raise PipelineError(
            f"Gemini {operation} failed (HTTP {status}); no automatic retry"
        ) from None
    except Exception as error:
        raise PipelineError(
            "Gemini selection failed (" + type(error).__name__ + ")"
        ) from None
    finally:
        failed = 0
        for name in uploaded:
            try:
                client.files.delete(name=name)
            except Exception:
                failed += 1
        try:
            client.close()
        except Exception:
            pass
        if result is not None:
            result["receipt"]["remote_file_cleanup"] = (
                "incomplete" if failed else "complete"
            )
        # File names/URIs and provider bodies are deliberately never logged.
        if failed:
            print(
                "Remote analysis file cleanup incomplete; files may remain until service expiry."
            )
