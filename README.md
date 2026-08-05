# Debugging-Framework

Tool tổng quát nhận trực tiếp **một project** và xuất ra **raw unified patch của
LLM cùng kết quả build/test cách ly**.

```text
/path/to/project
  -> tự nhận diện build system và test command
  -> chạy baseline trên bản sao validation cách ly
  -> tạo snapshot writable cách ly, không có git history, cho Codex thao tác
  -> Codex có thể sửa nhiều file/chạy lệnh và trả fault localization + unified diff
  -> lưu nguyên văn diff vào artifact của attempt
  -> framework apply toàn bộ diff vào một bản sao validation mới
  -> tự build và chạy test trên bản sao đó
  -> xuất raw unified diff; status cho biết validation pass/fail
```

Framework không đọc metadata Defects4C, không cần ground truth, không dùng
loader/validator/evaluator của `Unified-Debugging`, không có adapter theo tên
project, và không cần Docker container riêng cho dataset.

## Input và output

Input là đường dẫn trực tiếp tới project root:

```bash
python -m src run /home/halinh/Unified_Debugging/defects4c/data/my-project
```

Các thư mục `defectsc_tpl/projects*` không phải source project; chúng là recipe
chứa nhiều bug/version. Dùng materializer của Defects4C để biến đúng một entry
thành project input trước:

```bash
cd /home/halinh/Unified_Debugging/defects4c
python3 prepare_project.py CESNET___libyang --list
python3 prepare_project.py CESNET___libyang --bug A.3
```

Để tạo toàn bộ 15 version libyang:

```bash
python3 prepare_project.py CESNET___libyang --all
```

Mỗi version là một project input độc lập. Danh sách được ghi vào
`defects4c/data/CESNET___libyang__materialized.json`.

Kết quả hiện tại của ví dụ trên là 15 thư mục độc lập, ví dụ:

```text
/home/halinh/Unified_Debugging/defects4c/data/CESNET___libyang__A.3__f128972045a5
```

Materializer đọc `project.json`, `bugs_list_new.json` và Jinja recipe; tạo buggy
checkout, render build/test scripts vào chính project và ghi
`.debugging-framework.json`. Muốn build ngay trong bước chuẩn bị, thêm `--build`.
Sau đó framework chỉ đọc project đã materialize:

```bash
cd /home/halinh/Unified_Debugging/Debugging-Framework
python3 -m src run \
  /home/halinh/Unified_Debugging/defects4c/data/CESNET___libyang__A.3__f128972045a5 \
  --output /tmp/A.3.patch
```

Hoặc xử lý tuần tự toàn bộ 15 project từ manifest và tách patch theo version:

```bash
python3 -m src run-batch \
  /home/halinh/Unified_Debugging/defects4c/data/CESNET___libyang__materialized.json \
  --output-dir /tmp/libyang-patches
```

`run-batch` chỉ là vòng lặp qua các `project_path` trong manifest. Với từng path,
framework vẫn dùng đúng luồng project-in/patch-out như lệnh `run`. Tổng hợp trạng
thái được ghi tại `experiments/batch_result.json`.

Output mặc định:

```text
experiments/<project-name>/patch.diff
```

Có thể chọn đúng file output:

```bash
python -m src run /path/to/project --output /tmp/fix.patch
```

`--output` và `--results-dir` phải nằm ngoài input project; cấu hình trỏ vào bên
trong project bị từ chối trước khi framework ghi hoặc xoá bất kỳ artifact nào.

Mọi unified diff LLM trả về được lưu nguyên văn tại
`attempts/attempt_NN/llm.patch.diff`, kể cả khi diff không apply được hoặc test
vẫn fail. `--output` nhận raw diff được chọn; hãy đọc `status` hoặc
`patch_validation_passed` trong result để biết patch đã qua validation hay chưa.
Project đầu vào không bị chép đè: baseline và patched validation đều chạy trên
bản sao tạm; từng lệnh build/test còn chạy trong Bubblewrap với filesystem host
read-only và chỉ validation snapshot được ghi. Log, response và artifact audit nằm trong
`experiments/<project-name>/`.

