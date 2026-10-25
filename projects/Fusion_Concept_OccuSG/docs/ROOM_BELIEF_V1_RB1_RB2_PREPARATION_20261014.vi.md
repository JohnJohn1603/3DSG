# Room Belief v1 — RB1 data readiness and RB2 preparation

Ngày: 2026-10-14. Đây là checkpoint mới; báo cáo RB0/RB1 trước đó vẫn được giữ để truy vết.

## Trạng thái gate

| Gate | Trạng thái | Phạm vi đã xác nhận |
|---|---|---|
| RB0 local preflight | Đã kiểm tra read-only; local Qwen bị chặn bởi tài nguyên/dependencies | Python local 3.12.3; RAM khả dụng lúc kiểm tra 9.6 GiB, disk workspace còn 309 GiB; máy không có GPU trong preflight trước; cache Qwen3-4B-Base không tìm thấy. Python mặc định và `.venv-cg-cpu` thiếu Transformers/tokenizers/safetensors/huggingface_hub. Không tải/cài gì. |
| RB1 controller contract | Unit/property tests đạt | Belief chuẩn hóa nhưng chưa calibrated; incumbent/relabel, revisions, idempotence và retract được kiểm tra bằng tests. Chưa nối runtime/default launcher. |
| RB1 real-data readiness | Static adapter chạy được; room scoring bị chặn vì A table chưa có | Cache hybrid U6 00808 5 frame/18 observations, 9 hypotheses (2 retained/7 filtered), room snapshot cùng sequence có một room node. Không phải causal replay hay quality evaluation. |
| RB2 scorer code | Implemented/tested synthetically | Causal continuation scoring, exact token boundary, label-only log probability, mean/sum, neutral subtraction và A-row math. Không có Qwen model/tokenizer run. |
| RB2 Kaggle package/notebook | Prepared, not executed | Input chỉ gồm ontology, 11 canonical predictor category strings, scorer/config/lock. Tất cả install/model/scoring gates mặc định off. |
| Qwen/Kaggle model smoke và A table | Pending | Không upload, download, cài package, bật GPU, load weights hay chạy inference trong checkpoint này. |

## RB1 contract findings

- `minimum_top_belief=0.60` áp dụng lên **normalized uncalibrated belief**, không phải raw score `S` và không có diễn giải “xác suất đúng 60%”. Absolute gate còn yêu cầu ít nhất hai supported categories và margin giữa top với runner-up ≥0.20.
- Relabel so sánh top label mới với incumbent (hoặc `last_supported_label` khi label đang ambiguous) và yêu cầu chênh lệch ≥0.15, bên cạnh absolute gate. Hai relabel steps tăng theo category-presence evidence revisions; lặp lại cùng event/inference hoặc chỉ đổi metadata không tạo revisions mới. Các số đều `proposed_unapproved`, chưa calibrated.
- Khi không còn eligible category evidence, label được rút về `unknown`; lịch sử cũ được giữ nhưng không làm current label có vẻ còn được hỗ trợ.
- Pose grouping là equality hash trên ma trận pose 4×4 đã cache. Exact-pose revisit chỉ tạo một vote unit; nhiều transforms không được gọi là các góc nhìn độc lập về mặt thống kê. Conflict tại một pose chia unit giữa raw class strings. Không dùng detector confidence làm xác suất và không suy alias ngoài map được cấu hình.
- Membership room hiện dùng polygon XY. Cache và room snapshot này chỉ chứng minh một elevation quan sát được (`floor_0`, agent base Z `-2.738609 m`) trong đúng sequence; room snapshot không có `floor_id`/Z bounds. XY support trong readiness report chỉ là diagnostic của một scope một tầng đã xác minh, không phải bảo đảm cho scene HM3D nhiều tầng.
- Voxel measurement dùng `0.025 m` lấy từ effective mapping config `downsample_voxel_size`; origin `[0,0,0]` lấy theo helper `floor(xyz / voxel_size)`. Đây là quy ước đo membership, không thay cloud hay room geometry.

## Static readiness trên cache thật

Report: [readiness.md](../artifacts/room_belief/readiness/u6_00808_hybrid_v3_static_v2/readiness.md), [readiness.json](../artifacts/room_belief/readiness/u6_00808_hybrid_v3_static_v2/readiness.json), [objects.csv](../artifacts/room_belief/readiness/u6_00808_hybrid_v3_static_v2/objects.csv).

- Inputs: `hybrid_u6_00808_seed20261014_v3`, sequence `hm3d-v02-00808-u6-quality-v1`, room snapshot revision 6. Scene/cache/snapshot provenance khớp.
- Một object giữ lại có raw history `couch` qua 3 exact-pose units nhưng graph membership hiện là ambiguous. Object giữ lại còn lại có 3 pose units với raw votes `dining table: 2`, `couch: 1`, nên semantic status conflicted theo proposal đang khóa. Không dùng annotation làm predictor.
- Bảy filtered hypotheses được liệt kê riêng và không góp evidence. Không có object/category eligible cho room score hiện tại vì chưa có A table và các retained object chưa đồng thời đạt semantic/membership gates.
- Một room duy nhất không thể đo phân biệt bedroom/living_room/etc. `room label` không được tính; accuracy và calibration là unavailable.

