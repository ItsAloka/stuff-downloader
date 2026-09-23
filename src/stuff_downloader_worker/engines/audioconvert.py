"""Local audio conversion worker. The URL field carries a local source path."""

from pathlib import Path

from ..protocol import JobSpec
from .base import Emit, EngineError


class AudioConvertEngine:
    name = "audioconvert"

    def download(self, job: JobSpec, emit: Emit) -> dict:
        from ..audio_convert import convert_local_audio

        fmt = job.options.get("format")
        if not isinstance(fmt, str):
            raise EngineError("bad_options", "invalid audio format")
        result = convert_local_audio(Path(job.url), Path(job.output_dir), fmt,
                                     job_id=job.job_id, emit=emit)
        return {"files": [str(result)]}
