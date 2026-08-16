# Debugging Framework

Framework tự động **Fault Localization (FL)** và **Automated Program Repair
(APR)** cho project C/C++. Codex sửa một snapshot tạm; framework lấy Git diff
trực tiếp từ filesystem, reset snapshot rồi apply và chạy test trước khi trả kết quả.

CodeGraph 1.5.0 đã được đóng gói sẵn để hỗ trợ Codex điều hướng source code.
Người dùng không cần cài Node.js, npm, npx hoặc CodeGraph riêng.

## Yêu cầu

- Python 3.10 trở lên.
- Git.
- Codex CLI đã cài và đăng nhập bằng `codex login`.
- Môi trường build/test của project C/C++:
  - `host`: máy hiện tại đã có compiler, dependency và test tool; hoặc
  - `image`: Docker/Podman image đã được chuẩn bị sẵn.

Framework không tự cài dependency, không tự build/pull image và không tự đổi
giữa `host` và `image`.

Trên macOS với Docker Desktop, validation copy snapshot vào filesystem Linux
tạm trong container trước khi chạy command rồi đồng bộ artifact trở lại. Cách
này giữ đúng POSIX permission semantics cho các test dùng `access(2)`/`stat(2)`;
project đầu vào vẫn không bị sửa vì thao tác diễn ra trên validation snapshot.

## Cài đặt

### Cài wheel cho partner

```bash
pipx install /path/to/debugging_framework-0.7.0-py3-none-any.whl
codex login
debugging-framework --help
```

Wheel đã chứa CodeGraph cho macOS ARM64 và Linux x64. Trên platform khác,
`DEBUGGING_CONTEXT_MODE=auto` sẽ bỏ qua CodeGraph và giữ nguyên luồng Codex
FL/APR.

CodeGraph mặc định là zero-config: framework tự chọn runtime bundled, tạo index
non-interactive trong snapshot, kiểm tra index rồi đưa lệnh `codegraph` vào phiên
Codex. Framework không cài Git hook, daemon hoặc cấu hình CodeGraph toàn cục trên
máy đối tác.

### Cài từ source để phát triển

```bash
cd /path/to/Debugging-Framework
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
codex login
```

## Chạy repair từng bước

### Bước 1: khởi tạo project

Với môi trường `host`:

```bash
cd /path/to/project
debugging-framework init \
  --environment host \
  --test-id '<test-id>'
```

Ví dụ với image đã có sẵn:

```bash
debugging-framework init \
  --environment image \
  --environment-image project-tests:prepared \
  --test-id '<test-id>'
```

Lệnh này tự nhận diện workflow hiện tại để tạo **bản nháp contract**, đồng thời
tạo thư mục chứa failure log:

```text
.debugging-framework.json
.debugging-framework/
```

Đối tác cần kiểm tra lại `setup`, `build` và đặc biệt là `regression_test` trước
khi chạy repair. Ví dụ contract CMake/CTest:

```json
{
  "schema_version": 6,
  "system": "cmake",
  "setup": [
    ["cmake", "-S", ".", "-B", ".debugging-framework/build", "-DBUILD_TESTING=ON"]
  ],
  "build": [
    ["cmake", "--build", ".debugging-framework/build", "--parallel", "4"]
  ],
  "regression_test": [
    ["ctest", "--test-dir", ".debugging-framework/build", "--output-on-failure"]
  ],
  "repair": {
    "failing_tests": ["<test-id>"],
    "attempts": 2
  },
  "environment": {
    "mode": "host"
  }
}
```

### Bước 2: lưu output lỗi thực tế

Chạy failing test của project và lưu nguyên output vào:

```text
.debugging-framework/failure.log
```

File này phải không rỗng. Framework tin failure log và failing test IDs do caller
cung cấp, dùng chúng làm failure evidence chính cho CodeGraph/Codex và không chạy
test trên source gốc trước khi bắt đầu sửa.

### Bước 3: kiểm tra môi trường

```bash
debugging-framework doctor . --config .debugging-framework.json
```

Kiểm tra các dòng `[FAIL]` trước khi chạy repair. Với CodeGraph mode `auto`,
trạng thái `[WARN]` chỉ có nghĩa Codex sẽ dùng search/read thông thường.

### Bước 4: chạy FL + APR

```bash
debugging-framework repair \
  --project . \
  --config .debugging-framework.json \
  --failure-output .debugging-framework/failure.log \
  --output /tmp/fix.patch
```

Framework sẽ:

1. Nhận failure log và failing test IDs do caller cung cấp, không chạy source gốc.
2. Copy project đúng một lần sang snapshot tạm dùng chung cho các attempt và chuẩn bị CodeGraph nếu khả dụng.
3. Trước mỗi attempt, reset snapshot; cho Codex thực hiện FL và APR từ failure log đã cung cấp.
4. Lấy canonical Git diff từ thay đổi thật trong workspace, reset rồi apply diff đó để setup/build/test.
5. Nếu có `target_test`, chạy nó trước; target fail sẽ chuyển feedback sang attempt sau.
6. Chỉ khi target pass mới chạy `regression_test` full suite.
7. Nếu không có `target_test`, chạy thẳng full suite cho mỗi attempt.
8. Ghi patch được chọn vào `/tmp/fix.patch`.

### Bước 5: xem kết quả

Kết quả đầy đủ mặc định nằm tại:

```text
~/.local/state/debugging-framework/results/<project-name>/
```

Các file thường cần xem:

