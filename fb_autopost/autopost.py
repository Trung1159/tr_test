"""Tự động đăng bài lên Facebook Page từ ảnh trong Google Drive.

Quy trình mỗi lần chạy:
  1. Liệt kê các thư mục con trong thư mục Drive gốc (mỗi thư mục con = 1 mẫu).
  2. Chọn mẫu đầu tiên chưa được đăng (theo thứ tự tên thư mục).
  3. Tải toàn bộ ảnh của mẫu đó (+ file .txt mô tả nếu có).
  4. Gửi ảnh cho Claude để viết caption bán hàng (bài ảnh + Reels).
  5. Đăng bài nhiều ảnh lên Facebook Page.
  6. Ghép ảnh thành video Reels 9:16 và đăng lên Page.
  7. Ghi lại mẫu đã đăng vào posted.json để không đăng lại.
"""

import argparse
import base64
import html
import io
import json
import mimetypes
import os
import random
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import anthropic
import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from PIL import Image

import reel

DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
IMAGE_MIME_PREFIX = "image/"
TEXT_MIME_TYPES = {"text/plain", "application/vnd.google-apps.document"}
FOLDER_MIME = "application/vnd.google-apps.folder"

# Giới hạn số ảnh mỗi bài để bài đăng gọn và tránh lỗi khi tải lên.
MAX_IMAGES_PER_POST = 10
# Ảnh gửi cho Claude được thu nhỏ để giảm chi phí và tránh vượt giới hạn kích thước.
CLAUDE_IMAGE_MAX_SIDE = 1568

STATE_FILE = Path(os.environ.get("STATE_FILE", Path(__file__).parent / "posted.json"))
CONFIG_FILE = Path(__file__).parent / "config.json"
OUTPUT_DIR = Path(__file__).parent / "output"

CAPTION_SYSTEM_PROMPT = """Bạn là copywriter bán hàng online hàng đầu Việt Nam, chuyên viết nội dung Facebook Page có tỷ lệ chuyển đổi cao.

Nhiệm vụ: nhìn ảnh sản phẩm (một mẫu, có thể nhiều góc chụp) và viết nội dung tiếng Việt cho cả bài đăng ảnh lẫn video Reels của mẫu đó.

post_caption - caption bài đăng ảnh:
- Mở đầu bằng 1 câu "hook" thật cuốn hút trong dòng đầu tiên (người đọc chỉ thấy 1-2 dòng trước khi bấm "Xem thêm").
- Mô tả sản phẩm sinh động dựa trên những gì THỰC SỰ thấy trong ảnh: màu sắc, chất liệu, kiểu dáng, chi tiết nổi bật. Không bịa thông số không có trong ảnh hoặc thông tin shop cung cấp.
- Nêu lợi ích / cảm xúc khi sở hữu, gợi ý hoàn cảnh sử dụng phù hợp.
- Tạo cảm giác khan hiếm hoặc gấp gáp một cách tự nhiên, không phóng đại sai sự thật.
- Kết thúc bằng lời kêu gọi hành động rõ ràng (inbox, comment, gọi điện... theo thông tin shop).
- Dùng emoji vừa phải để bài sinh động, chia đoạn ngắn dễ đọc trên điện thoại.
- Thêm 3-6 hashtag liên quan ở cuối.
- Độ dài khoảng 80-180 từ.
- Nếu có giá, khuyến mãi, size... trong thông tin shop cung cấp thì đưa vào chính xác.

reel_caption - mô tả cho video Reels:
- Ngắn gọn 1-3 câu (dưới 40 từ), giọng trẻ trung, có emoji, khác với post_caption.
- Có lời kêu gọi hành động và 3-5 hashtag (có #reels).

video_headline - chữ hiển thị phía trên video:
- Tối đa 8 từ, gây tò mò hoặc nêu điểm nổi bật/giá. KHÔNG dùng emoji (font video không hiển thị được).

video_cta - chữ hiển thị phía dưới video:
- Tối đa 8 từ, kêu gọi đặt hàng. KHÔNG dùng emoji."""

