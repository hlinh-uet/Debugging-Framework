# Debugging-Framework

Tool tổng quát nhận trực tiếp **một project** và xuất ra **raw unified patch của
LLM cùng kết quả build/test cách ly**.

```text
/path/to/project
  -> discovery manifests/lockfiles/CI/Dockerfile/version files
  -> resolve EnvironmentSpec + plan digest
  -> reuse container Defects4C đang chạy (mặc định) hoặc provision local/OCI
  -> chạy failing test trên baseline sạch; bắt buộc phải reproduce failure
  -> Codex nhận baseline evidence và làm FL + APR
  -> apply patch vào bản sao sạch, cùng environment/plan digest
  -> target failing tests pass + regression suite pass
  -> xuất raw unified diff + plausible/failing/invalid
```

Framework không đọc ground truth hay gọi evaluator của `Unified-Debugging`. Với
project Defects4C, framework có thể reuse container đã được operator provision
theo Dockerfile của dataset (ví dụ `my_defects4c_libyang`) và chạy build/test
trong container đó.

## Input và output

Input của lệnh `run` gồm project root và ít nhất một tên/ID test đang fail:

```bash
python -m src run /home/halinh/Unified_Debugging/defects4c/data/my-project \
  --failing-test tests/test_api.py::test_retries
```

Có thể truyền `--failing-test` nhiều lần khi một bug làm fail nhiều test. Framework
resolve ID này thành target command theo test runner, chạy baseline trước FL/APR,
và chỉ chấp nhận patch khi các target test pass cùng regression suite.

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

Với project libyang đã materialize và container Defects4C đang chạy, lệnh thực
tế không cần cài native dependency trên host:

```bash
docker ps --format '{{.Names}}'
python3 -m src doctor \
  /home/halinh/Unified_Debugging/defects4c/data/CESNET___libyang__A.3__f128972045a5 \
  --environment-backend container
python3 -m src run \
  /home/halinh/Unified_Debugging/defects4c/data/CESNET___libyang__A.3__f128972045a5 \
  --failing-test utest_tree_schema_compile \
  --output /tmp/libyang-A3.patch
```

`doctor` phải thấy `my_defects4c_libyang`; nếu không, khởi container theo
`defects4c` hoặc đặt `DEFECTS4C_CONTAINER` trong `.env`.

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

### Shortcut cho Defects4C

Ngoài project path tổng quát, framework có adapter dataset riêng. Các shortcut
`--libyang`, `--fmt` (hoặc `--defects4c <alias>`) tự tìm recipe, project đã
materialize, container trong Dockerfile và failing test từ metadata:

```bash
python3 -m src run --libyang --bug-id A.3 --output /tmp/libyang-A3.patch
python3 -m src run --fmt --bug-id 6a1346405949ca1 --output /tmp/fmt.patch
python3 -m src run-batch --libyang --output-dir /tmp/libyang-patches
```

Nếu muốn ghi rõ test, vẫn có thể truyền `--failing-test`; option này được ưu tiên
hơn metadata. Bug id trùng nhau phải dùng commit SHA/prefix để chọn đúng version.
`run-batch --libyang` chạy lần lượt tất cả version materialized; thêm `--bug-id`
nếu chỉ muốn chạy một version trong batch.
Alias yêu cầu project đã được materialize dưới `defects4c/data`; ví dụ fmt:

```bash
cd /home/halinh/Unified_Debugging/defects4c
python3 prepare_project.py fmtlib___fmt --bug B
```

Root Defects4C mặc định là thư mục sibling `../defects4c`, có thể đổi bằng
`--defects4c-root` hoặc `DEBUGGING_DEFECTS4C_ROOT`.

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

Artifact chính của một run gồm `baseline.json`, `baseline/`,
`attempts/attempt_NN/validation/`, `run_manifest.json` và `result.json`.
`result.json` giữ `plan_digest`, `environment_digest`, container/image digest,
target test và regression outcome.

Project đầu vào không bị chép đè: Codex và patched validation đều chạy trên
bản sao tạm; với backend mặc định, từng lệnh build/test chạy trong container
đang cung cấp qua `docker exec`; backend `local` dùng Bubblewrap với filesystem
host read-only. Log, response và artifact audit nằm trong
`<results-dir>/<project-name>/`.

Codex chạy với sandbox `workspace-write`, `approval_policy=never` trong snapshot
tạm nên có thể chỉnh file, chạy formatter/build/test và dùng `git diff`. Framework
đã resolve/provision môi trường và gửi baseline evidence; Codex không còn là thành
phần quyết định environment contract. Mọi side effect trong snapshot này bị xoá
sau attempt. Toàn bộ log baseline được lưu audit ở `baseline/baseline-output.txt`
và được chép vào snapshot Codex tại `.debugging-framework/baseline-output.txt`;
prompt chỉ trỏ tới file này để không nhúng log dài vào context. `repair.diff` là
artifact có thẩm quyền:
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