- `patch.diff`: patch được chọn.
- `result.json`: trạng thái validation cuối cùng.
- `attempts/attempt_NN/`: prompt, phản hồi Codex, canonical workspace patch và log.

Codex chỉ trả mô tả repair và danh sách path; Codex không phải tự chép diff vào
JSON. Framework bổ sung canonical workspace diff vào `repair.diff` trong
`result.json`, và dùng cùng nội dung cho validation lẫn `patch.diff`.

Failure baseline chỉ đến từ caller nên không sinh artifact hoặc chạy command.
Mỗi thư mục kết quả repair chỉ giữ `attempts/`, `patch.diff` (khi Codex tạo được
candidate) và `result.json`.

`status=plausible` luôn đòi hỏi full regression suite pass. Nếu contract có
`target_test`, target cũng phải pass trước đó. Các trạng thái khác như
`cleanfix`, `noisefix`, `nonefix`, `negfix` hoặc `invalid` cần được xem lại trong
`result.json`.

## Cấu hình thường dùng

Phần lớn cấu hình của một job nên đặt trong `.debugging-framework.json`:

```json
{
  "schema_version": 6,
  "system": "partner-contract",
  "setup": [
    ["./scripts/configure.sh"]
  ],
  "build": [
    ["./scripts/build.sh"]
  ],
  "target_test": [
    {
      "command": ["./scripts/run_tests.sh", "--case", "{test_id}"],
      "evidence_pattern": "[1-9][0-9]* tests? (passed|failed)",
      "failure_pattern": "[1-9][0-9]* tests? failed"
    }
  ],
  "regression_test": [
    {
      "command": ["./scripts/run_tests.sh", "--all"],
      "evidence_pattern": "[1-9][0-9]* tests? (passed|failed)",
      "failure_pattern": "[1-9][0-9]* tests? failed"
    }
  ],
  "repair": {
    "failing_tests": ["<test-id>"],
    "attempts": 2,
    "model": "gpt-5.6-sol",
    "codex_timeout": 1800,
    "command_timeout": 1800,
    "jobs": 4
  },
  "environment": {
    "mode": "host"
  }
}
```

Các biến môi trường hữu ích:

| Biến | Mặc định | Công dụng |
| --- | --- | --- |
| `DEBUGGING_CODEX_BIN` | `codex` | Đường dẫn Codex CLI |
| `DEBUGGING_CODEX_MODEL` | `gpt-5.6-sol` | Model dùng cho repair |
| `DEBUGGING_RESULTS_DIR` | thư mục state của user | Nơi lưu kết quả |
| `DEBUGGING_CONTEXT_MODE` | `auto` | Chế độ CodeGraph: `auto`, `required`, `off` |
| `DEBUGGING_CODEGRAPH_TIMEOUT` | `180` | Timeout chuẩn bị graph, tính bằng giây |

Không cần cấu hình CodeGraph executable khi dùng bản wheel/repository đã đóng
gói. Với workflow thông thường, không cần đặt bất kỳ biến CodeGraph nào; các
biến trong bảng chỉ là override nâng cao.

### Chế độ CodeGraph

- `auto` — khuyến nghị: dùng CodeGraph khi sẵn sàng, tự fallback nếu lỗi.
- `required` — dừng job nếu CodeGraph không sẵn sàng; phù hợp benchmark/A-B.
- `off` — tắt CodeGraph và chạy đúng luồng Codex cũ.

CodeGraph chỉ hỗ trợ điều hướng repository. Failure evidence, output schema,
logic FL/APR và validation patch vẫn do framework hiện tại quyết định.

## Contract build/test của đối tác

Trong workflow `repair`, `regression_test` là bắt buộc và phải chạy full suite
chính thức của project. `setup` và `build` có thể là list rỗng nếu image/host đã
chuẩn bị artifact tương ứng. `target_test` là tùy chọn, chỉ dùng để fail nhanh;
nó không thay thế full-suite validation.

Framework có thể nhận diện CMake, Meson, Make/Autotools, Bazel và Ninja để hỗ
trợ lệnh `init` sinh bản nháp. Auto-detection không thay thế contract đã được
đối tác duyệt. Xem contract trước khi chạy:

```bash
debugging-framework inspect /path/to/project --environment host
```

Nếu không khai báo `target_test`, framework chạy thẳng `regression_test` đúng
một lần. Framework không tự thêm `ctest -R`, không truyền test ID vào runner lạ
và không chạy full suite giả làm target. `{test_id}` chỉ hợp lệ trong
`target_test`; `regression_test` chứa placeholder này sẽ bị từ chối.

Mỗi custom test command cần `evidence_pattern` chứng minh ít nhất một test đã
thực sự chạy. Nên thêm `failure_pattern` nếu output lỗi của runner không theo
format phổ biến. Command được chạy trực tiếp, không qua shell expansion.

## Các lệnh khác

```bash
# Xem build/test plan, không chạy lệnh
debugging-framework inspect /path/to/project --environment host

# Kiểm tra Codex, CodeGraph và environment
debugging-framework doctor /path/to/project --environment host

# Validate một patch có sẵn
debugging-framework validate /path/to/project /tmp/fix.patch --environment host
```

## Dành cho maintainer

```bash
# Build wheel để giao partner
python -m pip install build
python -m build --wheel
```

Wheel có kích thước khoảng 115 MB vì chứa hai CodeGraph runtime. Metadata và
license của CodeGraph nằm trong `third_party/codegraph/`.

Framework chỉ cho phép automatic repair thay đổi production C/C++ source/header.
Patch sửa test, fixture hoặc build/test infrastructure sẽ bị validation từ chối.
