# Debugging-Framework

Tool FL + APR cho project C/C++, nhận test ID và actual failing output, để Codex
sửa một snapshot tạm rồi tự clean-validate patch.
Environment phải được chọn rõ: `host` đã cài đầy đủ hoặc OCI `image` đã đóng
gói đầy đủ. Framework không tự cài dependency, không build/pull image và không
fallback sang môi trường khác.

```text
source project + test ID + actual failing output + host/image environment
  -> discovery build/target/regression contract
  -> kiểm tra đúng environment đã khai báo (không fallback)
  -> Codex nhận baseline evidence, làm FL + APR trên snapshot mặc định
  -> apply patch vào snapshot ban đầu, cùng environment/plan digest
  -> target failing tests pass + regression suite pass
  -> xuất raw unified diff + plausible/cleanfix/noisefix/nonefix/negfix/invalid
```

## Quick start: contract hai file

Thiết lập một lần tại project root. `init` giữ nguyên custom build/test contract
nếu project đã có `.debugging-framework.json`, tạo `repair` settings, tạo thư mục
chứa failure log và thêm log đó vào `.gitignore`.

```bash
cd /path/to/project
debugging-framework init --environment host \
  --test-id tests/test_api.py::test_retries
```

Sau mỗi lần thu được output lỗi, ghi nó vào
`.debugging-framework/failure.log`, rồi chạy bằng đúng hai path input:

```bash
debugging-framework repair \
  --config .debugging-framework.json \
  --failure-output .debugging-framework/failure.log
```

Config tối thiểu do `init` tạo là:

```json
{
  "schema_version": 5,
  "repair": {
    "failing_tests": ["tests/test_api.py::test_retries"],
    "attempts": 2
  },
  "environment": {"mode": "host"}
}
```

Codex và validation luôn chạy trên snapshot tạm, không sửa checkout đầu vào. Một
file chỉ có `repair`/`environment` vẫn dùng auto-detection build/test native của
project.

## Hai input bắt buộc

Sau khi failure output đã được lưu:

```bash
cd /path/to/project
debugging-framework repair \
  --config /path/to/project/.debugging-framework.json \
  --failure-output /path/to/failure.log \
  --output /tmp/fix.patch
```

Project root được suy ra từ thư mục chứa `--config`; `repair` không nhận project
path riêng, không tự tìm config/log khác, không nhận failure output qua stdin và không chạy
lại baseline. Test ID cùng environment nằm trong config; actual output nằm trong
file thứ hai.

Actual output là evidence do caller cung cấp: framework ghi
`baseline_source=caller`, `baseline_observed=true` và
`baseline_reproduced=false`; framework không claim đã tự reproduce failure đó.

## Input và output

Input của workflow là `.debugging-framework.json` ở project root và một file
actual failing output không rỗng. Config chứa ít nhất một test ID cùng environment
contract. Framework không chạy lại baseline ở bước này. Có thể khai báo nhiều
test ID khi một bug làm fail nhiều test; framework resolve các ID này thành target
command theo test runner. Hai input được copy vào `inputs/` trong results kèm
SHA-256 để audit.