CONTENT_SCHEMA = {
    "type": "object",
    "properties": {
        "post_caption": {"type": "string"},
        "reel_caption": {"type": "string"},
        "video_headline": {"type": "string"},
        "video_cta": {"type": "string"},
    },
    "required": ["post_caption", "reel_caption", "video_headline", "video_cta"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------------------
# Trạng thái đã đăng
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"posted": {}}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Cấu hình
# ---------------------------------------------------------------------------

def load_config() -> dict:
    """Đọc config.json; biến môi trường cùng tên (viết hoa) được ưu tiên hơn."""
    config = json.loads(CONFIG_FILE.read_text(encoding="utf-8")) if CONFIG_FILE.exists() else {}
    for key in ("drive_folder", "shop_info"):
        env = os.environ.get(key.upper(), "").strip()
        if env:
            config[key] = env
    return config


def folder_id_from(value: str) -> str:
    """Nhận link thư mục Drive hoặc ID, trả về ID."""
    match = re.search(r"/folders/([\w-]+)", value) or re.search(r"[?&]id=([\w-]+)", value)
    return match.group(1) if match else value.strip()


def check_setup(config: dict, dry_run: bool) -> None:
    missing = []
    if not config.get("drive_folder"):
        missing.append("drive_folder trong fb_autopost/config.json (link thư mục Drive)")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        missing.append("secret ANTHROPIC_API_KEY")
    if not dry_run:
        for name in ("FB_PAGE_ID", "FB_PAGE_ACCESS_TOKEN"):
            if not os.environ.get(name):
                missing.append(f"secret {name}")
    if missing:
        raise SystemExit("Thiếu cài đặt:\n  - " + "\n  - ".join(missing))


# ---------------------------------------------------------------------------
# Google Drive
#
# Mặc định đọc thư mục được chia sẻ "Bất kỳ ai có đường liên kết" mà không cần
# tài khoản Google Cloud. Nếu có GOOGLE_SERVICE_ACCOUNT_JSON/FILE thì dùng Drive API.
# ---------------------------------------------------------------------------

class PublicDrive:
    LIST_URL = "https://drive.google.com/embeddedfolderview?id={id}"
    DOWNLOAD_URLS = (
        "https://drive.usercontent.google.com/download?id={id}&export=download&confirm=t",
        "https://drive.google.com/uc?export=download&id={id}",
    )
    ENTRY_RE = re.compile(
        r'<a href="(?P<href>[^"]+)"[^>]*>.*?<div class="flip-entry-title">(?P<name>.*?)</div>',
        re.S,
    )

    def list_children(self, folder_id: str) -> list[dict]:
        resp = requests.get(self.LIST_URL.format(id=folder_id), timeout=60)
        if resp.status_code != 200:
            raise RuntimeError(
                f"Không đọc được thư mục Drive {folder_id} (HTTP {resp.status_code}). "
                "Hãy chia sẻ thư mục ở chế độ 'Bất kỳ ai có đường liên kết đều có thể xem'."
            )
        files = []
        for m in self.ENTRY_RE.finditer(resp.text):
            href, name = html.unescape(m.group("href")), html.unescape(m.group("name")).strip()
            if folder := re.search(r"/folders/([\w-]+)", href):
                files.append({"id": folder.group(1), "name": name, "mimeType": FOLDER_MIME})
            elif doc := re.search(r"/document/d/([\w-]+)", href):
                files.append({"id": doc.group(1), "name": name,
                              "mimeType": "application/vnd.google-apps.document"})
            elif f := re.search(r"/file/d/([\w-]+)", href):
                mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
                files.append({"id": f.group(1), "name": name, "mimeType": mime})
        return sorted(files, key=lambda f: f["name"])

    def download(self, file: dict) -> bytes:
        if file["mimeType"] == "application/vnd.google-apps.document":
            urls = [f"https://docs.google.com/document/d/{file['id']}/export?format=txt"]
        else:
            urls = [u.format(id=file["id"]) for u in self.DOWNLOAD_URLS]
        for url in urls:
            resp = requests.get(url, timeout=120)
            content_type = resp.headers.get("Content-Type", "")
            if resp.status_code == 200 and "text/html" not in content_type:
                return resp.content
        raise RuntimeError(f"Không tải được file '{file['name']}' từ Drive.")


class ApiDrive:
    def __init__(self):
        self.service = drive_service()

    def list_children(self, folder_id: str) -> list[dict]:
        return list_children(self.service, folder_id)

    def download(self, file: dict) -> bytes:
        return download_file(self.service, file)


def pick_music(drive, root_folder_id: str) -> bytes | None:
    """Chọn ngẫu nhiên 1 file nhạc đặt trực tiếp trong thư mục gốc (nếu có)."""
    tracks = [f for f in drive.list_children(root_folder_id) if f["mimeType"].startswith("audio/")]
    if not tracks:
        return None
    track = random.choice(tracks)
    print(f"Nhạc nền: {track['name']}")
    return drive.download(track)


def make_drive():
    if os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON") or os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE"):
        return ApiDrive()
    return PublicDrive()