## Qwen candidate lock và scoring contract

- Model: `Qwen/Qwen3-4B-Base`, immutable revision `906bfd4b4dc7f14ee4320094d8b41684abff8539`. Nguồn model ghi 4.0B parameters, BF16 tensor type và Apache-2.0; model card nêu Transformers dưới 4.51.0 gặp `KeyError: qwen3`. Candidate direct pins: `transformers==4.51.3`, `tokenizers==0.21.1`, `safetensors==0.5.3`, `huggingface_hub==0.30.2`. Đây chưa phải resolved Kaggle lock; Python/Torch/NumPy và transitive requirements vẫn phải được probe và `pip check` trên kernel đích.
- Bộ nhớ weights ước tính theo 4B × 2 byte ≈8.0 GB (decimal), chưa tính load/forward workspace. Lock không có expected upstream SHA-256 manifest. Revision pin cho provenance; SHA tính từ file local sau này chỉ là local-integrity hash.
- Upstream tensor dtype là BF16. Kaggle T4 probe trước đó báo hai Tesla T4; notebook dùng BF16 nguyên trạng và sẽ dừng nếu GPU không hỗ trợ. FP16 cast, quantization, offload hoặc device-map sharding chưa được duyệt/kiểm chứng.
- Prompt: `Observed object: {canonical_object_name}.\nRoom type:`; neutral thay category bằng `object`. Completion là chuỗi có dấu cách đầu từ ontology versioned. Tokenize prefix và full string riêng, yêu cầu prefix IDs là prefix chính xác của full IDs. BOS omitted, EOS không được score. Không `generate()`.
- Giữ 4 biến thể mean/sum × neutral/no-neutral. T=1, uniform prior K=8, epsilon `1e-8`. Export token logprobs và A log-ratio; normalized scores/A là uncalibrated compatibility values. Không chọn biến thể thắng khi chưa có room GT dev thích hợp.
- 11 object category strings lấy nguyên từ `vocabulary.room_relevant_classes` và canonical map trong detector profile đã hash. Đây là subset có giới hạn; unsupported/out-of-set category phải thiếu A row, không fallback.

## Kaggle handoff

Notebook: [room_belief_qwen3_rb2_v1.ipynb](../notebooks/room_belief_qwen3_rb2_v1.ipynb).

Data-free private input package: [room_belief_qwen_rb2_inputs_v1.zip](../artifacts/room_belief/room_belief_qwen_rb2_inputs_v1.zip), SHA-256 `5ffb276b431e514d3e2ac1649c6d3de95f131fa7f17cf71b7241cc7c83635afd`. Chứa ontology/category JSON, scoring contract, candidate lock, standalone scorer và inventory hash; không chứa scene data, annotations hay weights. Có thể tái tạo bằng:

```bash
python3 scripts/prepare_room_belief_rb2_kaggle.py
```

Tạo Kaggle Dataset **private**, chỉ upload ZIP trên và attach ở `/kaggle/input/room-belief-qwen-rb2-v1`. Notebook chấp nhận ZIP đã mount hoặc các file đã extract sẵn; nó xác minh archive hash trước khi giải nén. Có các checkpoint A resource probe (read-only), B input hashes, C gated direct-dependency install + version/pip check, D local-only model/tokenizer/revision check, E gated BF16 model load, F scoring smoke/full A export. Không dùng Run All. Hiện tất cả opt-in gates đều `False`; không bật trước khi có duyệt riêng cho package/network, model weights/GPU và dtype cần dùng.

Nếu chạy sau khi được duyệt, output sẽ gồm `raw_token_scores.json`, `a_table.csv/json`, `category_room_scores_by_variant.json` (dạng mapping category→room→A tương thích với `RoomBeliefLedger` sau khi chọn variant), và `manifest.json` kèm local hashes.

## Verification thực tế

```bash
python3 -m py_compile scripts/prepare_room_belief_rb2_kaggle.py
PYTHONPATH=src python3 -m pytest -q \
  tests/test_room_belief_v1.py \
  tests/test_region_object_room_semantics.py \
  tests/test_room_semantics.py \
  tests/test_m8_rooms.py \
  tests/test_room_belief_readiness.py \
  tests/test_qwen_room_scorer.py \
  tests/test_room_belief_qwen_notebook.py
```

Kết quả: **59 passed**; notebook code cells được compile nhưng không execute. Không có model/tokenizer/Kaggle smoke trong test này.

## Quyết định còn mở / bước kế tiếp

1. Duyệt một execution environment BF16-capable cho Qwen3-4B-Base, hoặc duyệt rõ FP16 cast cho T4 (đổi dtype so với upstream); đồng thời duyệt riêng download/network và inference. Không tự chọn thay.
2. Sau đó chạy model/tokenizer smoke và export A; trước đó RB1 real-data scoring vẫn unavailable.
3. Khi A có, static readiness tiếp theo cần room ground truth/review đúng geometry nếu muốn đánh giá accuracy; review identity/class hiện có không tự thành room GT.
