# Hướng dẫn Main Session điều phối SubAgent

## 1. Mục tiêu và phạm vi

Main Session giữ mục tiêu tổng thể, chia việc, quyết định các điểm quan trọng, tích hợp và kiểm chứng kết quả. Chủ động dùng SubAgent cho những phần việc tách được để Main không phải tự thực hiện tất cả. Không chia một thao tác nhỏ thành nhiều agent chỉ để có delegation.

Đây là chính sách do người dùng lựa chọn cho dự án 3DSG. `AGENTS.md` ở gốc dự án yêu cầu Main đọc tài liệu này ở mỗi task mới và sau khi compact hoặc chuyển session. Việc đọc lại mỗi task là quy tắc của dự án, không phải khẳng định ứng dụng tự nạp lại file ở mỗi tin nhắn.

## 2. Model và reasoning effort

| Vai trò / loại công việc | Model | Effort | Khi sử dụng |
| --- | --- | --- | --- |
| Main Session | `gpt-6.1-sol` | `medium` | Điều phối, ra quyết định, tích hợp và phản hồi |
| SubAgent thông thường | `gpt-6-luna` | `xhigh` | Đọc code, tìm nguyên nhân cục bộ, triển khai phần độc lập, viết hoặc chạy test phù hợp, tài liệu, phân tích log |
| SubAgent quan trọng | `gpt-6.1-sol` | `high` | Thiết kế giao diện giữa module, sửa lỗi ảnh hưởng nhiều thành phần, đánh giá thay đổi rủi ro cao |
| SubAgent chuyên sâu | `gpt-6.1-sol` | `xhigh` | Lỗi khó tái hiện, concurrency, bất đồng kỹ thuật chưa giải quyết, lập luận thuật toán hoặc kiểm chứng phức tạp |

- ExtraHigh tương ứng giá trị công cụ `xhigh`, không phải `extra_high` hay `max`.
- Cấm mọi model có tên hoặc alias chứa `astra` (không phân biệt chữ hoa/thường), ở mọi vai trò, kể cả reviewer, agent con hoặc fallback.
- Không tự hạ Luna xuống effort thấp hơn để tiết kiệm. Có thể dùng nhiều Luna khi thực sự có phần việc độc lập và giới hạn runtime cho phép.
- Không mặc định dùng mức `max` hoặc `ultra`. Dùng Sol High trước; nâng lên Sol ExtraHigh nếu độ khó hoặc bằng chứng thất bại cho thấy cần thiết.
- Không suy đoán model mới hoặc alias chưa được công cụ hỗ trợ. Kiểm tra danh sách model/effort thực tế trước khi gọi.
- Nếu Luna không khả dụng, có thể dùng Sol High cho phần việc cần giao, nêu ngắn gọn lý do. Nếu Sol không khả dụng cho phần việc quan trọng, Main tự xử lý khi đủ khả năng; nếu không, báo hạn chế. Không âm thầm hạ phần việc quan trọng sang Luna và không dùng Astra.
- Nếu không đặt được model/effort rõ ràng hoặc không biết model sẽ được kế thừa, không spawn bằng mặc định không xác định. Main tiếp tục phần việc có thể làm và báo giới hạn cụ thể.

## 3. Quy trình bắt đầu mỗi task

1. Đọc lại tài liệu này cùng các chỉ dẫn áp dụng cho thư mục sẽ làm việc.
2. Xác định yêu cầu, tiêu chí hoàn thành, ràng buộc và những thay đổi hiện có của người dùng. Không ghi đè công việc đang dở.
3. Kiểm tra model/effort Main nếu runtime có cung cấp; không đoán hoặc tuyên bố đã đổi model khi chưa có xác nhận. Nếu Main khác cấu hình mong muốn, báo ngắn gọn. Nếu runtime xác nhận Main là Astra, dừng xử lý task và yêu cầu chuyển model phù hợp.
4. Chia phần việc theo kết quả cần đạt và quan hệ phụ thuộc. Chọn phần Main giữ, phần giao Luna, phần cần Sol.
5. Với task đủ lớn, thông báo ngắn gọn cách chia việc và model dự kiến. Với task nhỏ, làm trực tiếp nếu chi phí giao việc lớn hơn lợi ích.

Main không cần xin phép lại để giao việc thường lệ hoặc nâng SubAgent lên Sol High/ExtraHigh trong chính sách này. Việc giao SubAgent không mở rộng quyền truy cập, quyền gửi tin nhắn, quyền triển khai hay phạm vi task.

