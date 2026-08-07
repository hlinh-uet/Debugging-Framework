# Debugging-Framework

Tool FL + APR chạy trực tiếp trong **project hiện tại**, nhận test ID và actual
failing output, để Codex sửa workspace như Codex CLI rồi tự clean-validate patch.
Người dùng chịu trách nhiệm chuẩn bị dependency trong current environment hoặc
cung cấp một OCI image đã build; lệnh `repair` không tự cài dependency.

```text
current project + test ID + actual failing output
  -> discovery build/target/regression contract
  -> dùng current environment, prebuilt image hoặc running container
  -> Codex nhận baseline evidence và làm FL + APR
  -> Codex sửa trực tiếp current project
  -> apply patch vào snapshot ban đầu, cùng environment/plan digest
  -> target failing tests pass + regression suite pass
  -> xuất raw unified diff + plausible/cleanfix/noisefix/nonefix/negfix/invalid
```

Framework không đọc ground truth hay gọi evaluator của `Unified-Debugging`. Với
project Defects4C, framework có thể reuse container đã được operator provision
theo Dockerfile của dataset (ví dụ `my_defects4c_libyang`) và chạy build/test
trong container đó.

## Quick start: project hiện tại

Sau khi project đã có đủ dependency và failure output đã được lưu:

```bash
cd /path/to/project
debugging-framework repair \
  --test-id tests/test_api.py::test_retries \
  --failure-output /path/to/failure.log \
  --output /tmp/fix.patch
```

Project mặc định là `$PWD`; có thể truyền project path ở positional argument.
`repair` bắt buộc có ít nhất một test ID và failure output, không chạy lại
baseline, không tạo venv và không chạy package-manager install do auto-discovery.
Codex sửa trực tiếp project. Validation dùng snapshot chụp trước Codex nên không
tạm revert hay ghi build artifact vào source thật.

Actual output là evidence do caller cung cấp: framework ghi
`baseline_source=caller`, `baseline_observed=true` và
`baseline_reproduced=false`; framework không claim đã tự reproduce failure đó.

## Input và output

Input duy nhất của workflow là project root, ít nhất một test ID và output fail đã
được caller thu thập. Framework không chạy lại baseline ở bước này. Có thể truyền
`--failing-test` nhiều lần khi một bug làm fail nhiều test; framework resolve các ID
này thành target command theo test runner.

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
python3 -m src repair \
  /home/halinh/Unified_Debugging/defects4c/data/CESNET___libyang__A.3__f128972045a5 \
  --failing-test utest_tree_schema_compile \
  --failure-output /tmp/libyang-A3.log \
  --output /tmp/A.3.patch
```

Với project libyang đã materialize và container Defects4C đang chạy, lệnh thực
tế không cần cài native dependency trên host:

```bash
docker ps --format '{{.Names}}'
python3 -m src doctor \
  /home/halinh/Unified_Debugging/defects4c/data/CESNET___libyang__A.3__f128972045a5 \
  --environment-backend container
python3 -m src repair \
  /home/halinh/Unified_Debugging/defects4c/data/CESNET___libyang__A.3__f128972045a5 \
  --failing-test utest_tree_schema_compile \
  --failure-output /tmp/libyang-A3.log \
  --output /tmp/libyang-A3.patch
```

`doctor` phải thấy `my_defects4c_libyang`; nếu không, khởi container theo
`defects4c` hoặc đặt `DEFECTS4C_CONTAINER` trong `.env`.

### Shortcut cho Defects4C

Ngoài project path tổng quát, framework có adapter dataset riêng. Các shortcut
`--libyang`, `--fmt` (hoặc `--defects4c <alias>`) tự tìm recipe, project đã
materialize, container trong Dockerfile và failing test từ metadata:

```bash
python3 -m src repair --libyang --bug-id A.3 \
  --failing-test utest_tree_schema_compile \
  --failure-output /tmp/libyang-A3.log \
  --output /tmp/libyang-A3.patch
python3 -m src repair --fmt --bug-id 6a1346405949ca1 \
  --failing-test <test-id> \
  --failure-output /tmp/fmt.log \
  --output /tmp/fmt.patch
```

Nếu muốn ghi rõ test, truyền `--failing-test`; option này được ưu tiên hơn metadata.
Output fail vẫn là input bắt buộc. Bug id trùng nhau phải dùng commit SHA/prefix để
chọn đúng version.
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
python -m src repair /path/to/project \
  --failing-test tests/test_api.py::test_retries \
  --failure-output /tmp/failing-test.log \
  --output /tmp/fix.patch
```

