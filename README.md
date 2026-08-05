# Debugging-Framework

Tool tổng quát nhận trực tiếp **một project** và xuất ra **raw unified patch của
LLM cùng kết quả build/test cách ly**.

```text
/path/to/project
  -> tạo snapshot writable cách ly, không có git history thật, cho Codex
  -> Codex tự đọc manifest/CI/docs, cài dependency, build/test, làm FL + APR
  -> lưu nguyên phản hồi Codex và raw unified diff trước khi validation
  -> apply toàn bộ diff vào một bản sao validation mới
  -> tự provision/build/test lại đúng cùng phạm vi một lần
  -> xuất raw unified diff + plausible/failing/invalid
```

Framework không đọc metadata Defects4C, không cần ground truth, không dùng
loader/validator/evaluator của `Unified-Debugging`, không có adapter theo tên
project, và không cần Docker container riêng cho dataset.

## Input và output

Input của lệnh `run` gồm project root và ít nhất một tên/ID test đang fail:

```bash
python -m src run /home/halinh/Unified_Debugging/defects4c/data/my-project \
  --failing-test tests/test_api.py::test_retries
```

Có thể truyền `--failing-test` nhiều lần khi một bug làm fail nhiều test. Tên/ID
này được đưa nguyên vào prompt Codex để làm failure signal chính cho FL/APR.

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
  --failing-test tests/test_api.py::test_retries \
  --output /tmp/A.3.patch
```

Hoặc xử lý tuần tự toàn bộ 15 project từ manifest và tách patch theo version:

```bash
python3 -m src run-batch \
  /home/halinh/Unified_Debugging/defects4c/data/CESNET___libyang__materialized.json \
  --output-dir /tmp/libyang-patches
```

`run-batch` là vòng lặp qua các record trong manifest. Mỗi record cần có
`failing_tests` (list) hoặc `failing_test` (string); có thể dùng
`--failing-test` trên command line làm giá trị mặc định cho record thiếu field.
Với từng path, framework vẫn dùng đúng luồng project-in/patch-out như lệnh `run`.
Tổng hợp trạng thái được ghi tại `<results-dir>/batch_result.json`.

Ví dụ record:

```json
{
  "project_path": "/path/to/project",
  "failing_tests": ["tests/test_api.py::test_retries"]
}
```

Output mặc định:

```text
~/.local/state/debugging-framework/results/<project-name>/patch.diff
```

Nếu có `XDG_STATE_HOME`, framework dùng
`$XDG_STATE_HOME/debugging-framework/results`. Có thể override bằng global option
`--results-dir` hoặc `DEBUGGING_RESULTS_DIR`.

Có thể chọn đúng file output:

```bash
python -m src run /path/to/project --output /tmp/fix.patch
```

`--output` và `--results-dir` phải nằm ngoài input project; cấu hình trỏ vào bên
trong project bị từ chối trước khi framework ghi hoặc xoá bất kỳ artifact nào.

Mọi phản hồi Codex được lưu tại `attempts/attempt_NN/codex.payload.json`; event,
stderr và prompt cũng được giữ cùng attempt. Mọi unified diff LLM trả về được lưu
nguyên văn tại `attempts/attempt_NN/llm.patch.diff`, kể cả khi diff không apply
được hoặc test vẫn fail. `--output` nhận raw diff được chọn; hãy đọc `status`
hoặc `patch_validation_passed` trong result để biết patch đã qua validation hay
chưa.

Project đầu vào không bị chép đè: Codex và patched validation đều chạy trên
bản sao tạm; từng lệnh build/test còn chạy trong Bubblewrap với filesystem host
read-only và chỉ validation snapshot được ghi. Log, response và artifact audit
nằm trong `<results-dir>/<project-name>/`.

Codex chạy với sandbox `workspace-write`, `approval_policy=never` trong snapshot
tạm nên có thể chỉnh file, chạy package manager/formatter/build/test và dùng
`git diff`. Network của sandbox được bật để tải dependency do project khai báo;
prompt cấm cài package hệ thống hoặc tìm lời giải bên ngoài. Mọi side effect trong
snapshot này bị xoá sau attempt. `repair.diff` là artifact có thẩm quyền:
framework áp lại toàn bộ diff lên một validation snapshot mới, vì vậy patch chỉ
pass nếu tự nó tái tạo được kết quả, không phụ thuộc file/cache mà Codex quên đưa
vào diff.

Không cần tạo venv, chạy `npm install`, restore package hoặc build project trước
khi gọi framework. Với Defects4C, bước `prepare_project.py` vẫn cần thiết chỉ để
biến recipe/dataset thành một source project thật; nó đặt từng version tại:

```text
/home/halinh/Unified_Debugging/defects4c/data/
```

Framework không phụ thuộc vào layout trên; nó chỉ dùng project path được truyền
trên command line.

## Auto-provisioning, build và test

Lệnh `run` có hai stage. Trước hết Codex tự khám phá workflow và tự chạy các lệnh
cần thiết trong snapshot của nó để FL/APR. Chỉ sau khi raw diff đã được lưu,
validator mới tự nhận diện một plan xác định từ file chuẩn trong project, apply
diff vào một bản sao sạch và chạy plan đó một lần. `inspect` và `doctor` chỉ là
công cụ chẩn đoán validator, không phải bước bắt buộc.

| Project marker | Provisioning và validation tự động |
| --- | --- |
| `CMakeLists.txt` | configure CMake, build, `ctest --output-on-failure` |
| `meson.build` | Meson setup/compile/test |
| `Makefile`, `configure[.ac]` | Make/Autotools build và target `test`/`check` |
| `Cargo.toml` | `cargo fetch` theo lockfile, build và test toàn bộ target |
| `go.mod` | tải module, `go build ./...`, `go test ./...` |
| `pyproject.toml`, requirements/pytest markers | tạo venv riêng, pip install project/test dependency, pytest |
| `package.json` | chọn npm/pnpm/yarn; dùng frozen/clean install khi có lockfile; build/test scripts |
| `pom.xml`, Gradle files | Maven resolve/build/test; Gradle wrapper tự resolve khi build |
| `Package.swift` | resolve package, build và test |
| `.sln`, `.csproj` | .NET restore, build và test |
| Ruby, Composer | bundle/composer install rồi chạy test |
| Bazel, Ninja | build/test command chuẩn tương ứng |

Patched validation bắt đầu từ source snapshot sạch và tự provision lại. Venv,
`node_modules`, vendor tree, package cache và build output chỉ tồn tại trong
snapshot rồi bị xóa; chúng không được copy về input project và không xuất hiện
trong patch. Lỗi auto-detection không ngăn Codex tự điều tra project trước; chỉ
validation sau khi có diff mới yêu cầu build/test contract hợp lệ.

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

## Phạm vi tự động hóa môi trường

Framework tự cài **dependency cấp project** được khai báo trong manifest/lockfile.
Nó không chạy `apt`, `dnf` hay thay đổi host. Compiler/runtime/package-manager
(ví dụ JDK, Node, Cargo, CMake) là dependency của service chạy framework và nên
được đóng sẵn trong image khi expose thành tool. Dependency native đặc thù không
thể suy ra chắc chắn từ source; project phải khai báo lệnh tái lập trong field
`setup` của `.debugging-framework.json`, hoặc service phải cung cấp image/backend
phù hợp. Nếu thiếu executable hay setup thất bại, validation trả `setup_failed`
và dừng fail-closed, không giả vờ patch đã pass.

Vì vậy giao diện cho người dùng cuối vẫn là đúng một lệnh:

```bash
debugging-framework run /path/to/project \
  --failing-test tests/test_api.py::test_retries \
  --output /tmp/fix.patch