```text
project-root/
├── .debugging-framework.json
├── failure_output.log
├── src/
└── tests/
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
debugging-framework repair \
  --config /path/to/project/.debugging-framework.json \
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
`result.json` giữ `plan_digest`, `environment_digest`, image digest,
target test và regression outcome.

Project đầu vào không bị chép đè: Codex và patched validation chạy trên các bản
sao tạm độc lập. Environment execution chỉ có `host` hoặc `image`; không có
backend mặc định hay fallback. Log, response và artifact audit nằm trong
`<results-dir>/<project-name>/`.

Codex chạy với sandbox `workspace-write`, `approval_policy=never` trên snapshot
nên có thể chỉnh file, chạy formatter/build/test và dùng `git diff`.
Framework resolve environment contract trước validation. Codex không là thành
phần quyết định environment contract. Mọi side effect trong snapshot bị xoá sau
attempt. Toàn bộ log caller cung cấp được lưu audit ở
`baseline/baseline-output.txt` và được chép vào snapshot Codex tại
`.debugging-framework/baseline-output.txt`; prompt chỉ trỏ tới file này để không
nhúng log dài vào context. `repair.diff` là artifact có thẩm quyền:
framework áp lại toàn bộ diff lên một validation snapshot mới, vì vậy patch chỉ
pass nếu tự nó tái tạo được kết quả, không phụ thuộc file/cache mà Codex quên đưa
vào diff.

Với mode `image`, caller phải cung cấp image đã chứa compiler, runtime và mọi
dependency.

## Discovery, environment, build và test

Luồng `repair` có các stage: discovery, environment resolution, FL/APR và clean
validation. EnvironmentSpec ghi lại
manifest, lockfile, CI, version file và digest của các file bằng
chứng. Plan và environment digest phải giống nhau giữa baseline evidence và patched
validation.

| Project marker | Plan auto-detect |
| --- | --- |
| `CMakeLists.txt` | configure CMake, build, `ctest --output-on-failure` |
| `meson.build` | Meson setup/compile/test |
| `Makefile`, `configure[.ac]` | Make/Autotools build và target `test`/`check` |
| Bazel, Ninja | build/test command chuẩn tương ứng |

Auto-detector chỉ hỗ trợ build system C/C++ ở bảng trên và không cài dependency.
Build output chỉ tồn tại trong snapshot rồi bị xóa; không được copy về input
project hoặc đưa vào patch. Với test runner/build system khác, project phải khai
báo command rõ ràng. Nếu discovery hoặc environment resolution không xác định
được workflow, pipeline dừng với lỗi contract/environment trước khi publish patch.

Với build system nội bộ không theo convention công khai, project có thể tự mô tả
command bằng `.debugging-framework.json` ở project root. Đây là cấu hình thuộc
chính input project, không phải adapter hoặc metadata bên ngoài:

```json
{
  "schema_version": 5,
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

Có thể thêm runtime settings vào chính file này mà không ảnh hưởng build/test
contract. `repair.failing_tests` là chuỗi hoặc list test ID;
`attempts`, `model`, `codex_timeout`, `command_timeout`, `jobs`,
`inherit_codex_config` và `output` là optional. `environment.mode` bắt buộc là
`host` hoặc `image`; mode `image` còn bắt buộc có `environment.image` và có thể
chọn `environment.runtime`. Field/mode cũ hoặc không hợp lệ bị từ chối trước khi
chạy. Trong `repair`, test/environment trong config là authoritative và CLI
override tương ứng bị từ chối. Không có environment default hoặc shortcut tự
chọn mode.

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

## Phạm vi tự động hóa môi trường

Framework chỉ hỗ trợ đúng hai mode explicit:

- `host`: chạy command trong source snapshot bằng toolchain/dependency đã cài
  sẵn trên host.
- `image`: chạy command trong container tạm bằng image đã build sẵn.

Ví dụ image mode:

```json
{
  "schema_version": 5,
  "repair": {"failing_tests": ["tests/test_api.py::test_retries"]},
  "environment": {
    "mode": "image",
    "runtime": "docker",
    "image": "project-tests@sha256:..."
  }
}
```

Framework không tự cài package/dependency, không tự build/pull image, không reuse
container đang chạy và không đổi từ `image` sang `host` khi lỗi. `doctor` kiểm tra
host executable hoặc kiểm tra runtime + image local trước khi repair.

Giao diện chính cho người dùng cuối là:

```bash
cd /path/to/project
debugging-framework repair \
  --config .debugging-framework.json \
  --failure-output failure_output.log \
  --output /tmp/fix.patch
```

Project config chứa test và environment contract:

```json
{
  "schema_version": 5,
  "repair": {
    "failing_tests": ["tests/test_api.py::test_retries"]
  },
  "environment": {"mode": "image", "runtime": "docker", "image": "project-tests:prepared"}
}
```

Nếu nhận source upload không tin cậy từ nhiều người dùng, hãy chạy mỗi framework
job trong container/VM riêng và không mount credential của host. Source snapshot
chỉ bảo vệ checkout đầu vào; mode `host` không phải ranh giới bảo mật multi-tenant.

## Cài đặt framework

Yêu cầu Python `>=3.10`, Git và Codex CLI. Mode `host` cần toàn bộ toolchain/test
dependency của project. Mode `image` cần Docker hoặc Podman và image local đã
đóng gói đầy đủ.

`pyproject.toml` là nguồn khai báo package/dependency duy nhất. Runtime không có
Python dependency ngoài standard library; extra `test` chỉ thêm `pytest`.

```bash
cd /path/to/Debugging-Framework
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

Build wheel và cài như một tool dùng cho các project C/C++ đối tác:

```bash
uv build --wheel
pipx install dist/debugging_framework-*.whl
```

Package public là `debugging_framework`; module `src` là implementation nội bộ.

## Commands

Xem plan được auto-detect nhưng chưa chạy build/test:

```bash
debugging-framework inspect /path/to/project --environment host
debugging-framework inspect /path/to/project --environment host --json
```

Kiểm tra môi trường và executable cần thiết:

```bash
debugging-framework doctor /path/to/project --environment host
```

Khởi tạo config một lần rồi chạy contract hai file:

```bash
cd /path/to/project
debugging-framework init --environment host --test-id tests/test_api.py::test_retries
# ghi output fail vào .debugging-framework/failure.log
debugging-framework repair \
  --config .debugging-framework.json \
  --failure-output .debugging-framework/failure.log
```

Chạy một repair job:

```bash
cd /path/to/project
debugging-framework repair \
  --config .debugging-framework.json \
  --failure-output /path/to/failing-test-output.txt \
  --attempts 2 --output /tmp/fix.patch
```

Framework discovery build/test contract, dùng caller-supplied evidence, chạy
FL/APR trên snapshot, rồi validate target test và regression suite.

`repair` không nhận stdin và không tự tìm failure output từ config. `--config`
cùng `--failure-output` đều bắt buộc; framework không có nhánh tự chạy baseline.

Validate lại một patch có sẵn bằng cùng cơ chế tổng quát:

```bash
debugging-framework validate /path/to/project /tmp/fix.patch --environment host
```

## Cấu hình Codex

Thứ tự cấu hình chung là CLI explicit, project config, shell environment/`.env`,
rồi default. Riêng partner-facing `repair` bắt buộc nhận config và failure-output
path rõ ràng; `.env` chỉ nên chứa cấu hình Codex chung và secret.

```dotenv
CODEX_API_KEY=
DEBUGGING_CODEX_MODEL=gpt-5.6-sol
DEBUGGING_CODEX_BIN=codex
DEBUGGING_RESULTS_DIR=./experiments
DEBUGGING_ENVIRONMENT_BACKEND=host
DEBUGGING_ENVIRONMENT_RUNTIME=auto
DEBUGGING_ENVIRONMENT_IMAGE=
```

Các tùy chọn timeout, số attempt và test ID nên đặt trong
`.debugging-framework.json`; các biến provider/API nâng cao chỉ cần khi dùng
backend Codex không mặc định.

## Bảo đảm validation

- `repair` bắt buộc có output file do caller cung cấp qua `--failure-output` và
  không chạy lại failing test. Evidence được lưu nguyên vẹn nhưng không được ghi
  nhận là framework đã reproduce baseline.
- Target failing test được chạy riêng sau patch, sau đó regression suite được chạy
  trên cùng environment/plan digest.
- EnvironmentSpec, image/runtime, file evidence và digest được lưu trong result.
- Patched validation chạy trên snapshot sạch độc lập; checkout đầu vào không bị
  thay đổi.
- Patched validation phải build được và phải có bằng chứng ít nhất một test thực sự chạy.
- Auto-detector không chạy dependency installer. Setup được khai báo rõ trong
  `.debugging-framework.json` vẫn là contract của project.
- Build/test plan của patched validation được chốt từ snapshot sạch trước khi
  apply diff; patch không thể thay validation contract để chọn lệnh test dễ hơn.
- Codex có quyền `workspace-write` chỉ trên bản sao tạm; mọi side effect ngoài
  unified diff được chọn đều bị loại sau attempt.
- `repair.paths` phải khớp chính xác mọi file trong diff. Diff nhiều file được
  apply nguyên khối lên một validation snapshot sạch.
- Automatic validation chỉ cho phép patch vào production C/C++ source/header
  (`.c`, `.cc`, `.cpp`, `.cxx`, `.h`, `.hh`, `.hpp`, `.hxx`, `.inl`, `.inc`).
  Patch vào test/fixture, `Makefile`, `CMakeLists.txt`, Dockerfile, script hoặc
  config validation bị fail-closed với `test_oracle_modified=true` và không bao
  giờ được phân loại `plausible`; raw diff vẫn được publish để audit. Repair thực
  sự cần đổi build/test infrastructure phải đi qua workflow review riêng.
- Generated/build output, cache và credential không được đưa vào patch; prompt
  cũng cấm làm yếu hoặc xoá test chỉ để tạo kết quả xanh.
- Raw diff của mọi attempt luôn được giữ để audit; output cũng là unified diff,
  không phải toàn bộ source file.
- Validator ưu tiên ID từng test từ JUnit/XML/TRX và output test có cấu trúc. Nếu
  runner không cung cấp ID test, nó dùng ID cấp command và phân loại bảo thủ.

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
