# Hướng dẫn bắt buộc cho Agent trong dự án

## Đọc trước mỗi task

Trước khi lập kế hoạch, gọi SubAgent hoặc sửa file cho **mỗi task mới từ người dùng**, Main Session phải mở và đọc toàn bộ [docs/agent_orchestration.md](docs/agent_orchestration.md). Không chỉ dựa vào trí nhớ hoặc bản tóm tắt từ task trước. Sau khi compact context hoặc tiếp tục task ở session khác, đọc lại tài liệu này trước khi làm tiếp. Tin nhắn bổ sung trong cùng task không cần đọc lại nếu tài liệu vẫn còn trong context và chưa thay đổi.

Nếu không đọc được tài liệu, báo rõ vấn đề; không tự suy diễn chính sách model hay gọi SubAgent trước khi khôi phục được hướng dẫn.

## Chính sách cốt lõi

- Main Session mong muốn: `gpt-6.1-sol`, effort `medium`.
- SubAgent thông thường: `gpt-6-luna`, effort `xhigh` (ExtraHigh).
- SubAgent cho việc quan trọng: `gpt-6.1-sol`, effort `high` hoặc `xhigh`, theo tiêu chí trong tài liệu.
- **Tuyệt đối không sử dụng Astra**, gồm mọi tên, alias hoặc biến thể có chứa `astra`; không dùng làm fallback.
- Người dùng cho phép Main chủ động sử dụng SubAgent theo tài liệu, không cần hỏi lại cho từng lần giao việc. Chỉ giao việc trong phạm vi task được yêu cầu và quyền thực thi hiện có.
- Main chịu trách nhiệm điều phối, tích hợp, kiểm chứng và trả lời cuối cùng. Không coi báo cáo của SubAgent là bằng chứng đã kiểm chứng.
- SubAgent phải đọc tài liệu điều phối, tuân thủ phạm vi được giao và không tự tạo agent con nếu Main chưa cho phép.

Các quy tắc này áp dụng cho dự án này, trong giới hạn chỉ dẫn ưu tiên cao hơn và khả năng công cụ thực tế. Markdown hướng dẫn hành vi; không tự thay đổi model đang chạy của Main Session.