## Discovery, environment provisioning, build và test

Lệnh `run` có sáu stage: discovery, environment resolution, provisioning,
baseline reproduction, FL/APR, và clean validation. EnvironmentSpec ghi lại
manifest, lockfile, CI, version file, system package và digest của các file bằng
chứng. Plan và environment digest phải giống nhau giữa baseline và patched run.

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

Patched validation bắt đầu từ source snapshot sạch và provision lại cùng
EnvironmentSpec. Venv,
`node_modules`, vendor tree, package cache và build output chỉ tồn tại trong
snapshot rồi bị xóa; chúng không được copy về input project và không xuất hiện
trong patch. Nếu discovery hoặc environment resolution không xác định được
workflow, pipeline dừng với lỗi contract/environment rõ ràng trước khi publish
patch; Codex không được dùng để che giấu một baseline không tái hiện được.

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

Framework mặc định dùng backend `container`: tìm container từ
`DEFECTS4C_CONTAINER`, hoặc theo quy ước `my_defects4c_<project>` / `my_defects4c`,
chép workspace tạm vào đó, rồi chạy baseline/build/test bằng `docker exec`. Vì
image đã được operator dựng từ Dockerfile của dataset, native dependency không
cần cài trên host.

`local` là fallback/tuỳ chọn cho project thông thường và yêu cầu toolchain đã có
trên host. `oci` là backend tự build image generic; nó không tự dùng một
Dockerfile nằm ngoài project root. `auto` ưu tiên container đang chạy, sau đó OCI,
cuối cùng local.

Container có sẵn phải được chuẩn bị một lần bởi operator. Framework không tự
`apt install` lên host và không tự khởi container vì việc đó phụ thuộc policy,
volume mount và image của dataset.

Vì vậy giao diện cho người dùng cuối vẫn là đúng một lệnh:

```bash
debugging-framework run /path/to/project \
  --failing-test tests/test_api.py::test_retries \
  --output /tmp/fix.patch
```

Có thể chỉ rõ container:

```bash
export DEFECTS4C_CONTAINER=my_defects4c_libyang
debugging-framework --environment-backend container run /path/to/project \
  --failing-test tests/test_api.py::test_retries
```

Hoặc cấu hình `DEBUGGING_ENVIRONMENT_CONTAINER` trong `.env`.

Nếu nhận source upload không tin cậy từ nhiều người dùng, hãy chạy mỗi framework
job trong container/VM riêng và không mount credential của host. Bubblewrap ở
đây bảo đảm command không ghi ngược input/host, nhưng profile read-only hiện tại
không được xem là ranh giới multi-tenant thay cho container/VM.

## Cài đặt framework

Yêu cầu Python `>=3.10`, Git, Codex CLI và Docker/Podman với container đã chạy
khi dùng backend mặc định `container`. Backend `local` cần Bubblewrap (`bwrap`).
Nếu backend đã chọn không khả dụng, validation fail-closed thay vì chạy lệnh
project ngoài môi trường đã chỉ định.

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

Đây là lệnh duy nhất cần chạy cho mỗi project. Framework tự discovery, provision,
tái hiện baseline, chạy FL/APR, rồi validate target test và regression suite.

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
DEBUGGING_ENVIRONMENT_BACKEND=container
DEBUGGING_ENVIRONMENT_RUNTIME=auto
DEBUGGING_ENVIRONMENT_CONTAINER=
```

`DEBUGGING_JOBS=0` tự chọn mức parallelism an toàn. `DEBUGGING_TIMEOUT` là timeout
cho mỗi Codex attempt; `DEBUGGING_COMMAND_TIMEOUT` là timeout cho từng setup,
build hoặc test command.

## Bảo đảm validation

- Baseline failing test luôn chạy trước Codex; baseline output và raw diff được lưu
  trước khi pipeline bắt đầu patch validation.
- Nếu failing test không reproduce được, pipeline dừng với `baseline_not_reproduced`;
  không gọi Codex và không publish patch xanh.
- Target failing test được chạy riêng sau patch, sau đó regression suite được chạy
  trên cùng environment/plan digest.
- EnvironmentSpec, image/runtime, file evidence và digest được lưu trong result.
- Patched validation chạy trên một bản sao cách ly. Container được chép workspace
  tạm vào đường dẫn riêng rồi xoá sau run; backend local dùng Bubblewrap mount
  host read-only. Vì vậy input project không bị thay source hoặc giữ build
  artifact của patch.
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
| `plausible` | Target failing tests và regression suite đều pass |
| `failing` | Patch apply được nhưng target/regression test còn fail |
| `invalid` | Baseline/environment/contract/patch không xác minh được |

`invalid` được giữ riêng cho lỗi hạ tầng/contract, chẳng hạn không build được,
không xác minh được test đã chạy hoặc diff không apply được. Raw diff vẫn được
publish trong cả hai trường hợp `failing` và `invalid`; `patch_validation_passed`
cho biết diff có pass validation hay không.
