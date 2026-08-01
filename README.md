# Debugging-Framework

Mã nguồn nằm trực tiếp dưới `src/` và được chia theo chức năng:

```text
src/
├── core/          # Codex runner, prompt, FL+APR pipeline
├── loaders/       # Đọc BugRecord/Defects4C
├── validation/    # SandboxAdapter và validation snapshot
├── evaluation/    # FL/APR evaluator
├── utils/         # config, JSON atomic I/O, worktree, Unified runtime import
└── schemas/       # JSON Schema cho Codex final response
```

Framework chạy **Fault Localization (FL) + Automated Program Repair (APR) bằng
Codex CLI**, đồng thời giữ nguyên semantics thực nghiệm của dự án
`../Unified-Debugging`:

- Input được đọc bằng chính `data_loaders.get_loader()` và
  `Defects4CLoader` hiện tại.
- Patch được compile/test bằng chính `SandboxAdapter.validate()` hiện tại.
- Kết quả được phân loại bằng `build_validation_snapshot()` hiện tại.
- FL/APR metrics được tính bằng chính `evaluation/eval_fl.py` và
  `evaluation/eval_apr.py` hiện tại.

Phần FL/APR phức tạp của Unified-Debugging không được import. Thay vào đó, mỗi
bug được giao cho một lần `codex exec` trong một git worktree cách ly. Codex chỉ
đọc repo, định vị lỗi và trả về JSON chứa unified diff; framework kiểm tra rồi tự
apply diff vào buggy worktree, lưu source đã ghép và gửi artifact sang validation
adapter.

## Luồng chạy

```text
Defects4C metadata
  -> Unified-Debugging Defects4CLoader
  -> disposable buggy git worktree
  -> codex exec (FL + chỉ trả unified diff cho 1 production source file)
  -> framework tự apply diff vào buggy worktree
  -> kiểm tra changed-file policy + lưu diff/artifact
  -> Unified-Debugging SandboxAdapter (Docker compile + full tests)
  -> retry Codex với validation feedback nếu cần
  -> fault_localization_results.json + apr_results.json
  -> Unified-Debugging FL/APR evaluation
```

Ground truth/fixed source chỉ được loader và evaluator giữ để tính metrics;
prompt gửi Codex **không chứa ground truth, accepted file hay fixed commit**.
Danh sách file Codex được phép sửa được suy ra từ coverage của failing tests,
không lấy từ danh sách file thay đổi giữa buggy/fixed; chỉ khi metadata hoàn
toàn thiếu coverage framework mới fallback về `source_relpath`.

## Cài đặt

Nên dùng cùng virtualenv của Unified-Debugging để bảo đảm dependency và parser
hoàn toàn giống nhau:

```bash
cd /home/halinh/Unified_Debugging/Unified-Debugging
source .venv/bin/activate

cd /home/halinh/Unified_Debugging/Debugging-Framework
pip install -r requirements.txt
pip install -e .
```

Nếu chỉ muốn chạy trực tiếp từ source và virtualenv Unified-Debugging đã có đủ
dependency, có thể bỏ qua bước install và dùng ngay `python -m src`.

Codex CLI phải đăng nhập sẵn (`codex login`). Validation Defects4C cần Docker
container tương ứng đang chạy, ví dụ `my_defects4c_fmt`; có thể override bằng
`DEFECTS4C_CONTAINER` như trong Unified-Debugging.

Kiểm tra môi trường:

```bash
python -m src doctor --dataset fmt
```

## Sử dụng

Đọc đúng một bug qua loader chuẩn, chưa gọi Codex/validation:

```bash
python -m src list --dataset fmt --bug-id A.2
```

Chạy trọn Codex FL+APR -> validation -> evaluation:

```bash
python -m src run --dataset fmt --bug-id A.2 --attempts 2
```

Chạy toàn bộ metadata folder và bỏ qua bug đã có kết quả:

```bash
python -m src run --dataset libyang --attempts 3 --only-missing
```

Chọn model cụ thể cho Codex CLI:

```bash
python -m src run \
  --dataset php \
  --bug-id CVE-2018-7584 \
  --model <codex-model> \
  --attempts 3
```

Mặc định worker dùng `--ignore-user-config --ignore-rules` để các lần chạy tự
động có cấu hình ổn định, nhưng vẫn dùng authentication Codex đã lưu. Dùng
`--inherit-codex-config` nếu muốn nạp model/MCP/config cá nhân.

Validate lại artifact đã chọn mà không gọi Codex:

```bash
python -m src validate --dataset fmt --bug-id A.2
```

Chạy lại evaluator chuẩn:

```bash
python -m src evaluate --dataset fmt
```

## Output

```text
experiments/
├── run_manifest.json
├── fault_localization_results.json
├── apr_results.json
├── evaluation.txt
├── patches/                       # plausible patches
└── codex_artifacts/<bug-id>/
    └── attempt_01/
        ├── prompt.txt
        ├── events.jsonl           # full `codex exec --json` trace
        ├── stderr.txt
        ├── response.json          # response JSON nguyên bản từ Codex
        ├── patch.diff
        └── patched__<source-file>
```

`response.json` được lưu cho từng attempt. `apr_results.json` giữ response được
chọn trong `codex_response`, đường dẫn artifact trong `codex_response_artifact`
và toàn bộ lịch sử trong `codex_responses`; `fault_localization_results.json`
cũng giữ các response tương ứng để audit/reproduce.

Hai JSON tổng hợp giữ các field evaluator hiện tại cần, gồm `scores`,
`ground_truth`, `selected_function`, `patched_function`, `patched_file`,
`init_*`, `post_*`, `full_post_*`, `status` và `real_status`.

## Nguyên tắc an toàn và tái lập

- Codex chạy với `--sandbox read-only`; Codex không được tự ghi hoặc apply patch.
- Framework là thành phần duy nhất apply unified diff vào disposable buggy
  worktree trước khi validation.
- Git history mà Codex nhìn thấy được tái tạo thành đúng một commit buggy
  snapshot; staged overlay/fixed HEAD của cache Defects4C không bị lộ cho agent.
- Chỉ đúng một production-source path thuộc candidate universe suy ra từ
  failing-test coverage được chấp nhận; thay đổi file khác làm attempt thành
  `invalid`.
- Codex không được nhận fixed revision hoặc ground truth.
- Codex không chạy full suite; validation duy nhất có thẩm quyền là adapter
  Docker của Unified-Debugging.
- Worktree bị thu hồi sau mỗi attempt; diff và patched file được lưu riêng.
- Kết quả được ghi atomic sau từng bug để có thể resume bằng `--only-missing`.