`--output` và `--results-dir` phải nằm ngoài input project; cấu hình trỏ vào bên
trong project bị từ chối trước khi framework ghi hoặc xoá bất kỳ artifact nào.

Mọi phản hồi Codex được lưu tại `attempts/attempt_NN/codex.payload.json`; event,
stderr và prompt cũng được giữ cùng attempt. Mọi unified diff LLM trả về được lưu
nguyên văn tại `attempts/attempt_NN/llm.patch.diff`, kể cả khi diff không apply
được hoặc test vẫn fail. `--output` nhận diff sau normalize an toàn được chọn; hãy đọc `status`
hoặc `patch_validation_passed` trong result để biết patch đã qua validation hay
chưa. Trước khi parse/validate, framework chỉ normalize an toàn line ending,
Markdown/apply-patch wrapper và hunk line-count metadata; không tự đoán path hoặc
thay đổi nội dung thêm/xóa. Khi có thay đổi, bản đã normalize nằm tại
`attempts/attempt_NN/normalized.patch.diff` và `diff_normalization_actions` ghi rõ
các thao tác. Diff thiếu header/body vẫn bị đánh dấu invalid.

Artifact chính của một repair gồm `baseline.json`, `baseline/`,
`attempts/attempt_NN/validation/`, `run_manifest.json` và `result.json`.
`result.json` giữ `plan_digest`, `environment_digest`, container/image digest,
target test và regression outcome.

Ở chế độ `snapshot`, project đầu vào không bị chép đè: Codex và patched validation
đều chạy trên bản sao tạm. Ở chế độ `current`, Codex được cấp trực tiếp project
đầu vào và các thay đổi của Codex sẽ còn lại tại đó; patched validation vẫn chạy
trên snapshot ban đầu. Backend mặc định `current` chạy command bằng environment
đã gọi CLI; `image` khởi container tạm, `container` dùng `docker exec`, còn
`local` dùng Bubblewrap với filesystem host read-only. Log, response và artifact audit nằm trong
`<results-dir>/<project-name>/`.

Codex chạy với sandbox `workspace-write`, `approval_policy=never` trên workspace
được chọn nên có thể chỉnh file, chạy formatter/build/test và dùng `git diff`.
Framework resolve environment contract trước validation. Codex không là thành
phần quyết định environment contract. Mọi side effect trong snapshot bị xoá sau
attempt; ở chế độ `current`, thay đổi source của Codex được giữ lại. Toàn bộ log
caller cung cấp được lưu audit ở `baseline/baseline-output.txt`
và ở chế độ `snapshot` được chép vào snapshot Codex tại
`.debugging-framework/baseline-output.txt` (chế độ `current` ghi tạm rồi khôi phục
file này trong project);
prompt chỉ trỏ tới file này để không nhúng log dài vào context. `repair.diff` là
artifact có thẩm quyền:
framework áp lại toàn bộ diff lên một validation snapshot mới, vì vậy patch chỉ
pass nếu tự nó tái tạo được kết quả, không phụ thuộc file/cache mà Codex quên đưa
vào diff.

Trước khi gọi `repair`, caller phải bảo đảm build/test command chạy được trong
current environment hoặc image đã cung cấp. Với Defects4C, bước
`prepare_project.py` vẫn cần thiết để biến recipe/dataset thành source project:

```text
/home/halinh/Unified_Debugging/defects4c/data/
```

Framework không phụ thuộc vào layout trên; nó chỉ dùng project path được truyền
trên command line.

## Discovery, environment provisioning, build và test

Luồng `repair` có các stage: discovery, environment resolution, FL/APR và clean
validation. EnvironmentSpec ghi lại
manifest, lockfile, CI, version file, system package và digest của các file bằng
chứng. Plan và environment digest phải giống nhau giữa baseline evidence và patched
validation.

| Project marker | Plan auto-detect |
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

Với `repair`, các dependency-install step do bảng trên suy ra được loại khỏi
plan; caller phải cung cấp dependency sẵn hoặc thêm contract project rõ ràng.
Patched validation bắt đầu từ source snapshot sạch trong cùng EnvironmentSpec. Venv,
`node_modules`, vendor tree, package cache và build output chỉ tồn tại trong
snapshot rồi bị xóa; chúng không được copy về input project và không xuất hiện
trong patch. Nếu discovery hoặc environment resolution không xác định được
workflow, pipeline dừng với lỗi contract/environment rõ ràng trước khi publish
patch.

