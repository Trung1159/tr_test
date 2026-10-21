"""Tự động đăng bài lên Facebook Page từ ảnh trong Google Drive.

Quy trình mỗi lần chạy:
  1. Liệt kê các thư mục con trong thư mục Drive gốc (mỗi thư mục con = 1 mẫu).
  2. Chọn mẫu đầu tiên chưa được đăng (theo thứ tự tên thư mục).
  3. Tải toàn bộ ảnh của mẫu đó (+ file .txt mô tả nếu có).
  4. Gửi ảnh cho Claude để viết caption bán hàng.
  5. Đăng bài nhiều ảnh lên Facebook Page.
  6. Ghi lại mẫu đã đăng vào posted.json để không đăng lại.
"""

import argparse
import base64
import io
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import anthropic
import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from PIL import Image

DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
IMAGE_MIME_PREFIX = "image/"
TEXT_MIME_TYPES = {"text/plain", "application/vnd.google-apps.document"}
FOLDER_MIME = "application/vnd.google-apps.folder"

# Giới hạn số ảnh mỗi bài để bài đăng gọn và tránh lỗi khi tải lên.
MAX_IMAGES_PER_POST = 10
# Ảnh gửi cho Claude được thu nhỏ để giảm chi phí và tránh vượt giới hạn kích thước.
CLAUDE_IMAGE_MAX_SIDE = 1568

STATE_FILE = Path(os.environ.get("STATE_FILE", Path(__file__).parent / "posted.json"))

CAPTION_SYSTEM_PROMPT = """Bạn là copywriter bán hàng online hàng đầu Việt Nam, chuyên viết bài đăng Facebook Page có tỷ lệ chuyển đổi cao.

Nhiệm vụ: nhìn ảnh sản phẩm (một mẫu, có thể nhiều góc chụp) và viết MỘT caption Facebook bằng tiếng Việt.

Yêu cầu caption:
- Mở đầu bằng 1 câu "hook" thật cuốn hút trong dòng đầu tiên (người đọc chỉ thấy 1-2 dòng trước khi bấm "Xem thêm").
- Mô tả sản phẩm sinh động dựa trên những gì THỰC SỰ thấy trong ảnh: màu sắc, chất liệu, kiểu dáng, chi tiết nổi bật. Không bịa thông số không có trong ảnh hoặc thông tin shop cung cấp.
- Nêu lợi ích / cảm xúc khi sở hữu, gợi ý hoàn cảnh sử dụng phù hợp.
- Tạo cảm giác khan hiếm hoặc gấp gáp một cách tự nhiên, không phóng đại sai sự thật.
- Kết thúc bằng lời kêu gọi hành động rõ ràng (inbox, comment, gọi điện... theo thông tin shop).
- Dùng emoji vừa phải để bài sinh động, chia đoạn ngắn dễ đọc trên điện thoại.
- Thêm 3-6 hashtag liên quan ở cuối.
- Độ dài khoảng 80-180 từ.
- Nếu có giá, khuyến mãi, size... trong thông tin shop cung cấp thì đưa vào chính xác.

Chỉ trả về nội dung caption, không giải thích, không đặt tiêu đề "Caption:"."""


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
# Google Drive
# ---------------------------------------------------------------------------

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


def pick_next_product(service, root_folder_id: str, state: dict) -> dict | None:
    """Trả về thư mục mẫu đầu tiên chưa đăng và có ít nhất 1 ảnh."""
    for item in list_children(service, root_folder_id):
        if item["mimeType"] != FOLDER_MIME or item["id"] in state["posted"]:
            continue
        children = list_children(service, item["id"])
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


def write_caption(product_name: str, images: list[bytes], notes: str) -> str:
    client = anthropic.Anthropic()

    shop_info = os.environ.get("SHOP_INFO", "").strip()
    context = [f"Tên thư mục mẫu: {product_name}"]
    if notes:
        context.append(f"Thông tin mẫu do shop cung cấp:\n{notes}")
    if shop_info:
        context.append(f"Thông tin chung của shop (dùng cho phần kêu gọi hành động):\n{shop_info}")
    context.append("Hãy viết caption cho bài đăng này.")

    content = [to_claude_image(b) for b in images]
    content.append({"type": "text", "text": "\n\n".join(context)})

    response = client.beta.messages.create(
        model=os.environ.get("CLAUDE_MODEL", "claude-opus-5-5"),
        max_tokens=16000,
        system=CAPTION_SYSTEM_PROMPT,
        output_config={"effort": "medium"},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        messages=[{"role": "user", "content": content}],
    )
    if response.stop_reason == "refusal":
        raise RuntimeError(f"Claude từ chối viết caption cho mẫu '{product_name}'.")

    caption = "".join(b.text for b in response.content if b.type == "text").strip()
    if not caption:
        raise RuntimeError("Claude trả về caption rỗng.")
    return caption


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
                        help="Chỉ lấy ảnh và viết caption, không đăng lên Facebook, không lưu trạng thái.")
    args = parser.parse_args()

    root_folder_id = os.environ["DRIVE_FOLDER_ID"]
    state = load_state()
    service = drive_service()

    product = pick_next_product(service, root_folder_id, state)
    if product is None:
        print("Không còn mẫu nào chưa đăng. Hãy thêm thư mục mẫu mới vào Drive.")
        return 0

    folder = product["folder"]
    print(f"Mẫu được chọn: {folder['name']} ({len(product['images'])} ảnh)")

    images = [(f["name"], download_file(service, f), f["mimeType"]) for f in product["images"]]
    notes = "\n".join(download_file(service, f).decode("utf-8", errors="ignore").strip()
                      for f in product["notes"]).strip()

    caption = write_caption(folder["name"], [data for _, data, _ in images], notes)
    print("----- CAPTION -----")
    print(caption)
    print("-------------------")

    if args.dry_run:
        print("Dry run: không đăng lên Facebook.")
        return 0

    post_id = post_to_facebook(caption, images)
    print(f"Đã đăng thành công: https://www.facebook.com/{post_id}")

    state["posted"][folder["id"]] = {
        "name": folder["name"],
        "post_id": post_id,
        "posted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "caption": caption,
    }
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