```

Việc cài framework/toolchain bên dưới là công việc một lần của người vận hành
service, không phải thao tác lặp lại cho từng project.

Nếu nhận source upload không tin cậy từ nhiều người dùng, hãy chạy mỗi framework
job trong container/VM riêng và không mount credential của host. Bubblewrap ở
đây bảo đảm command không ghi ngược input/host, nhưng profile read-only hiện tại
không được xem là ranh giới multi-tenant thay cho container/VM.

## Cài đặt framework

Yêu cầu Python `>=3.10`, Git, Codex CLI, Bubblewrap (`bwrap`) và các toolchain mà
deployment muốn hỗ trợ. Nếu thiếu `bwrap`, validation fail-closed thay vì chạy
lệnh project không cách ly.

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
debugging-framework run /path/to/project \
  --failing-test tests/test_api.py::test_retries \
  --attempts 2 --output /tmp/fix.patch
```

Đây là lệnh duy nhất cần chạy cho mỗi project. Codex tự setup, tái hiện lỗi, làm
fault localization/APR và trả diff trước; pipeline sau đó apply diff vào snapshot
sạch và validation lại patch.

Chạy mọi project đã materialize trong một manifest:

```bash
python -m src run-batch /path/to/materialized.json --output-dir /tmp/patches
```

Validate lại một patch có sẵn bằng cùng cơ chế tổng quát:

```bash
python -m src validate /path/to/project /tmp/fix.patch
```

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

- Codex luôn chạy trước trên một bản sao tạm; phản hồi JSON và raw diff được lưu
  trước khi pipeline bắt đầu patch validation.
- Patched validation chạy trên một bản sao cách ly. Bubblewrap mount
  host read-only và chỉ cho phép ghi vào snapshot tạm, nên input project không bị
  thay source hoặc giữ build artifact của patch.
- Patched validation phải build được và phải có bằng chứng ít nhất một test thực sự chạy.
- Dependency setup chạy tự động trước patched build/test; kết quả
  ghi rõ `setup_executed`, `environment_provisioned` và log từng setup command.
- Build/test plan của patched validation được chốt từ snapshot sạch trước khi
  apply diff; patch không thể thay validation contract để chọn lệnh test dễ hơn.
- Codex có quyền `workspace-write` và chạy lệnh trong snapshot tạm; có thể sửa một
  hoặc nhiều file project nhưng không có quyền ghi vào project đầu vào.
- `repair.paths` phải khớp chính xác mọi file trong diff. Diff nhiều file được
  apply nguyên khối lên một validation snapshot sạch.
- Generated/build output, cache và credential không được đưa vào patch; prompt
  cũng cấm làm yếu hoặc xoá test chỉ để tạo kết quả xanh.
- Raw diff của mọi attempt luôn được giữ để audit; output cũng là unified diff,
  không phải toàn bộ source file.
- Validator ưu tiên ID từng test từ JUnit/XML/TRX và output chuẩn của pytest,
  Cargo, CTest, Go. Nếu runner không cung cấp ID test, nó dùng ID cấp command và
  phân loại bảo thủ.

Kết quả validation phản ánh trực tiếp trạng thái của bản đã apply diff:

| `status` | Ý nghĩa |
| --- | --- |
| `plausible` | Diff apply được, build pass và tất cả test đã chạy đều pass |
| `failing` | Diff apply được nhưng có test fail |

`invalid` được giữ riêng cho lỗi hạ tầng/contract, chẳng hạn không build được,
không xác minh được test đã chạy hoặc diff không apply được. Raw diff vẫn được
publish trong cả hai trường hợp `failing` và `invalid`; `patch_validation_passed`
cho biết diff có pass validation hay không.