Với build system nội bộ không theo convention công khai, project có thể tự mô tả
command bằng `.debugging-framework.json` ở project root. Đây là cấu hình thuộc
chính input project, không phải adapter hoặc metadata bên ngoài:

```json
{
  "schema_version": 2,
  "system": "custom",
  "setup": [],
  "build": [["./scripts/build"]],
  "target_test": [
    {
      "command": ["./scripts/test", "--case", "{test_id}"],
      "cwd": ".",
      "evidence_pattern": "[1-9][0-9]* tests? (passed|failed)",
      "failure_pattern": "[1-9][0-9]* tests? failed"
    }
  ],
  "regression_test": [
    {
      "command": ["./scripts/test", "--all"],
      "cwd": ".",
      "evidence_pattern": "[1-9][0-9]* tests? (passed|failed)",
      "failure_pattern": "[1-9][0-9]* tests? failed"
    }
  ]
}
```

`{test_id}` được thay an toàn như một argument riêng. Nếu có nhiều test ID,
framework chạy target command một lần cho mỗi ID. Field legacy `test` vẫn được
hỗ trợ và được dùng làm cả target base lẫn regression khi hai field mới vắng mặt.

Command luôn chạy trực tiếp, không qua shell expansion. `cwd` phải nằm trong
project. Custom test command phải khai báo `evidence_pattern`; pattern phải khớp
output chứng minh ít nhất một test đã chạy. Với runner chuẩn, framework nhận diện
summary hoặc report JUnit/XML/TRX. Nếu custom runner dùng thông báo lỗi riêng,
thêm `failure_pattern` để phân biệt test fail với lỗi hạ tầng. Lệnh trả mã 0 nhưng
không có bằng chứng test bị đánh dấu `test_execution_unverified`; return code và
summary mâu thuẫn bị đánh dấu invalid, không được coi là pass.

Với project do `prepare_project.py` tạo, contract trỏ tới các script nằm ngay
trong project:

```json
{
  "schema_version": 2,
  "system": "defects4c-rendered-recipe",
  "build": [["bash", ".debugging-framework/recipe_build.sh"]],
  "target_test": [["bash", ".debugging-framework/recipe_target_test.sh", "{test_id}"]],
  "regression_test": [["bash", ".debugging-framework/recipe_regression_test.sh"]]
}
```

Các script này được render từ recipe của đúng bug trong lúc materialize. Vì
vậy validation về sau không cần quay lại đọc thư mục `defectsc_tpl/projects*`.
Materializer cũng xuất failure evidence tại
`.debugging-framework/failure-output.txt` khi metadata dataset có actual output.

## Phạm vi tự động hóa môi trường

Backend mặc định là `current`: build/test chạy bằng chính toolchain và dependency
đã có trong environment gọi CLI. Validation vẫn chạy trong source snapshot tạm,
nhưng không cài dependency cho lệnh `repair`.

`image` nhận image đã được caller build và khởi container tạm cho từng command:

```bash
debugging-framework repair \
  --test-id tests/test_api.py::test_retries \
  --failure-output /tmp/failure.log \
  --environment-backend image \
  --environment-image project-tests@sha256:...
```

`container` reuse một container đang chạy; phù hợp với Defects4C. `local` là
backend Bubblewrap. `oci` là backend OCI tương thích cho image đã provision.
`auto` ưu tiên running container, sau đó OCI runtime, cuối cùng current environment.

Container có sẵn phải được chuẩn bị một lần bởi operator. Framework không tự
`apt install` lên host và không tự khởi container vì việc đó phụ thuộc policy,
volume mount và image của dataset.

Giao diện chính cho người dùng cuối là:

```bash
cd /path/to/project
debugging-framework repair \
  --test-id tests/test_api.py::test_retries \
  --failure-output /tmp/failure.log \
  --output /tmp/fix.patch
```

Có thể chỉ rõ container:

```bash
debugging-framework repair /path/to/project \
  --test-id tests/test_api.py::test_retries \
  --failure-output /tmp/failure.log \
  --environment-backend container \
  --environment-container my_defects4c_libyang
```

Hoặc cấu hình `DEBUGGING_ENVIRONMENT_CONTAINER` trong `.env`.