def drive_service():
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if raw:
        info = json.loads(raw)
        creds = service_account.Credentials.from_service_account_info(info, scopes=DRIVE_SCOPES)
    else:
        path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json")
        creds = service_account.Credentials.from_service_account_file(path, scopes=DRIVE_SCOPES)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def list_children(service, folder_id: str) -> list[dict]:
    files, page_token = [], None
    while True:
        resp = service.files().list(
            q=f"'{folder_id}' in parents and trashed = false",
            fields="nextPageToken, files(id, name, mimeType, createdTime)",
            orderBy="name",
            pageSize=1000,
            pageToken=page_token,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        files.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            return files


def download_file(service, file: dict) -> bytes:
    if file["mimeType"] == "application/vnd.google-apps.document":
        request = service.files().export_media(fileId=file["id"], mimeType="text/plain")
    else:
        request = service.files().get_media(fileId=file["id"], supportsAllDrives=True)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buf.getvalue()


def pick_next_product(drive, root_folder_id: str, state: dict) -> dict | None:
    """Trả về thư mục mẫu đầu tiên chưa đăng và có ít nhất 1 ảnh."""
    for item in drive.list_children(root_folder_id):
        if item["mimeType"] != FOLDER_MIME or item["id"] in state["posted"]:
            continue
        children = drive.list_children(item["id"])
        images = [f for f in children if f["mimeType"].startswith(IMAGE_MIME_PREFIX)]
        if not images:
            print(f"Bỏ qua thư mục '{item['name']}': không có ảnh.")
            continue
        notes = [f for f in children if f["mimeType"] in TEXT_MIME_TYPES]
        return {"folder": item, "images": images[:MAX_IMAGES_PER_POST], "notes": notes}
    return None


# ---------------------------------------------------------------------------
# Claude: viết caption
# ---------------------------------------------------------------------------

def to_claude_image(data: bytes) -> dict:
    img = Image.open(io.BytesIO(data))
    img = img.convert("RGB")
    img.thumbnail((CLAUDE_IMAGE_MAX_SIDE, CLAUDE_IMAGE_MAX_SIDE))
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=85)
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/jpeg",
            "data": base64.standard_b64encode(out.getvalue()).decode("utf-8"),
        },
    }


def write_content(product_name: str, images: list[bytes], notes: str, shop_info: str) -> dict:
    """Trả về dict gồm post_caption, reel_caption, video_headline, video_cta."""
    client = anthropic.Anthropic()

    context = [f"Tên thư mục mẫu: {product_name}"]
    if notes:
        context.append(f"Thông tin mẫu do shop cung cấp:\n{notes}")
    if shop_info:
        context.append(f"Thông tin chung của shop (dùng cho phần kêu gọi hành động):\n{shop_info}")
    context.append("Hãy viết nội dung cho bài đăng ảnh và video Reels của mẫu này.")

    content = [to_claude_image(b) for b in images]
    content.append({"type": "text", "text": "\n\n".join(context)})

    # Haiku là model rẻ nhất (~50đ/bài); đặt CLAUDE_MODEL=claude-opus-5-5 nếu muốn caption tốt hơn.
    response = client.messages.create(
        model=os.environ.get("CLAUDE_MODEL", "claude-haiku-5-5"),
        max_tokens=16000,
        system=CAPTION_SYSTEM_PROMPT,
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": CONTENT_SCHEMA}},
        messages=[{"role": "user", "content": content}],
    )
    if response.stop_reason == "refusal":
        raise RuntimeError(f"Claude từ chối viết caption cho mẫu '{product_name}'.")

    text = next((b.text for b in response.content if b.type == "text"), "")
    result = {k: v.strip() for k, v in json.loads(text).items()}
    if not result["post_caption"]:
        raise RuntimeError("Claude trả về caption rỗng.")
    return result