## 4. Cách chia và giao việc

- Ưu tiên các nhánh độc lập: đọc module khác nhau, điều tra các giả thuyết khác nhau, triển khai các file riêng biệt hoặc review một bản thay đổi đã ổn định.
- Những bước phụ thuộc kết quả nhau phải chạy theo thứ tự. Không cho reviewer kiểm tra một file đang được agent khác sửa mà không xác định rõ phiên bản.
- Mỗi file có một agent sở hữu quyền sửa tại một thời điểm. Main xử lý thay đổi chung hoặc tuần tự chuyển quyền sở hữu. Các agent đọc cùng file được phép.
- Nêu rõ file/thư mục được sửa, phạm vi chỉ đọc và vùng không được đụng tới. Không tự commit, reset, xóa thay đổi của người khác hoặc chạy tác vụ có tác dụng phụ ngoài phạm vi.
- Với test, benchmark hoặc simulation dùng chung GPU, port, output folder, dataset hay trạng thái runtime: tách tài nguyên/output hoặc chạy tuần tự. Tránh kết quả đè nhau và tranh chấp tài nguyên.
- Không giao cùng một nhiệm vụ cho nhiều agent trừ khi cần kiểm chứng độc lập cho phần quan trọng; khi đó nêu rõ mục đích.
- Tôn trọng số slot runtime hiện tại, tính cả Main. Nếu chỉ có 4 slot tổng cộng, tối đa 3 SubAgent cùng lúc; không coi đây là giới hạn cố định của mọi môi trường.
- Mặc định Main là nơi duy nhất spawn agent. SubAgent chỉ được tạo agent con khi Main cho phép rõ ràng và phải truyền tiếp chính sách cấm Astra, chọn model và giới hạn tài nguyên.
- Trong lúc SubAgent làm việc, Main xử lý phần độc lập còn lại. Tránh làm lại đúng phần đã giao; chờ kết quả khi thật sự có phụ thuộc.

Mẫu giao việc:

```text
Vai trò: [triển khai / điều tra / kiểm thử / review]
Model và effort: [model ID, effort]
Đọc trước: AGENTS.md và docs/agent_orchestration.md tại gốc dự án.
Mục tiêu: [kết quả cụ thể, liên hệ với mục tiêu tổng thể]
Bối cảnh: [quyết định đã chốt, dữ kiện và đường dẫn cần đọc]
Được sửa: [danh sách file hoặc thư mục; ghi 'không' nếu chỉ đọc]
Chỉ đọc / không được sửa: [phạm vi]
Phụ thuộc và phối hợp: [agent khác, giao diện, quyền sở hữu file]
Tiêu chí hoàn thành: [hành vi hoặc bằng chứng cần có]
Kiểm chứng: [test/check phù hợp; tài nguyên và output riêng nếu cần]
Giới hạn: không Astra; không tự spawn agent con; không sửa ngoài phạm vi.
Trả về: kết quả, file đã đổi, lệnh kiểm chứng và kết quả thực tế,
        điểm chưa chắc chắn, blocker, đề xuất bước tiếp theo.
```

## 5. Đặt model khi dùng công cụ hiện tại

Trong môi trường có `collaboration.spawn_agent`, phải truyền model và effort rõ ràng. Theo schema hiện tại, override model/effort yêu cầu `fork_turns="none"`; vì vậy Main phải đưa đủ bối cảnh vào `message`, không trông chờ agent nhận toàn bộ lịch sử.

Ví dụ tham số cho SubAgent thông thường:

```json
{
  "task_name": "inspect_module",
  "fork_turns": "none",
  "model": "gpt-6-luna",
  "reasoning_effort": "xhigh",
  "message": "Đọc AGENTS.md và docs/agent_orchestration.md tại gốc dự án. Đây là nhiệm vụ chỉ đọc: ... Mục tiêu: ... Phạm vi: ... Bằng chứng cần trả về: ... Không sử dụng Astra; không tự spawn agent con."
}
```

Với phần quan trọng, đổi thành `gpt-6.1-sol` và `high` hoặc `xhigh`. Điền đầy đủ nội dung nhiệm vụ trước khi gọi; ví dụ trên chỉ là mẫu.

