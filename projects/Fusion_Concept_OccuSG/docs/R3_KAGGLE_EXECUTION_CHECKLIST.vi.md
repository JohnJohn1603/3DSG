# R3 Kaggle checklist — ConceptGraphs-Detect

Ngày cập nhật: 2026-10-12. Tài liệu chuẩn bị thao tác, chưa có upload, cài dependency, tải checkpoint hoặc chạy GPU.

## Trạng thái bằng chứng

- Lock hiện pin ConceptGraphs `93277a02bd89171f8121e84203121cf7af9ebb5d`, Grounded-SAM `a4d76a2b55e348943cba4cd57d7553c354296223`, GradSLAM `59ca872e3d265ad09f63c4793d011fad67064452`, chamferdist `ee753899b583bdc06bd2505dc162fd54ba0c6c5b`.
- ConceptGraphs `environment.yml` ghi GradSLAM và chamferdist version `1.0.0`; README chỉ yêu cầu GradSLAM nhánh `conceptfusion` và chamferdist repo, không pin commit. GradSLAM là pin ứng viên được tham khảo từ recipe RAG-3DSG khai báo theo ConceptGraphs stack và nằm trên import path dataset loader. chamferdist được giữ để tái lập reference evaluators, nhưng static search không thấy trên selected detector/mapping path nên v11 không cài. Fetch/build/import/smoke trên Kaggle chưa chạy; không kết luận tương thích runtime.
- `expected_upstream_sha256` và `measured_local_sha256` là hai trường riêng. Lock hiện không có upstream checksum đã xác nhận cho các checkpoint. SHA cục bộ chỉ đo bytes sau khi file được cấp; nó không xác thực nguồn.
- RAM chỉ có URL upstream trỏ nhánh Hugging Face `main`, chưa có immutable revision trong lock. Không chạy cell tải RAM; dừng nếu chưa xác lập provenance cho file được mount.
- OpenCLIP repo revision được ghi trong lock; việc ConceptGraphs/OpenCLIP loader thực sự buộc revision đó chưa xác minh. Reference loader pass và Kaggle pipeline pass vẫn pending.

## Trước khi đưa dữ liệu lên Kaggle

