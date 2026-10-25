# 3DSG — gói bàn giao nghiên cứu

Gói tập trung mã hiện tại của **3DSG frontier-based**, **OccuSG standalone không ROS** và **ConceptGraphs CPU hybrid**. Source, model, một scene demo, native workers và hướng dẫn dựng môi trường nằm trong thư mục này. Có thể chuyển cả thư mục sang vị trí mới; launcher lấy đường dẫn từ vị trí hiện tại.

Không kèm lịch sử run, RGB-D đã ghi, graph đã dựng, cache, `.git` hoặc virtualenv. Mỗi lần demo sinh kết quả **bên ngoài** thư mục bàn giao. Các repo gốc trên máy người gửi không bị sửa khi tạo gói.

## Cấu trúc

```text
3DSG_Research_Bundle/
  demo.py                      # lệnh demo chính: native hoặc navigation, có desktop
  projects/
    3DSG/                      # frontier, observed NavMap, A*, controller, runtime, UI
    Fusion_Concept_OccuSG/      # ConceptGraphs core + adapter/P2/P3/OpenCLIP + protocol
    Rebuild_3DScene_OccuSG/     # OccuSG standalone, graph/region/native client
  third_party/
    mapconversion/             # dependency M3 trước đây nằm ngoài cả ba repo
    pytorch3d-v0.7.4/           # source + CPU extension hiện có
    octomap-prefix/            # headers/static libraries để dựng M2/M3
    m4-prefix/                 # headers/libraries CGAL/GMP/MPFR và notices
  assets/models/               # YOLO11n-seg ONNX và OpenCLIP ViT-B-32
  assets/scenes/hm3d-00802/     # một basis mesh + navmesh; không dùng semantic GT
  prebuilt/linux-x86_64/       # hai worker đã build lại + thư viện riêng
  config/                      # spawn/calibration, feature contract, bootstrap template
  environments/                # specs đã chụp từ bốn môi trường đang hoạt động
  tools/                       # setup, rebuild native, kiểm tra manifest/test/UI
  docs/                        # hướng dẫn setup và phạm vi portability
```

Hai bản `standalone_sg` được giữ theo đúng repo sở hữu vì chúng đã có thay đổi khác nhau; không trộn hai package bằng cách chép đè. Nguồn OccuSG tham chiếu dùng chung qua **symlink tương đối nội bộ** từ Fusion sang Rebuild. Không có symlink ra ngoài thư mục bàn giao.

## Nhận từ GitHub