Codex chạy với sandbox `workspace-write` trong snapshot tạm nên có thể chỉnh file,
chạy formatter/build/test và dùng `git diff`. Mọi side effect trong snapshot này
bị xoá sau attempt. `repair.diff` là artifact có thẩm quyền: framework áp lại
toàn bộ diff lên một validation snapshot mới, vì vậy patch chỉ pass nếu tự nó tái
tạo được kết quả, không phụ thuộc file/cache mà Codex quên đưa vào diff.

Project có thể được checkout/configure/build trước bởi bất kỳ script bên ngoài
nào. Với Defects4C, `prepare_project.py` đặt từng version đã chuẩn bị trong:

```text
/home/halinh/Unified_Debugging/defects4c/data/
```

Framework không phụ thuộc vào layout trên; nó chỉ dùng project path được truyền
trên command line.

## Build/test auto-detection

Tool nhận diện workflow từ file chuẩn nằm trong project:

| Project marker | Validation tự động |
| --- | --- |
| `CMakeLists.txt` | configure CMake, build, `ctest --output-on-failure` |
| `meson.build` | Meson setup/compile/test |
| `Makefile`, `configure[.ac]` | Make/Autotools build và target `test`/`check` |
| `Cargo.toml` | `cargo build`, `cargo test` |
| `go.mod` | `go build ./...`, `go test ./...` |
| `pyproject.toml`, `pytest.ini`, `setup.cfg` | `python3 -m pytest` |
| `package.json` | npm/pnpm/yarn build và test scripts |
| `pom.xml`, Gradle files | Maven/Gradle build và test |
| `Package.swift` | Swift build và test |
| `.sln`, `.csproj` | .NET build và test |
| Bazel, Ninja, Ruby, Composer markers | build/test command chuẩn tương ứng |

Với build system nội bộ không theo convention công khai, project có thể tự mô tả
command bằng `.debugging-framework.json` ở project root. Đây là cấu hình thuộc
chính input project, không phải adapter hoặc metadata bên ngoài:

```json
{
  "system": "custom",
  "setup": [],
  "build": [["./scripts/build"]],
  "test": [
    {
      "command": ["./scripts/test", "--all"],
      "cwd": ".",
      "evidence_pattern": "[1-9][0-9]* tests? (passed|failed)",
      "failure_pattern": "[1-9][0-9]* tests? failed"
    }
  ]
}
```

Command luôn chạy trực tiếp, không qua shell expansion. `cwd` phải nằm trong
project. Custom test command phải khai báo `evidence_pattern`; pattern phải khớp
output chứng minh ít nhất một test đã chạy. Với runner chuẩn, framework nhận diện
summary hoặc report JUnit/XML/TRX. Nếu custom runner dùng thông báo lỗi riêng,
thêm `failure_pattern` để phân biệt test fail với lỗi hạ tầng. Lệnh trả mã 0 nhưng
không có bằng chứng test bị đánh dấu `test_execution_unverified`; return code và
summary mâu thuẫn bị đánh dấu invalid, không được coi là pass.

Với project do `prepare_project.py` tạo, contract trỏ tới hai script nằm ngay
trong project:

```json
{
  "system": "defects4c-rendered-recipe",
  "build": [["bash", ".debugging-framework/recipe_build.sh"]],
  "test": [["bash", ".debugging-framework/recipe_test.sh"]]
}
```

Hai script này đã được render từ recipe của đúng bug trong lúc materialize. Vì
vậy validation về sau không cần quay lại đọc thư mục `defectsc_tpl/projects*`.

## Cài đặt

Yêu cầu Python `>=3.10`, Git, Codex CLI, Bubblewrap (`bwrap`) và toolchain của
chính project đầu vào. Nếu thiếu `bwrap`, validation fail-closed thay vì chạy lệnh
project không cách ly.