Nếu nhận source upload không tin cậy từ nhiều người dùng, hãy chạy mỗi framework
job trong container/VM riêng và không mount credential của host. Bubblewrap ở
đây bảo đảm command không ghi ngược input/host, nhưng profile read-only hiện tại
không được xem là ranh giới multi-tenant thay cho container/VM.

## Cài đặt framework

Yêu cầu Python `>=3.10`, Git, Codex CLI và một project đã build/test được trong
current environment. Docker/Podman chỉ bắt buộc với backend `image`, `container`
hoặc `oci`; backend `local` cần Bubblewrap (`bwrap`).

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

Build wheel và cài như một tool dùng ở mọi project:

```bash
uv build --wheel
pipx install dist/debugging_framework-*.whl
```

Package public là `debugging_framework`; module lịch sử `src` vẫn được bundle để
giữ tương thích trong giai đoạn migrate.

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

Chạy workflow chính trên project hiện tại:

```bash
cd /path/to/project
debugging-framework repair \
  --test-id tests/test_api.py::test_retries \
  --failure-output /path/to/failing-test-output.txt \
  --attempts 2 --output /tmp/fix.patch
```

Framework discovery build/test contract, dùng caller-supplied evidence, chạy
FL/APR trực tiếp trên project, rồi validate target test và regression suite.

`--failing-output -` đọc output từ stdin. `repair` luôn yêu cầu output này và
không có nhánh tự chạy baseline.

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
DEBUGGING_CODEX_WORKSPACE=auto
DEBUGGING_RESULTS_DIR=./experiments
DEBUGGING_ENVIRONMENT_BACKEND=current
DEBUGGING_ENVIRONMENT_RUNTIME=auto
DEBUGGING_ENVIRONMENT_CONTAINER=
DEBUGGING_ENVIRONMENT_IMAGE=
```

`DEBUGGING_JOBS=0` tự chọn mức parallelism an toàn. `DEBUGGING_TIMEOUT` là timeout
cho mỗi Codex attempt; `DEBUGGING_COMMAND_TIMEOUT` là timeout cho từng setup,
build hoặc test command.

## Bảo đảm validation

- `repair` bắt buộc caller cung cấp output và không chạy lại failing test. Evidence
  được lưu nguyên vẹn nhưng không được ghi nhận là framework đã reproduce baseline.
- Target failing test được chạy riêng sau patch, sau đó regression suite được chạy
  trên cùng environment/plan digest.
- EnvironmentSpec, image/runtime, file evidence và digest được lưu trong result.
- Patched validation chạy trên snapshot chụp trước khi Codex sửa current project.
  Framework không tạm revert live checkout; container workspace/snapshot được xoá
  sau repair. Thay đổi Codex vẫn còn trong project giống Codex CLI.
- Patched validation phải build được và phải có bằng chứng ít nhất một test thực sự chạy.
- `repair` loại các bước dependency install do auto-detector suy ra vì caller đã
  chuẩn bị environment. Setup được khai báo rõ trong `.debugging-framework.json`
  vẫn là contract của project.
- Build/test plan của patched validation được chốt từ snapshot sạch trước khi
  apply diff; patch không thể thay validation contract để chọn lệnh test dễ hơn.
- Ở chế độ `snapshot`, Codex có quyền `workspace-write` trên bản sao tạm và không
  ghi vào project đầu vào. Ở chế độ `current`, Codex sửa trực tiếp project được
  truyền vào; các thay đổi đó được giữ lại sau repair, còn clean validation vẫn dùng
  bản sao sạch.
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
| `plausible` | Toàn bộ failing tests ban đầu và regression suite đều pass |
| `cleanfix` | Có failing test ban đầu được sửa, không phát sinh regression, nhưng vẫn còn test fail |
| `noisefix` | Có failing test được sửa nhưng đồng thời phát sinh regression |
| `nonefix` | Không sửa được failing test ban đầu và không phát sinh regression |
| `negfix` | Không sửa được failing test ban đầu và phát sinh regression |
| `invalid` | Baseline/environment/contract/patch không xác minh được |

`invalid` được giữ riêng cho lỗi hạ tầng/contract, chẳng hạn không build được,
không xác minh được test đã chạy hoặc diff không apply được. Raw diff vẫn được
publish trong cả hai trường hợp chưa đạt và `invalid`; `patch_validation_passed`
chỉ true với `plausible`.
