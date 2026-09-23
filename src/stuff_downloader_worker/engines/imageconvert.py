"""Local image conversion worker. The URL field carries a local source path."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..protocol import JobSpec
from .base import Emit, EngineError


class ImageConvertEngine:
    name = "imageconvert"

    def download(self, job: JobSpec, emit: Emit) -> dict[str, Any]:
        from ..image_convert import convert_local_image

        fmt = job.options.get("format")
        background = job.options.get("background", "#ffffff")
        quality = job.options.get("quality", 90)
        if not isinstance(fmt, str) or not isinstance(background, str):
            raise EngineError("bad_options", "invalid conversion options")
        result = convert_local_image(
            Path(job.url), Path(job.output_dir), fmt,
            background=background, quality=quality, job_id=job.job_id, emit=emit,
        )
        return {"files": [str(result.path)], "note": result.note}