`pyproject.toml` là nguồn khai báo package/dependency duy nhất. Runtime không có
Python dependency ngoài standard library; extra `test` chỉ thêm `pytest`.

```bash
cd /home/halinh/Unified_Debugging/Debugging-Framework
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[test]'
cp -n .env.example .env
codex login
```

Lần cài đầu cần mạng để pip lấy build dependency và `pytest`. Nếu chỉ chạy tool,
không chạy test phát triển, có thể thay lệnh cài bằng `python -m pip install -e .`.
Để kiểm tra chính framework:

```bash
python -m pytest
```

Không cần cài requirements của `Unified-Debugging`.

## Commands

Xem plan được auto-detect nhưng chưa chạy build/test:

```bash
python -m src inspect /path/to/project
python -m src inspect /path/to/project --json
```

Kiểm tra môi trường và executable cần thiết:

```bash
python -m src doctor /path/to/project
```

Chạy repair đầy đủ:

```bash
python -m src run /path/to/project --attempts 2 --output /tmp/fix.patch
```

Chạy mọi project đã materialize trong một manifest:

```bash
python -m src run-batch /path/to/materialized.json --output-dir /tmp/patches
```

Validate lại một patch có sẵn bằng cùng cơ chế tổng quát:

```bash
python -m src validate /path/to/project /tmp/fix.patch
```

Nếu baseline đã pass, `run` mặc định không yêu cầu model sửa ngẫu nhiên. Chỉ dùng
`--allow-clean-project` khi chủ động muốn phân tích một project không có failing test.

## Cấu hình Codex

Thứ tự ưu tiên là CLI, shell environment, `.env`, rồi default. `.env` chỉ chứa
cấu hình runtime của framework/Codex, không chứa project-specific validation data.

```dotenv
CODEX_API_KEY=
DEBUGGING_REQUIRE_API_KEY=false
DEBUGGING_CODEX_MODEL=gpt-5.6-sol
DEBUGGING_CODEX_BIN=codex
DEBUGGING_ATTEMPTS=2
DEBUGGING_TIMEOUT=1800
DEBUGGING_COMMAND_TIMEOUT=1800
DEBUGGING_JOBS=0
DEBUGGING_INHERIT_CODEX_CONFIG=false
DEBUGGING_RESULTS_DIR=./experiments
```

`DEBUGGING_JOBS=0` tự chọn mức parallelism an toàn. `DEBUGGING_TIMEOUT` là timeout
cho mỗi Codex attempt; `DEBUGGING_COMMAND_TIMEOUT` là timeout cho từng setup,
build hoặc test command.

## Bảo đảm validation

- Baseline và patched validation chạy trong hai bản sao cách ly. Bubblewrap mount
  host read-only và chỉ cho phép ghi vào snapshot tạm, nên input project không bị
  thay source hoặc giữ build artifact của patch.
- Baseline phải build được và phải có bằng chứng ít nhất một test thực sự chạy.
- Build/test plan của patched validation được chốt từ snapshot sạch trước khi
  apply diff; patch không thể thay validation contract để chọn lệnh test dễ hơn.
- Nếu baseline đã xanh, mặc định không tạo patch.
- Codex có quyền `workspace-write` và chạy lệnh trong snapshot tạm; có thể sửa một
  hoặc nhiều file project nhưng không có quyền ghi vào project đầu vào.
- `repair.paths` phải khớp chính xác mọi file trong diff. Diff nhiều file được
  apply nguyên khối lên một validation snapshot sạch.
- Generated/build output, cache và credential không được đưa vào patch; prompt
  cũng cấm làm yếu hoặc xoá test chỉ để tạo kết quả xanh.
- Raw diff của mọi attempt luôn được giữ để audit; output cũng là unified diff,
  không phải toàn bộ source file.
- `status=plausible` chỉ được ghi khi patched build và toàn bộ test command pass;
  patch fail vẫn được xuất nhưng giữ `status=failing` hoặc `invalid`.