Nếu schema thay đổi, theo schema thực tế và giữ nguyên chính sách model. Không chuyển sang tạo chat mới của người dùng để thay thế SubAgent nếu người dùng chưa yêu cầu tạo chat. Khi tái sử dụng agent, kiểm tra model/effort của agent đó phù hợp nhiệm vụ mới; nếu không phù hợp, tạo agent đúng cấu hình trong giới hạn cho phép.

## 6. Khi nào nâng cấp sang Sol

Chuyển phần việc sang Sol High khi có một trong các dấu hiệu:

- Thay đổi kiến trúc, protocol hoặc contract giữa nhiều module.
- Nguy cơ mất dữ liệu, lỗi bảo mật, sai định danh, race condition hoặc kết quả sai nhưng khó nhận thấy.
- Thay đổi thuật toán cốt lõi hoặc cách đánh giá ảnh hưởng độ tin cậy của kết quả nghiên cứu.
- Luna báo không đủ tự tin với bằng chứng cụ thể, hoặc đã thử hướng sửa có cơ sở nhưng vẫn không giải quyết được lỗi.

Với 3DSG, các thay đổi liên quan đồng bộ map/graph, identity mapping, transport protocol, concurrency và shutdown có thể thuộc nhóm này tùy phạm vi ảnh hưởng. Không nâng model chỉ vì tên file; đánh giá rủi ro thay đổi thực tế.

Khi chuyển giao, gửi triệu chứng, cách tái hiện, log liên quan, giả thuyết đã loại và diff hiện tại. Không bắt agent mới điều tra lại từ đầu. Nếu Sol High vẫn thiếu căn cứ hoặc bài toán cần suy luận sâu, dùng Sol ExtraHigh. Tránh vòng lặp retry cùng cách làm mà không có thông tin mới.

## 7. Tích hợp và kiểm chứng

Main phải:

1. Đọc kết quả và diff thực tế; kiểm tra agent có sửa ngoài phạm vi hay làm mất thay đổi hiện có không.
2. Đối chiếu tiêu chí hoàn thành, interface và giả định giữa các phần việc.
3. Chạy kiểm chứng tích hợp phù hợp sau khi ghép các thay đổi. Tận dụng kết quả test đã có khi vẫn đúng với phiên bản hiện tại; không lặp test tốn kém vô cớ.
4. Với phần quan trọng, có review độc lập bởi Sol High/ExtraHigh hoặc Main kiểm tra trực tiếp đủ sâu với bằng chứng. Reviewer không chỉ lặp lại nhận xét của người triển khai.
5. Nếu test thất bại, giao sửa đúng nguyên nhân và kiểm tra lại. Nếu không thể chạy do môi trường hoặc thiếu dữ liệu, ghi rõ chưa kiểm chứng; không báo pass.
6. Thu nhận hoặc dừng các agent còn làm việc liên quan trước khi kết luận. Không để agent tiếp tục sửa file sau khi Main báo hoàn tất.

Task chỉ sửa tài liệu hoặc thay đổi nhỏ có thể chỉ cần kiểm tra nội dung, liên kết và diff; không bắt buộc viết test vô ích.

## 8. Báo cáo và sử dụng lâu dài

Trả lời người dùng bằng tiếng Việt trừ khi được yêu cầu khác. Nêu kết quả, thay đổi chính, kiểm chứng đã thực hiện và hạn chế còn lại. Chỉ báo việc điều phối/model khi có ích hoặc có ngoại lệ; không đưa toàn bộ log agent vào phản hồi.

Để dùng Main đúng cấu hình mong muốn, chọn **GPT-6.1 Sol + Medium** trong session bằng cơ chế cấu hình mà ứng dụng cung cấp. Tài liệu và `AGENTS.md` không tự đổi model/effort đang chạy và không phải cơ chế chặn model ở cấp runtime.

Bộ hướng dẫn hiện áp dụng trong dự án này. Để tái sử dụng ở dự án khác, mang cả `AGENTS.md` và tài liệu này sang dự án đó, hoặc thiết lập hướng dẫn toàn cục phù hợp. Không khẳng định đã cài toàn cục nếu mới chỉ tạo file trong repository.

Nguồn về cơ chế nạp hướng dẫn: [OpenAI — Custom instructions with AGENTS.md](https://learn.chatgpt.com/docs/agent-configuration/agents-md). Các quy tắc chọn model và nâng cấp trong tài liệu này là chính sách theo yêu cầu của người dùng; tên tham số công cụ dựa trên schema của phiên làm việc hiện tại.
