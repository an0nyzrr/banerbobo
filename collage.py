"""
Сборка коллажа через ffmpeg.

Раскладка кадра 9:16 (канвас 1080×1920, H = 2 × половина):
  верх  — видео от пользователя (кадр заполняет половину, центральная обрезка);
  низ   — фиолетовое кольцо (assets/ring.mp4), вписано в половину, по центру.

Таймлайн:
  0 .. T        — видео пользователя играет;
  T             — видео пользователя «зависает» на последнем кадре (верх);
  T .. T+B      — поверх зависшего кадра играет баннер (хромакей, зелёный вырезан);
  T+B .. конец  — видео пользователя продолжает играть до конца.
Кольцо внизу всё это время крутится.
T = середина видео пользователя (cut_ratio).
"""

import os
import re
import shutil
import subprocess
from dataclasses import dataclass


class CollageError(RuntimeError):
    pass


def get_ffmpeg() -> str:
    """Ищет ffmpeg: FFMPEG_PATH из окружения → ffmpeg в PATH → бинарник из пакета imageio-ffmpeg."""
    env_path = os.environ.get("FFMPEG_PATH")
    if env_path:
        return env_path
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as e:
        raise CollageError(
            "Не найден ffmpeg. Установи его в систему или pip install imageio-ffmpeg"
        ) from e


@dataclass
class MediaInfo:
    duration: float
    has_audio: bool


_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_AUDIO_RE = re.compile(r"Stream #\d+:\d+.*?:\s*Audio:")


def probe(path: str) -> MediaInfo:
    """Длительность и наличие звука через вывод `ffmpeg -i` (ffprobe в пакете imageio-ffmpeg нет)."""
    res = subprocess.run(
        [get_ffmpeg(), "-hide_banner", "-i", path],
        capture_output=True, text=True,
    )
    # ffmpeg -i без выходного файла всегда возвращает ошибку, это нормально; смотрим stderr
    info = res.stderr
    m = _DURATION_RE.search(info)
    if not m:
        raise CollageError("Не удалось прочитать файл, возможно он повреждён или не видео")
    h, mnt, s = m.groups()
    duration = int(h) * 3600 + int(mnt) * 60 + float(s)
    if duration <= 0:
        raise CollageError("Не удалось определить длительность видео")
    return MediaInfo(duration=duration, has_audio=bool(_AUDIO_RE.search(info)))


def _even(n: int) -> int:
    return n - (n % 2)


def build_collage(
    user_path: str,
    banner_path: str,
    ring_path: str,
    out_path: str,
    canvas_w: int = 1080,
    canvas_h: int = 1920,
    cut_ratio: float = 0.5,
    banner_width_ratio: float = 0.9,
    fps: int = 30,
    chroma_color: str = "0x00FF00",
    chroma_similarity: float = 0.2,
    chroma_blend: float = 0.1,
) -> str:
    user = probe(user_path)
    banner = probe(banner_path)

    W = _even(canvas_w)
    H = _even(canvas_h)
    HALF = _even(H // 2)          # высота верхней и нижней половины
    BW = _even(int(W * banner_width_ratio))

    D = round(user.duration, 3)           # длительность видео пользователя
    T = round(D * cut_ratio, 3)           # момент зависания
    REST = round(D - T, 3)                # сколько видео идёт после баннера
    B = round(banner.duration, 3)         # длительность баннера
    TOTAL = round(D + B, 3)               # итоговая длительность

    if T <= 0 or REST <= 0:
        raise CollageError("Видео слишком короткое для зависания посередине")

    parts = []

    # ---- Верх: видео пользователя, заполняет половину кадра ----
    parts.append(
        f"[0:v]fps={fps},scale={W}:{HALF}:force_original_aspect_ratio=increase,"
        f"crop={W}:{HALF},setsar=1,format=yuv420p,split=2[tA][tB]"
    )
    # Первая часть + зависание: последний кадр держим B секунд
    parts.append(
        f"[tA]trim=end={T},setpts=PTS-STARTPTS,"
        f"tpad=stop_mode=clone:stop_duration={B}[top1]"
    )
    # Продолжение после баннера
    parts.append(
        f"[tB]trim=start={T},setpts=PTS-STARTPTS[top2]"
    )
    parts.append("[top1][top2]concat=n=2:v=1:a=0[top]")

    # ---- Низ: фиолетовое кольцо, вписано в половину, крутится весь ролик ----
    parts.append(
        f"[2:v]fps={fps},scale={W}:{HALF}:force_original_aspect_ratio=decrease,"
        f"pad={W}:{HALF}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,format=yuv420p,"
        f"trim=end={TOTAL},setpts=PTS-STARTPTS[ring]"
    )

    # Склейка верх/низ в один кадр
    parts.append("[top][ring]vstack=inputs=2[base]")

    # ---- Баннер: хромакей, стартует ровно в момент T ----
    parts.append(
        f"[1:v]fps={fps},chromakey={chroma_color}:{chroma_similarity}:{chroma_blend},"
        f"format=yuva420p,scale={BW}:-2,setpts=PTS+{T}/TB[bk]"
    )
    parts.append(
        f"[base][bk]overlay=(W-w)/2:(H-h)/2:eof_action=pass:format=auto,"
        f"trim=end={TOTAL},setpts=PTS-STARTPTS,format=yuv420p[v]"
    )

    # ---- Звук: речь пользователя → тишина/звук баннера → продолжение ----
    if user.has_audio:
        parts.append(
            f"[0:a]atrim=end={T},asetpts=PTS-STARTPTS,"
            f"aresample=44100,aformat=channel_layouts=stereo[a1]"
        )
        parts.append(
            f"[0:a]atrim=start={T},asetpts=PTS-STARTPTS,"
            f"aresample=44100,aformat=channel_layouts=stereo[a2]"
        )
    else:
        parts.append(f"anullsrc=r=44100:cl=stereo:d={T}[a1]")
        parts.append(f"anullsrc=r=44100:cl=stereo:d={REST}[a2]")

    if banner.has_audio:
        parts.append(
            f"[1:a]atrim=end={B},asetpts=PTS-STARTPTS,"
            f"aresample=44100,aformat=channel_layouts=stereo[ab]"
        )
    else:
        parts.append(f"anullsrc=r=44100:cl=stereo:d={B}[ab]")

    parts.append("[a1][ab][a2]concat=n=3:v=0:a=1[a]")

    filter_complex = ";".join(parts)

    cmd = [
        get_ffmpeg(), "-y", "-v", "error",
        "-i", user_path,                       # 0: видео пользователя
        "-i", banner_path,                     # 1: баннер
        "-stream_loop", "-1", "-i", ring_path, # 2: кольцо (зациклено)
        "-filter_complex", filter_complex,
        "-map", "[v]", "-map", "[a]",
        "-t", str(TOTAL),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-maxrate", "5M", "-bufsize", "10M",  # ограничиваем битрейт, чтобы итог не перевалил за лимит 50 МБ
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "160k",
        "-movflags", "+faststart",
        out_path,
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise CollageError(f"ffmpeg упал: {res.stderr.strip()[-800:]}")
    return out_path
