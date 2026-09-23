"""Local video conversion worker. The URL field carries a local source path."""

from pathlib import Path

from ..protocol import JobSpec
from ..video_convert import convert_local_video
from .base import Emit, EngineError


class VideoConvertEngine:
    name = "videoconvert"

    def download(self, job: JobSpec, emit: Emit) -> dict:
        fmt = job.options.get("format")
        if not isinstance(fmt, str):
            raise EngineError("bad_options", "invalid video format")
        result = convert_local_video(Path(job.url), Path(job.output_dir), fmt,
                                     job_id=job.job_id, emit=emit)
        return {"files": [str(result)]}
