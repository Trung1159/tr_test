"""Tạo video Reels dọc 9:16 từ ảnh sản phẩm và đăng lên Facebook Page.

Mỗi ảnh thành một cảnh ~3 giây: nền là chính ảnh đó làm mờ, ảnh gốc ở giữa,
hiệu ứng zoom chậm, chuyển cảnh mờ dần, chữ hook ở trên và lời kêu gọi ở dưới.
"""

import io
import os
import subprocess
import tempfile
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

WIDTH, HEIGHT = 1080, 1920
FPS = 30
SLIDE_SECONDS = 3.0
FADE_SECONDS = 0.5
MIN_SECONDS = 6.0
# Phóng to trước khi zoom để chuyển động mượt, không bị rung.
ZOOM_SCALE = 1.5
ZOOM_AMOUNT = 0.08

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]


def load_font(size: int) -> ImageFont.FreeTypeFont:
    for path in FONT_CANDIDATES:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    raise RuntimeError("Không tìm thấy font hỗ trợ tiếng Việt (cần cài fonts-dejavu-core).")


def wrap_text(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> list[str]:
    lines, line = [], ""
    for word in text.split():
        candidate = f"{line} {word}".strip()
        if draw.textlength(candidate, font=font) <= max_width or not line:
            line = candidate
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines


def draw_banner(img: Image.Image, text: str, font_size: int, center_y: int) -> None:
    if not text:
        return
    draw = ImageDraw.Draw(img, "RGBA")
    font = load_font(font_size)
    lines = wrap_text(draw, text, font, WIDTH - 160)
    line_height = int(font_size * 1.25)
    box_height = line_height * len(lines) + 50
    top = center_y - box_height // 2
    draw.rounded_rectangle((50, top, WIDTH - 50, top + box_height), radius=36, fill=(0, 0, 0, 150))
    y = top + 25
    for line in lines:
        w = draw.textlength(line, font=font)
        draw.text(((WIDTH - w) / 2, y), line, font=font, fill="white")
        y += line_height


def make_slide(image_bytes: bytes, headline: str, cta: str) -> Image.Image:
    photo = ImageOps.exif_transpose(Image.open(io.BytesIO(image_bytes))).convert("RGB")

    background = ImageOps.fit(photo, (WIDTH, HEIGHT))
    background = background.filter(ImageFilter.GaussianBlur(40))
    background = Image.eval(background, lambda v: int(v * 0.6))

    foreground = photo.copy()
    foreground.thumbnail((WIDTH - 80, int(HEIGHT * 0.62)))
    background.paste(foreground, ((WIDTH - foreground.width) // 2, (HEIGHT - foreground.height) // 2))

    draw_banner(background, headline, 64, 250)
    draw_banner(background, cta, 50, HEIGHT - 230)
    return background


def build_reel(images: list[bytes], headline: str, cta: str, out_path: Path,
               music: bytes | None = None) -> Path:
    """Ghép ảnh thành video MP4 1080x1920 và trả về đường dẫn file."""
    slide_seconds = max(SLIDE_SECONDS, MIN_SECONDS) if len(images) == 1 else SLIDE_SECONDS
    total = len(images) * slide_seconds - (len(images) - 1) * FADE_SECONDS
    frames = int(slide_seconds * FPS)

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        cmd = ["ffmpeg", "-y", "-loglevel", "error"]
        filters = []
        for i, data in enumerate(images):
            slide_path = tmp / f"slide_{i}.png"
            make_slide(data, headline, cta).save(slide_path)
            cmd += ["-loop", "1", "-framerate", str(FPS), "-t", f"{slide_seconds}", "-i", str(slide_path)]
            # Cảnh chẵn zoom vào, cảnh lẻ zoom ra.
            progress = f"on/{frames}" if i % 2 == 0 else f"(1-on/{frames})"
            filters.append(
                f"[{i}:v]scale={int(WIDTH * ZOOM_SCALE)}:{int(HEIGHT * ZOOM_SCALE)},"
                f"zoompan=z='1+{ZOOM_AMOUNT}*{progress}':d=1:"
                f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={WIDTH}x{HEIGHT}:fps={FPS},"
                f"setsar=1,format=yuv420p[v{i}]"
            )

        last = "v0"
        for i in range(1, len(images)):
            offset = i * (slide_seconds - FADE_SECONDS)
            filters.append(
                f"[{last}][v{i}]xfade=transition=fade:duration={FADE_SECONDS}:offset={offset}[x{i}]"
            )
            last = f"x{i}"

        audio_index = len(images)
        if music:
            music_path = tmp / "music"
            music_path.write_bytes(music)
            cmd += ["-stream_loop", "-1", "-i", str(music_path)]
            filters.append(
                f"[{audio_index}:a]atrim=0:{total},asetpts=N/SR/TB,"
                f"afade=t=in:d=0.5,afade=t=out:st={max(total - 1.5, 0)}:d=1.5[a]"
            )
        else:
            # Reels cần có kênh âm thanh; dùng âm thanh im lặng nếu không có nhạc.
            cmd += ["-f", "lavfi", "-t", f"{total}", "-i", "anullsrc=r=48000:cl=stereo"]
            filters.append(f"[{audio_index}:a]anull[a]")

        cmd += [
            "-filter_complex", ";".join(filters),
            "-map", f"[{last}]", "-map", "[a]",
            "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k", "-ar", "48000",
            "-t", f"{total}", "-movflags", "+faststart",
            str(out_path),
        ]
        subprocess.run(cmd, check=True)
    return out_path


def post_reel(video_path: Path, description: str, graph_url, check_fb) -> str:
    """Đăng Reels lên Page theo 3 bước: khởi tạo, tải video, xuất bản."""
    page_id = os.environ["FB_PAGE_ID"]
    token = os.environ["FB_PAGE_ACCESS_TOKEN"]

    start = check_fb(requests.post(
        graph_url(f"{page_id}/video_reels"),
        data={"upload_phase": "start", "access_token": token},
        timeout=60,
    ))
    video_id = start["video_id"]

    data = video_path.read_bytes()
    upload = requests.post(
        start["upload_url"],
        headers={
            "Authorization": f"OAuth {token}",
            "offset": "0",
            "file_size": str(len(data)),
        },
        data=data,
        timeout=600,
    )
    check_fb(upload)

    check_fb(requests.post(
        graph_url(f"{page_id}/video_reels"),
        data={
            "upload_phase": "finish",
            "video_id": video_id,
            "video_state": "PUBLISHED",
            "description": description,
            "access_token": token,
        },
        timeout=60,
    ))
    return video_id
