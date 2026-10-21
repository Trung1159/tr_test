# Tool tự động đăng bài lên Facebook Page

Mỗi ngày tool sẽ:

1. Lấy **1 mẫu** chưa đăng từ thư mục Google Drive của bạn.
2. Gửi ảnh cho Claude để **viết caption bán hàng** hấp dẫn, có hook, CTA và hashtag.
3. **Đăng bài nhiều ảnh** lên Facebook Page.
4. Ghi lại mẫu đã đăng vào `posted.json` để không bao giờ đăng trùng.

Tool chạy miễn phí trên **GitHub Actions** lúc 9:00 sáng (giờ VN) mỗi ngày — bạn không cần bật máy.

## 1. Sắp xếp thư mục Drive

Mỗi mẫu là **một thư mục con**, bên trong có 1 hoặc nhiều ảnh:

```
Ảnh bán hàng/              <- thư mục gốc (DRIVE_FOLDER_ID)
├── 001 - Váy hoa nhí/
│   ├── 1.jpg
│   ├── 2.jpg
│   └── info.txt           <- (không bắt buộc) giá, size, chất liệu...
├── 002 - Áo sơ mi trắng/
│   └── anh.jpg
└── ...
```

- Tool đăng theo **thứ tự tên thư mục** (nên đánh số 001, 002, ... để kiểm soát thứ tự).
- File `.txt` hoặc Google Docs trong thư mục mẫu sẽ được đưa cho Claude làm thông tin sản phẩm
  (ví dụ: `Giá 350k, size S-M-L, vải lụa, freeship đơn từ 500k`). Có thông tin này caption sẽ chính xác và chốt đơn tốt hơn nhiều.
- Mỗi bài tối đa 10 ảnh.

## 2. Chuẩn bị tài khoản

### Google Drive (service account)
1. Vào [Google Cloud Console](https://console.cloud.google.com/), tạo project, bật **Google Drive API**.
2. *IAM & Admin → Service Accounts* → tạo service account → tab *Keys* → *Add key → JSON* → tải file JSON về.
3. Mở thư mục gốc trên Drive → **Chia sẻ** cho email của service account (dạng `xxx@xxx.iam.gserviceaccount.com`) quyền **Người xem**.
4. `DRIVE_FOLDER_ID` là đoạn cuối của link thư mục: `https://drive.google.com/drive/folders/<DRIVE_FOLDER_ID>`.

### Facebook Page
1. Tạo app tại [developers.facebook.com](https://developers.facebook.com/) (loại *Business*).
2. Dùng [Graph API Explorer](https://developers.facebook.com/tools/explorer/) lấy **User token** với quyền
   `pages_show_list`, `pages_read_engagement`, `pages_manage_posts`.
3. Đổi sang token dài hạn rồi gọi `GET /me/accounts` để lấy **Page access token** (token này không hết hạn) và **Page ID**.

### Claude
Tạo API key tại [console.anthropic.com](https://console.anthropic.com/).

## 3. Cài đặt trên GitHub

Vào repo → *Settings → Secrets and variables → Actions*:

| Loại | Tên | Giá trị |
|---|---|---|
| Secret | `ANTHROPIC_API_KEY` | API key Claude |
| Secret | `GOOGLE_SERVICE_ACCOUNT_JSON` | Dán **toàn bộ nội dung** file JSON service account |
| Secret | `DRIVE_FOLDER_ID` | ID thư mục gốc |
| Secret | `FB_PAGE_ID` | ID của Page |
| Secret | `FB_PAGE_ACCESS_TOKEN` | Page access token |
| Variable | `SHOP_INFO` | (khuyến nghị) Thông tin chung của shop, ví dụ: `Inbox page hoặc gọi/Zalo 09xx.xxx.xxx. Ship COD toàn quốc, kiểm tra hàng trước khi nhận.` |

Workflow nằm ở `.github/workflows/fb-daily-post.yml` và cần được merge vào nhánh mặc định thì lịch hằng ngày mới chạy.

**Chạy thử:** tab *Actions → Facebook daily post → Run workflow*, tích **dry_run** để chỉ xem caption mà không đăng.

Đổi giờ đăng: sửa dòng `cron` trong workflow (giờ UTC, VN = UTC+7). Ví dụ 20:00 tối VN là `"0 13 * * *"`.

## 4. Chạy trên máy tính (không bắt buộc)

```bash
cd fb_autopost
pip install -r requirements.txt
export ANTHROPIC_API_KEY=... DRIVE_FOLDER_ID=... FB_PAGE_ID=... FB_PAGE_ACCESS_TOKEN=...
export GOOGLE_SERVICE_ACCOUNT_FILE=service_account.json
python autopost.py --dry-run   # xem thử caption
python autopost.py             # đăng thật
```

## Ghi chú

- Muốn đăng lại một mẫu: xoá mục của nó trong `posted.json`.
- Hết mẫu chưa đăng thì tool chỉ báo "Không còn mẫu nào" và không đăng gì.
- Có thể chỉnh giọng văn caption trong `CAPTION_SYSTEM_PROMPT` ở `autopost.py`.
