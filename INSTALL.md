# Cài GenOS MVP

## Target được chứng nhận

Bản MVP hiện chứng nhận **Ubuntu Server 24.04 LTS amd64, native dedicated server/VPS**. Không suy rộng thành mọi Linux hoặc workstation/VM chưa được fresh-host gate chứng nhận.

## Cài sau khi tải release

Release chính thức cung cấp tar archive, Git commit SHA và SHA-256 tương ứng. Sau khi tải archive và source bootstrap từ cùng release đã xác minh, chạy một lệnh:

```bash
sudo ./scripts/bootstrap.sh \
  --release ./genos-mvp-<version>.tar.gz \
  --sha256 <SHA256_TỪ_RELEASE> \
  --git-sha <40_HEX_GIT_SHA>
```

Bootstrap **verify SHA-256 trước khi chạy mã GenOS**, kiểm tra archive path/link/device safety, rồi gọi cùng `genos install` engine mà CI fresh-host dùng. Không có đường cài `curl | sudo bash` chạy payload chưa xác minh.

Khi core local sẵn sàng, mở Mission Control trên host:

```text
http://127.0.0.1:17882/
```

Normal first-run UX:

1. tạo Owner duy nhất;
2. đăng nhập Mission Control;
3. Agy-gen hiển thị `NEEDS_ACTION` nếu provider OAuth/model probe thật còn thiếu;
4. kết nối Google Drive bằng tài khoản Google của chính user trong Connections;
5. MCP Hub tự có một endpoint loopback bền vững; tạo Agent principal/token một lần và cấp scope ngay trong Console;
6. upstream MCP như GitHub/Google Drive được đăng ký tập trung trong Hub, Agent chỉ cần endpoint/token GenOS.

MCP Hub **không hard-code một port cố định**: installer chọn một port loopback không xung đột trong managed range, lưu tại `/etc/genos/mcp-port` và giữ nguyên qua restart/reboot/update/restore. Endpoint chính xác luôn xem trong **Connections & Credentials → Unified MCP Hub**; Agent ngoài chỉ cấu hình endpoint + one-time GenOS token ở đó, không cần cài credential/upstream MCP riêng trên từng Agent.

## Lifecycle

Các mutation cuối đều là typed command, không nhận arbitrary shell:

```bash
sudo genos backup --output /var/lib/genos/backups/manual.tar.gz --json
sudo genos restore --archive /var/lib/genos/backups/manual.tar.gz --sha256 <SHA256> --json
sudo genos update --release ./next.tar.gz --release-sha256 <SHA256> --git-sha <SHA> --json
sudo genos support-bundle --output /tmp/genos-support.tar.gz --json
sudo genos uninstall --json
sudo genos purge --confirm-instance-id <INSTANCE_UUID> --json
```

### Backup / restore

Backup mặc định **không chứa raw SecretProvider material**. Restore backup mặc định giữ nguyên secret hiện hữu trên máy và phục hồi Product DB/state/config. Chỉ `--include-secrets` mới đưa SecretProvider vào archive permission-restricted; archive đó phải được bảo vệ như credential vault.

### Update / rollback

Update luôn tạo checkpoint trước mutation, stage release đã verify, atomic cutover và health check. Nếu release mới không healthy, GenOS phục hồi **cả previous release lẫn DB/state checkpoint**, không chỉ đổi symlink.

### Uninstall / reinstall

`genos uninstall` xóa service units/release/generated config nhưng giữ durable data, secret material và một bản preserved non-secret identity config. Khi cài lại trên cùng host, bootstrap khôi phục `instance-id` và MCP port trước installer để không sinh một instance/endpoint mới ngoài ý muốn.

### Purge

`genos purge` là destructive local action riêng, cần nhập chính xác `instance_id`. Nó xóa GenOS local state/config/releases và Product DB/role. Google Drive/Cloudflare resource **không bị xóa từ xa**; mặc định chỉ unbind local.

## Kiểm tra sau cài

```bash
genos status --json
genos doctor --json
```

Mission Control, Product API, Worker, Runtime và MCP Hub phải recover qua reboot. Trạng thái thiếu external OAuth phải là `NEEDS_ACTION`, không giả `READY`.