Có thể tải gói đầy đủ trong [GitHub Release research-handoff-2026-10-26](https://github.com/JohnJohn1603/3DSG/releases/tag/research-handoff-2026-10-26), giải nén rồi dùng trực tiếp hướng dẫn bên dưới.

Nếu dùng `git clone`, checkpoint OpenCLIP 605 MB chưa có trong Git. Chạy `python3 -B tools/fetch_demo_assets.py` để lấy tài sản còn thiếu từ archive Release đã pin SHA-256. Sau đó `python3 -B tools/verify_bundle.py` kiểm tra toàn bộ. Có thể kiểm tra riêng source trước khi tải bằng `--source-only`. Thư mục `.git` ở gốc được bỏ qua khi kiểm tra checkout; các `.git` lồng trong source vẫn bị từ chối.

## Kiểm tra gói vừa nhận

```bash
cd 3DSG_Research_Bundle
python3 -B tools/verify_bundle.py
```

Manifest chứa SHA-256 của source và tài sản. `SOURCE_SNAPSHOT.json` ghi hash source gốc trước các chỉnh sửa đường dẫn; `docs/PORTABILITY.md` mô tả những thay đổi đó. Các commit nguồn được ghi trong `SOURCE_PROVENANCE.json`.

Gói nhắm **Linux x86_64**, đã chạy trên Ubuntu 24.04 với CPU rendering. Cần driver OpenGL/EGL hoạt động và runtime libraries OpenCV/jsoncpp. Prebuilt không phải binary phổ quát cho mọi hệ điều hành/ABI.

## Tạo môi trường trên máy teammate

Dùng bốn môi trường vì Python/NumPy/PyTorch ABI khác nhau. **Không sao chép virtualenv từ máy cũ.** Cần conda, Python 3.10 và Python 3.12 có module venv/dev headers; việc cài package cần mạng. Cấu hình đầy đủ và gói hệ thống được ghi ở [docs/ENVIRONMENT_SETUP.md](docs/ENVIRONMENT_SETUP.md).

Đặt môi trường bên ngoài để thư mục source vẫn sạch:

```bash
python3 tools/setup_environments.py \
  --venvs-dir ../3dsg-envs \
  --runtime-config ../3dsg-runtime.json \
  --python310 python3.10 --python312 python3.12 --conda conda --jobs 2
```

Có thể dùng các môi trường đã cài sẵn: tạo JSON theo `config/runtime.example.json`, thay bốn đường dẫn interpreter. JSON được phép nằm ngoài gói. Chỉ các interpreter/package cài đặt được dùng từ bên ngoài; mã dự án, native workers, model và scene của launcher đều lấy trong gói.

Nếu dùng lại môi trường PyTorch3D editable của máy người gửi, runtime JSON cần thêm `"pytorch3d_source": "third_party/pytorch3d-v0.7.4"` để nạp source/extension đi kèm. Với môi trường mới do setup tạo, bỏ trường này để dùng extension vừa build/cài trong môi trường đó.

Bộ setup đã được kiểm tra cú pháp/dry-run và chụp versions thực tế. Chưa dựng lại toàn bộ bốn môi trường từ Internet trên một máy sạch. Demo bàn giao đã chạy với các môi trường đang có, trong khi chặn đọc source/model từ các checkout cũ.

## Chạy demo thật

```bash
python3 -B demo.py --runtime ../3dsg-runtime.json --check-only \
  --output-root ../3dsg-demo-results

python3 -B demo.py --runtime ../3dsg-runtime.json \
  --mode native --duration-s 120 --output-root ../3dsg-demo-results
```

`native` bật P2, P3/OpenCLIP, ConceptGraphs và OccuSG native thật. `--mode navigation` chỉ chạy frontier navigation. Khởi đầu quét 360 độ là hành vi bình thường. Không có route/waypoint cài sẵn cho policy: goal và path do frontier/observed map/A* chọn.

Khi đã tạo môi trường ở đường dẫn mặc định `.venvs/<role>` bên trong gói, có thể bỏ `--runtime`. Nếu di chuyển môi trường, tạo lại runtime JSON.

### Desktop ba panel

Python UI cần NumPy và PyQt5. Môi trường UI kiểm chứng dùng NumPy 1.26.4 / PyQt5 5.15.10. Có thể tạo riêng:

```bash
python3.12 -m venv ../3dsg-envs/ui
../3dsg-envs/ui/bin/python -m pip install numpy==1.26.4 PyQt5==5.15.10
python3 -B demo.py --runtime ../3dsg-runtime.json --desktop \
  --ui-python ../3dsg-envs/ui/bin/python \
  --duration-s 600 --output-root ../3dsg-demo-results
```

Toolbar **Pause/Stop** giữ hành vi hiện tại. Có thể xem lại một `live/` vừa sinh bằng `projects/3DSG/scripts/run_desktop.py --live-dir /path/to/run/live` với Python UI. Lệnh UI cần display; `QT_QPA_PLATFORM=offscreen` chỉ dùng cho kiểm chứng widget không màn hình.

## Build lại và test

Hai native workers đã được build lại **từ source trong gói**, không cần ROS. Rebuild khi đổi máy/toolchain:

```bash
python3 tools/build_native.py --build-dir /tmp/3dsg-native-build --jobs 2
```

MapConversion được kiểm tra tree hash theo commit đã pin; không cần mang `.git`. Worker dùng `$ORIGIN/../lib`, không còn RUNPATH về repo cũ. PyTorch3D có toàn bộ source để build lại cho Python 3.10 / Torch 2.0.1 CPU. Extension prebuilt đi kèm chỉ dùng với đúng ABI này.

Với Python có pytest, NumPy, SciPy và PyQt5:

```bash
python3 -B tools/run_tests.py --python /path/to/test-python
```

Bộ 3DSG trong gói đã chạy **193 tests passed**. Các tests nghiên cứu cũ của Fusion/Rebuild vẫn được giữ cùng source; một số cần fixture/dataset lịch sử không nằm trong gói. Entry point được hỗ trợ cho demo bàn giao là `demo.py`; các script thử nghiệm cũ có thể cần cấu hình lại input riêng.

Sau khi chủ động sửa source hoặc rebuild binary, cập nhật manifest bằng `python3 -B tools/verify_bundle.py --write-manifest`. Thao tác này ghi nhận trạng thái mới, không thay thế test.

## Kiểm chứng bàn giao

- Chuyển bản sao sang `/tmp` rồi chạy native demo 120 s: cả ConceptGraphs và OccuSG kết nối thật, **19 paired graph revisions**, shutdown sạch.
- Đường đi 2.260 m, net displacement 1.386 m, 3 viewpoint cách nhau ít nhất 0.5 m; movement/spatial evaluator đạt.
- **Continuity audit không đạt**: có một đoạn gần đứng tại chỗ 36.73 s với chu kỳ alignment/recovery. Đây là giới hạn điều hướng còn tồn tại trong source hiện tại, cần giữ làm bài toán nghiên cứu; không khẳng định exploration đã tối ưu.
- Audit 9 Python processes: không có import/đọc source hoặc model từ checkout cũ; chỉ dùng interpreter và thư viện đã cài. Native workers có RPATH tương đối và đã kiểm tra `ldd`.
- Desktop được khởi chạy thật, chụp widget lúc runner hoạt động: RGB/map/graph đều tải, graph revision 4, không snapshot-hash mismatch.

Raw run, trace, screenshot và báo cáo kiểm chứng được giữ **ngoài gói source** ở máy người gửi. Gói không chứa graph/capture có sẵn để giả làm kết quả demo.

## Phạm vi nghiên cứu và tài sản

ConceptGraphs giữ mapping core đã pin, với perception YOLO11n-seg + OpenCLIP ViT-B-32 đang dùng. Đây là pipeline hybrid hiện tại; không phải toàn bộ recipe RAM/GroundingDINO/SAM của ConceptGraphs-Detect. Graph viewer lấy snapshot bền vững từ nhánh OccuSG và giữ provenance của nhánh ConceptGraphs; chính sách nối graph sâu hơn vẫn là phần nghiên cứu.

Phần lớn dung lượng là hai model và một mesh để demo inference trực tiếp; không phải kết quả run. Model/mesh được kiểm tra bằng `ASSET_MANIFEST.json`. Không cần mang toàn bộ HM3D, checkpoint khác hoặc thư mục artifacts cũ.

Giữ các thông báo trong [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md), đặc biệt notice của FIST/DuDe và điều kiện tài sản HM3D/model. Gói không áp một giấy phép chung lên tất cả thành phần.
