**[English](../README.md)** | **[Tiếng Việt](vi.md)**

![Cloudflare Gateway](https://github.com/luxysiv/Cloudflare-Gateway-Pihole/assets/46205571/b8b7b12b-2fd8-4978-8e3c-2472a4167acb)

# Cloudflare Gateway DNS Filter — Chặn + Cho phép

> Chặn quảng cáo theo kiểu Pihole bằng Cloudflare Gateway Zero Trust, kết hợp rule Allow ưu tiên cao hơn rule Block — tất cả trong một workflow duy nhất.

---

# Cài thời gian script tự động chạy

[![Deploy to Cloudflare Workers](https://deploy.workers.cloudflare.com/button)](https://deploy.workers.cloudflare.com/?url=https://github.com/luxysiv/cloudflare-gateway-pihole-trigger)

| Variable | Mô tả | Ví dụ |
| :--- | :--- | :--- |
| `GITHUB_TOKEN` | GitHub Personal Access Token (cần quyền Workflow, không hết hạn) | `ghp_xxxxxxxxxxxx` |
| `GITHUB_USER` | Tên tài khoản GitHub của bạn | `luxysiv` |
| `GITHUB_REPO` | Tên repository của bạn | `Cloudflare-Gateway-DNS-Filter` |
| `WORKFLOW_ID` | Tên file workflow | `main.yml` |

* Nên chọn **private repository** khi deploy.

* Sau khi deploy xong, bạn có thể xoá repository đã clone.

* Nếu nhận được email thông báo Github Action bị dừng, đừng lo — dùng Cloudflare Workers bên trên để script chạy mãi mãi.

---

## Cơ chế hoạt động

Script quản lý hai tập rule DNS trên Cloudflare Gateway trong một lần chạy duy nhất:

| Rule | Hành động | Độ ưu tiên | Nguồn dữ liệu |
| :--- | :--- | :--- | :--- |
| `[AdBlock-DNS-Filters] Block Ads` | block | 1000 | `adlist.ini` + `dynamic_blacklist.txt` |
| `[AdAllow-DNS-Filters] Allow` | allow | 999 *(ưu tiên cao hơn)* | `whitelist.ini` + `dynamic_whitelist.txt` |

Vì rule Allow có **số độ ưu tiên thấp hơn** (999 < 1000), Cloudflare đánh giá nó trước — tên miền được cho phép luôn được phân giải kể cả khi nằm trong danh sách chặn. Script không cần trừ whitelist khỏi blocklist nữa.

Script cũng kiểm tra **giới hạn 300 lists** của gói miễn phí Cloudflare Gateway (tính tổng cả block lẫn allow). Nếu vượt quá, script dừng lại trước khi gọi bất kỳ API nào.

---

## Hướng dẫn cài đặt

### 1. Fork repository này về tài khoản của bạn

### 2. Lấy thông tin xác thực Cloudflare

- **Account ID** — lấy từ URL: `https://dash.cloudflare.com/?to=/:account/workers`

- **API Token** — tạo tại `https://dash.cloudflare.com/profile/api-tokens` với 3 quyền:
  1. `Account.Zero Trust : Edit`
  2. `Account.Account Firewall Access Rules : Edit`
  3. `Account.Access: Apps and Policies : Edit`

Tạo `CF_API_TOKEN` như sau:

![CF_API_TOKEN](https://github.com/luxysiv/Cloudflare-Gateway-Pihole/assets/46205571/a5b90438-26cc-49ae-9a55-5409a90b683f)

### 3. Thêm Repository Secrets

Vào `https://github.com/<username>/Cloudflare-Gateway-DNS-Filter/settings/secrets/actions` và thêm:

| Secret | Giá trị |
| :--- | :--- |
| `CF_API_TOKEN` | API Token vừa tạo |
| `CF_IDENTIFIER` | Account ID của bạn |

`Secret Github Action` như sau:

![1000015672](https://github.com/luxysiv/Cloudflare-Gateway-Pihole/assets/46205571/6bd7f41d-0ca5-4944-95d3-d41dfd913c60)

### 4. Cấu hình danh sách

**Danh sách chặn** — chỉnh sửa [`lists/adlist.ini`](../lists/adlist.ini):
```ini
[Ad-Urls]
Adguard = https://adguardteam.github.io/AdGuardSDNSFilter/Filters/filter.txt
```

**Danh sách cho phép** — chỉnh sửa [`lists/whitelist.ini`](../lists/whitelist.ini):
```ini
[Allow-Urls]
MyAllow = https://example.com/my-whitelist.txt
```

Cả hai file đều chấp nhận URL thuần (mỗi dòng một URL) hoặc định dạng INI `[Section] key = url`.

### 5. (Tuỳ chọn) Thêm danh sách qua GitHub Actions Variables

Vào `https://github.com/<username>/Cloudflare-Gateway-DNS-Filter/settings/variables/actions` và thêm:

| Name | Value |
| :--- | :--- |
| `ADLIST_URLS` | Các URL blocklist cách nhau bằng dấu cách |
| `WHITELIST_URLS` | Các URL allowlist cách nhau bằng dấu cách |
| `DYNAMIC_BLACKLIST` | Tên miền chặn thêm, mỗi dòng một tên |
| `DYNAMIC_WHITELIST` | Tên miền cho phép thêm, mỗi dòng một tên |

* Bạn nên thêm danh sách tùy chỉnh vào Action variables để không bị mất khi pull cập nhật repo.

---

## Dành cho các bạn Việt Nam

Các bạn cần phân biệt **bộ lọc DNS** và **bộ lọc browser**. Nhiều bạn đem bộ lọc browser lên chạy dẫn đến lỗi lướt web — bộ lọc browser chứa các quy tắc CSS/cosmetic không dùng được cho DNS.

---

## Hướng dẫn sử dụng trên Termux

#### Cách 1:

1. Mở Termux và chạy lần lượt các lệnh sau:

```sh
yes | pkg upgrade
yes | pkg install python-pip
yes | pkg install git
git clone https://github.com/<username>/Cloudflare-Gateway-DNS-Filter.git
```

2. Di chuyển vào thư mục vừa clone:

```sh
cd Cloudflare-Gateway-DNS-Filter
```

3. Chỉnh file `.env` (bắt buộc):

```sh
nano .env
```

Sau khi chỉnh xong, nhấn `CTRL + X`, rồi `Y`, rồi `ENTER` để lưu.

4. Chạy lệnh để tải lên (cập nhật) danh sách DNS:

```sh
python -m src run
```

5. Chạy lệnh để xoá danh sách DNS:

```sh
python -m src leave
```

#### Cách 2:

1. Tải file ZIP của repository từ nút **Code → Download ZIP** trên GitHub.

2. Giải nén file vừa tải.

3. Chỉnh các giá trị trong `.env`, `lists/adlist.ini`, `lists/whitelist.ini`...

4. Mở Termux và chạy:

```sh
yes | pkg upgrade
yes | pkg install python-pip
termux-setup-storage
```

5. Cho phép Termux truy cập bộ nhớ.

6. Di chuyển vào thư mục chứa source code đã giải nén:

```sh
cd storage/downloads/Cloudflare-Gateway-DNS-Filter-main
```

7. Chạy lệnh để tải lên (cập nhật) danh sách DNS:

```sh
python -m src run
```

8. Chạy lệnh để xoá danh sách DNS:

```sh
python -m src leave
```

Nếu gặp lỗi trong quá trình cài đặt, tham khảo [termux-change-repo](https://wiki.termux.com/wiki/Package_Management) để đổi nguồn Termux.

---

## Chú ý

* **Giới hạn** của Cloudflare Gateway Zero Trust free là **300 lists** (block + allow cộng lại), mỗi list 1.000 tên miền — tối đa **300.000 domains**. Script tự dừng nếu vượt giới hạn.

* Các bạn đã tải danh sách bằng script khác thì nên xoá đi bằng tính năng xoá của script đó hoặc xoá tay.

* Để xoá toàn bộ lists và rules do script này tạo ra, vào **[main.yml](../.github/workflows/main.yml)** và đổi lệnh:

```yml
      - name: Cloudflare Gateway Zero Trust
        run: python -m src leave
```

* Hỗ trợ **[dynamic_blacklist.txt](../lists/dynamic_blacklist.txt)** và **[dynamic_whitelist.txt](../lists/dynamic_whitelist.txt)** để tự chặn hoặc bỏ chặn tên miền theo ý thích.

---

👌 Chúc các bạn thành công!

👌 Mọi thắc mắc về script, các bạn có thể mở issue.