HM3D được Habitat mô tả là dùng cho nghiên cứu học thuật phi thương mại. AI Habitat dẫn chiếu Matterport Academic Use EULA. EULA giới hạn cả dataset và “Derived Information” vào mục đích học thuật phi thương mại; quy định khi publish/distribute; và cấm cấp phép/ủy quyền cho bên thứ ba dùng hoặc phân phối cho mục đích phi học thuật nếu không có thỏa thuận bằng văn bản. [AI Habitat HM3D](https://aihabitat.org/datasets/hm3d/0017.html), [Matterport Academic Use EULA](https://matterport.com/legal/matterport-end-user-license-agreement-academic-use-model-data).

Kaggle yêu cầu người upload có đủ quyền cần thiết. Personal/private submission vẫn được Kaggle lưu trữ và xử lý để cung cấp dịch vụ; “private” không đồng nghĩa dữ liệu không rời máy hoặc Kaggle không truy cập. Public dataset/notebook cho phép phạm vi hiển thị rộng hơn. Các tài liệu đã xem không giải quyết dứt điểm việc upload capture/derived RGB-D của HM3D lên Kaggle có được EULA cho phép hay không. Chủ sở hữu dữ liệu cần tự xác minh với Matterport/đơn vị cấp quyền trước khi upload; checklist này không xác nhận quyền thay người dùng. [Kaggle Terms](https://www.kaggle.com/terms), [Kaggle Datasets docs](https://www.kaggle.com/docs/datasets).

Trước upload, người dùng cần tự đánh dấu:

- [ ] Quyền dùng scene/capture này bao gồm xử lý trên dịch vụ cloud bên thứ ba.
- [ ] Notebook và dataset đặt **Private**, không thêm collaborators, không publish output.
- [ ] Không đưa GT, annotations, credentials hoặc scene mesh/semantic assets vào package.
- [ ] Đã đọc điều khoản Kaggle hiện hành và các checkpoint/model terms riêng.

Nếu mục đầu còn chưa rõ, dừng trước khi upload package Q1.

## Chuẩn bị file và notebook

Các đường dẫn local trong workspace:

- Code + lock + notebook: `artifacts/integration_v2/kaggle_bundle/conceptgraphs_r2_bundle_v11.zip`.
- Capture package 7 frame: `artifacts/integration_v2/packages/q1_00808_cg_v6.zip`.
- Notebook: `notebooks/conceptgraphs_detect_q1_00808.ipynb`.

Sau khi người dùng xác nhận quyền upload, tạo hai Kaggle Datasets riêng, cả hai đặt Private và không mời collaborators:

1. Dataset code bundle: chỉ chứa ZIP v11, mount vào notebook tại `/kaggle/input/conceptgraphs-r2-bundle`.
2. Dataset capture: chỉ chứa ZIP Q1 đã khóa, mount tại `/kaggle/input/q1-00808-cg-package`.

Không trộn checkpoints vào hai dataset trên. Nếu sau này có checkpoint với nguồn/quyền/provenance đã kiểm tra, đặt trong dataset private thứ ba và mount tại `/kaggle/input/approved-cg-checkpoints`; copy vào thư mục writable của phiên Kaggle theo hướng dẫn checkpoint riêng. Không upload checkpoints qua bundle v11.

Mở notebook đã đóng trong bundle v11 trên Kaggle, đặt Notebook visibility là Private, Accelerator là **GPU T4 x2**, và bật Internet chỉ khi cần fetch đúng source revisions hoặc checkpoints đã được duyệt. Hai T4 không gộp VRAM; ConceptGraphs hiện chạy các model trên `cuda:0`.

## Chạy theo thứ tự cell

| Cell | Tác động |
|---|---|
| A — Probe | Chỉ đọc Python, CUDA, từng GPU, VRAM, host RAM và disk. Chọn T4 x2 trong UI trước; xác minh thực tế tên GPU và bộ nhớ sau đó. Runner yêu cầu GPU 0 có ít nhất 13 GiB free. Nếu Python/CUDA, GPU hoặc disk không phù hợp, dừng; không đổi model/precision. |
| B — Core setup | Mặc định bỏ qua. Nếu sau này được bật, cài các package base đã pin vào kernel Kaggle. Không chạy nếu kernel không phải Python 3.10 hoặc không có conda theo cell. |
| B2 — GradSLAM exact-source setup | Mặc định bỏ qua. Chỉ fetch full SHA trong lock; pip dùng `--no-deps` để tránh kéo phiên bản thả nổi. Sau đó `pip check` và import phải pass; nếu thiếu dependency hoặc build lỗi, dừng và cập nhật lock trước khi thử lại. Pass ở đây chưa phải reference loader/pipeline pass. |
| C — Checkpoint inventory | Chỉ kiểm tra file/size/provenance và tính SHA trên file đã có. Không tải. Cell tải GroundingDINO/SAM tách riêng và tắt mặc định; không gồm RAM. Cell OpenCLIP cũng tắt mặc định và tải đúng một file từ revision khóa nếu được bật. Không bật download cells trong lượt chuẩn bị này. |
| D — Package/loader preflight | Fetch code đúng commit SHA vào `/kaggle/working` khi chưa có; kiểm tra package và adapter. Đây là network fetch mã nguồn, không cài package hay tải weights. Commit mismatch hoặc loader check fail thì dừng. |
| E — One-frame smoke | Chạy detector + mapping frame 0 từ bản package sạch. Đây là inference GPU thật, không phải preflight. Kiểm tra stage status, detections/masks, object map và manifest. |
| F — Seven-frame pilot | Chạy frame 0–6 từ một bản package sạch khác. Không đọc smoke output. Kiểm tra toàn bộ stage/output/export validators trước khi dùng kết quả. |
| G — Archive | Đóng gói kết quả pilot để tải về trước khi phiên Kaggle hết hạn. Đây là thao tác lưu output, không chạy inference. |

Cell 1 tạo `RUN_TAG` UTC mới mỗi lần được chạy; kết quả nằm trong `/kaggle/working/cg_q1_00808/<RUN_TAG>/`. Smoke và pilot có thư mục `smoke_1frame` và `full_7frames` riêng. Runner từ chối ghi đè output đã tồn tại. Để chạy lại, chạy lại cell 1 để tạo root mới; không nối smoke vào pilot và không reuse map cũ.

## Điều kiện dừng trước detector smoke

- Không có xác nhận quyền chuyển dữ liệu HM3D lên Kaggle: không upload.
- Chưa xác minh RAM checkpoint immutable source/provenance: không tải RAM, không chạy inference.
- Thiếu bất kỳ checkpoint/model đúng recipe, checkpoint không khớp source/file manifest, hoặc OpenCLIP loader không buộc được revision đã khóa: dừng, không thay weights.
- GradSLAM exact fetch, install/import, `pip check` hoặc reference loader chưa pass: dừng; không coi local static checks là pass thay thế.
- Kaggle resource probe không đạt cấu hình/VRAM/disk, hoặc runtime requirements khác lock: dừng và báo thông tin thực tế.

## Chứng cứ kết thúc R3

Các trạng thái phải báo độc lập:

1. Local static validation: lock/notebook JSON, code cell syntax, bundle manifest/integrity.
2. Pinned source fetch/build/import và dependency check: pending Kaggle.
3. Reference package loader/adapter validation: pending Kaggle.
4. One-frame detector + mapping smoke: pending Kaggle.
5. Seven-frame pilot/export validation: pending Kaggle.

Không nâng trạng thái tầng sau chỉ vì tầng trước pass.


## Bundle v11 scope update

The minimal call-path map is in [CONCEPTGRAPHS_MINIMAL_CORE_MAP.vi.md](CONCEPTGRAPHS_MINIMAL_CORE_MAP.vi.md). The notebook no longer installs the full Grounded-SAM `requirements.txt`; its opt-in cell installs only the selected GroundingDINO and SAM source packages with `--no-deps`, then checks imports. Any missing direct/transitive dependency is a hard stop and requires an explicit lock update. This is static scoping, not a completed Kaggle dependency/runtime check.

Bundle v11 includes selected direct dependency pins completed after the first v10 packaging pass. Bundles v8–v10 are preserved; use v11 for the current notebook/builder state.

## Bundle v12 — isolated Kaggle Python environment

The Kaggle probe on 2026-10-12 reported Python 3.13.15 and Torch 2.11.0+cu128, which do not match the locked Python 3.10 / Torch 2.0.1 / CUDA 11.8 recipe. Bundle v11's core setup therefore cannot be enabled on that kernel. Bundle v12 keeps v11 intact and adds one opt-in micromamba environment at `/kaggle/working/cg_q1_env_v12`; it does not downgrade or mutate the Kaggle notebook kernel.

For the v12 first checkpoint, create a Private code dataset containing only `conceptgraphs_r2_bundle_v12.zip`. Do not upload the capture until its transfer/storage permission is confirmed. The v12 setup cell permits bundle-only extraction and the read-only A probe with no capture mounted. If capture permission is confirmed, mount the separate private package dataset at `/kaggle/input/q1-00808-cg-package`; the setup cell checks its expected package layout. Upload both `conceptgraphs_r2_bundle_v12.zip` and its small `conceptgraphs_r2_bundle_v12.manifest.json` sidecar in the private code dataset. The setup cell verifies the ZIP against that manifest and verifies the capture ZIP against its known package hash before extracting either archive.

Run only setup/extraction and A — Probe first, then send the complete output. `CREATE_ISOLATED_ENVIRONMENT` remains `False`; enabling it downloads the pinned micromamba release and installs the locked Python/Torch/core dependencies into the separate prefix. All later source installs, imports, validation and inference must use `CG_PYTHON`; do not run the v11 kernel-install cell. After isolated environment setup, stop and review the versions, imports, `pip check`, CUDA visibility, GPU 0 free memory and remaining disk before source fetch, checkpoint staging or model inference.

RAM provenance is pinned in the v12 lock to `xinyu1205/recognize_anything_model`, revision `ebc52dc741e86466202a5ab8ab22eae6e7d48bf1`, file `ram_swin_large_14m.pth`. The expected digest is the SHA-256 published in the exact Hugging Face file metadata; it is not a locally measured digest. The previous GSA URL used mutable `main`, and byte identity between those two URLs is not claimed. No RAM download cell is provided.
