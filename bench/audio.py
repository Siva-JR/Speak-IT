"""Audio loading: everything is converted to 16 kHz mono float32."""
import subprocess
import numpy as np
import soundfile as sf

SR = 16000


def load_audio(path: str) -> np.ndarray:
    try:
        data, sr = sf.read(path, dtype="float32", always_2d=True)
        data = data.mean(axis=1)
        if sr != SR:
            raise ValueError("resample")
        return np.ascontiguousarray(data)
    except Exception:
        out = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", path, "-f", "f32le", "-ac", "1", "-ar", str(SR), "-"],
            capture_output=True, check=True,
        ).stdout
        return np.frombuffer(out, dtype=np.float32).copy()
