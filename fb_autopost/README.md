# Tool tự động đăng bài lên Facebook Page

Mỗi ngày lúc **9:00 sáng (giờ VN)** tool sẽ tự:

1. Lấy **1 mẫu** chưa đăng từ thư mục Google Drive của bạn.
2. Gửi ảnh cho Claude để **viết caption bán hàng** hấp dẫn, có hook, CTA và hashtag.
3. **Đăng bài nhiều ảnh** lên Facebook Page.
4. **Tạo video Reels** dọc 9:16 từ ảnh của mẫu (zoom chậm, chuyển cảnh, chữ hook + lời kêu gọi, nhạc nền) và đăng lên Page cùng ngày.
5. Ghi lại mẫu đã đăng vào `posted.json` để không bao giờ đăng trùng.

Tool chạy trên **GitHub Actions**, không cần bật máy.

## Việc hằng ngày của bạn: chỉ thêm ảnh vào Drive

Mỗi mẫu là **một thư mục con** trong thư mục gốc, bên trong có 1 hoặc nhiều ảnh:

```
Ảnh bán hàng/              <- thư mục gốc
├── 001 - Váy hoa nhí/
│   ├── 1.jpg
│   ├── 2.jpg
│   └── info.txt           <- (không bắt buộc) giá, size, chất liệu...
├── 002 - Áo sơ mi trắng/
│   └── anh.jpg
├── nhac-nen-1.mp3         <- (không bắt buộc) nhạc nền cho Reels
└── ...
```

- Tool đăng theo **thứ tự tên thư mục** → nên đánh số 001, 002, ...
- File `.txt` hoặc Google Docs trong thư mục mẫu là thông tin sản phẩm cho Claude
  (ví dụ: `Giá 350k, size S-M-L, vải lụa`). Có thông tin này caption sẽ chốt đơn tốt hơn nhiều.
- Mỗi bài tối đa 10 ảnh. Ảnh nên có đuôi `.jpg`, `.png`, `.webp`.
- **Nhạc nền Reels:** đặt file `.mp3`/`.m4a` **trực tiếp trong thư mục gốc** (không phải trong thư mục mẫu).
  Mỗi ngày tool chọn ngẫu nhiên 1 bài. Không có nhạc thì video im lặng.
  Chỉ dùng nhạc miễn phí bản quyền (ví dụ YouTube Audio Library) để Facebook không tắt tiếng hay gỡ video.
- Video dài khoảng 3 giây mỗi ảnh (1 ảnh thì 6 giây).

## Cài đặt một lần

### 1. Chia sẻ thư mục Drive
Chuột phải thư mục gốc → **Chia sẻ** → *Quyền truy cập chung* → **Bất kỳ ai có đường liên kết** (Người xem).
Dán link thư mục vào `drive_folder` trong `fb_autopost/config.json`.

### 2. Thông tin shop
Điền `shop_info` trong `fb_autopost/config.json`, ví dụ:
`Inbox page hoặc gọi/Zalo 09xx.xxx.xxx. Ship COD toàn quốc, kiểm tra hàng trước khi nhận.`

### 3. Ba khoá bí mật trên GitHub
Repo → *Settings → Secrets and variables → Actions → New repository secret*:

| Tên | Lấy ở đâu |
|---|---|
| `ANTHROPIC_API_KEY` | [console.anthropic.com](https://console.anthropic.com/settings/keys) → *Create Key* (nạp tối thiểu khoảng 5 USD một lần; tool dùng Claude Haiku 5.5 nên mỗi bài chỉ khoảng 50đ, số tiền nạp đủ dùng nhiều năm) |
| `FB_PAGE_ID` | Page → *Giới thiệu* → *Minh bạch về Trang* → ID Trang |
| `FB_PAGE_ACCESS_TOKEN` | Xem bên dưới |

**Lấy Page access token:**
1. Vào [developers.facebook.com/apps](https://developers.facebook.com/apps) → *Tạo ứng dụng* → loại *Doanh nghiệp / Business*.
2. Mở [Graph API Explorer](https://developers.facebook.com/tools/explorer/), chọn app vừa tạo, thêm quyền
   `pages_show_list`, `pages_read_engagement`, `pages_manage_posts` → *Generate Access Token* → đăng nhập và chọn Page.
3. Ở ô *User or Page*, chọn Page của bạn → copy token.
4. Để token **không hết hạn**: dán token vào [Access Token Debugger](https://developers.facebook.com/tools/debug/accesstoken/)
   → *Extend Access Token* → quay lại Graph API Explorer với token dài hạn đó, chọn lại Page ở ô *User or Page* → copy token Page.

### 4. Chạy thử
Tab *Actions → Facebook daily post → Run workflow*, tích **dry_run** → xem caption trong log mà không đăng.
Video Reels được lưu ở mục *Artifacts → reel-video* của lần chạy đó (tải về xem trước, lưu 7 ngày).
Nếu thiếu cài đặt nào, log sẽ ghi rõ thiếu gì.

## Tuỳ chỉnh

- Đổi giờ đăng: sửa dòng `cron` trong `.github/workflows/fb-daily-post.yml` (giờ UTC, VN = UTC+7). Ví dụ 20:00 tối VN là `"0 13 * * *"`.
- Muốn đăng lại một mẫu: xoá mục của nó trong `posted.json`.
- Tắt Reels: đặt `"reel": false` trong `config.json`.
- Nếu đăng Reels lỗi, bài ảnh vẫn được tính là đã đăng; lỗi được ghi ở `reel_error` trong `posted.json` và lần chạy được đánh dấu đỏ.
- Chỉnh giọng văn caption: `CAPTION_SYSTEM_PROMPT` trong `autopost.py`.
- Muốn caption hay hơn (đắt hơn, khoảng 1.500–2.000đ/bài): đặt biến môi trường `CLAUDE_MODEL=claude-opus-5-5` trong bước *Create post* của workflow.
- Không muốn chia sẻ Drive công khai: tạo service account Google Cloud, chia sẻ thư mục cho email service account,
  rồi thêm secret `GOOGLE_SERVICE_ACCOUNT_JSON` (nội dung file JSON). Tool sẽ tự dùng cách này khi có secret.

## Chạy trên máy tính (không bắt buộc)

```bash
cd fb_autopost
pip install -r requirements.txt   # cần cài thêm ffmpeg
export ANTHROPIC_API_KEY=... FB_PAGE_ID=... FB_PAGE_ACCESS_TOKEN=...
python autopost.py --dry-run   # xem thử caption
python autopost.py             # đăng thật
```