# ---------------------------------------------------------------------------
# Facebook Graph API
# ---------------------------------------------------------------------------

def graph_url(path: str) -> str:
    version = os.environ.get("FB_GRAPH_VERSION", "v23.0")
    return f"https://graph.facebook.com/{version}/{path}"


def check_fb(resp: requests.Response) -> dict:
    data = resp.json()
    if resp.status_code >= 400 or "error" in data:
        raise RuntimeError(f"Facebook API lỗi: {data.get('error', data)}")
    return data


def post_to_facebook(caption: str, images: list[tuple[str, bytes, str]]) -> str:
    page_id = os.environ["FB_PAGE_ID"]
    token = os.environ["FB_PAGE_ACCESS_TOKEN"]

    # Bước 1: tải từng ảnh lên ở trạng thái chưa xuất bản.
    media_ids = []
    for name, data, mime in images:
        resp = requests.post(
            graph_url(f"{page_id}/photos"),
            data={"published": "false", "access_token": token},
            files={"source": (name, data, mime)},
            timeout=120,
        )
        media_ids.append(check_fb(resp)["id"])

    # Bước 2: tạo bài đăng gắn tất cả ảnh kèm caption.
    payload = {"message": caption, "access_token": token}
    for i, media_id in enumerate(media_ids):
        payload[f"attached_media[{i}]"] = json.dumps({"media_fbid": media_id})
    resp = requests.post(graph_url(f"{page_id}/feed"), data=payload, timeout=120)
    return check_fb(resp)["id"]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Tự động đăng 1 mẫu lên Facebook Page.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Chỉ viết caption và tạo video, không đăng lên Facebook, không lưu trạng thái.")
    args = parser.parse_args()

    config = load_config()
    check_setup(config, args.dry_run)
    root_folder_id = folder_id_from(config["drive_folder"])
    state = load_state()
    drive = make_drive()

    product = pick_next_product(drive, root_folder_id, state)
    if product is None:
        print("Không còn mẫu nào chưa đăng. Hãy thêm thư mục mẫu mới vào Drive.")
        return 0

    folder = product["folder"]
    print(f"Mẫu được chọn: {folder['name']} ({len(product['images'])} ảnh)")

    images = [(f["name"], drive.download(f), f["mimeType"]) for f in product["images"]]
    notes = "\n".join(drive.download(f).decode("utf-8", errors="ignore").strip()
                      for f in product["notes"]).strip()

    content = write_content(folder["name"], [data for _, data, _ in images], notes,
                            config.get("shop_info", "").strip())
    print("----- CAPTION BÀI ĐĂNG -----")
    print(content["post_caption"])
    print("----- REELS -----")
    print(f"Chữ trên video: {content['video_headline']} | {content['video_cta']}")
    print(content["reel_caption"])
    print("-----------------")

    video_path = None
    if config.get("reel", True):
        OUTPUT_DIR.mkdir(exist_ok=True)
        music = pick_music(drive, root_folder_id)
        video_path = reel.build_reel(
            [data for _, data, _ in images], content["video_headline"], content["video_cta"],
            OUTPUT_DIR / f"reel_{folder['id']}.mp4", music,
        )
        print(f"Đã tạo video Reels: {video_path}")

    if args.dry_run:
        print("Dry run: không đăng lên Facebook.")
        return 0

    post_id = post_to_facebook(content["post_caption"], images)
    print(f"Đã đăng bài ảnh: https://www.facebook.com/{post_id}")

    entry = {
        "name": folder["name"],
        "post_id": post_id,
        "posted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "caption": content["post_caption"],
    }
    # Lưu ngay sau khi đăng bài ảnh để lỗi Reels không làm bài ảnh bị đăng lại hôm sau.
    state["posted"][folder["id"]] = entry
    save_state(state)

    if video_path:
        try:
            reel_id = reel.post_reel(video_path, content["reel_caption"], graph_url, check_fb)
        except Exception as exc:
            entry["reel_error"] = str(exc)
            save_state(state)
            print(f"Đăng Reels thất bại: {exc}")
            return 1
        entry["reel_id"] = reel_id
        entry["reel_caption"] = content["reel_caption"]
        save_state(state)
        print(f"Đã đăng Reels: https://www.facebook.com/reel/{reel_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
