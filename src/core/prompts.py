from __future__ import annotations

import json
from typing import Any, Optional


def _clip(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated {len(text) - limit} chars]"


def _test_context(tests: list) -> dict:
    failing = []
    passing_ids = []
    for test in tests or []:
        if not isinstance(test, dict):
            continue
        test_id = str(test.get("test_id") or "").strip()
        outcome = str(test.get("outcome") or "").strip().upper()
        if outcome in {"FAIL", "FAILED"}:
            coverage = test.get("covered_functions")
            if not isinstance(coverage, list):
                coverage = test.get("covered_methods")
            coverage = coverage if isinstance(coverage, list) else []
            failing.append(
                {
                    "test_id": test_id,
                    "outcome": outcome,
                    "expected_output": _clip(test.get("expected_output"), 3000),
                    "actual_output": _clip(test.get("actual_output"), 3000),
                    "fail_reason": _clip(test.get("fail_reason"), 3000),
                    "covered_functions": [str(item) for item in coverage[:120]],
                    "coverage_truncated": max(0, len(coverage) - 120),
                }
            )
        elif test_id:
            passing_ids.append(test_id)
    return {
        "failing_tests": failing,
        "passing_test_count": len(passing_ids),
        "passing_test_sample": passing_ids[:40],
    }


def build_codex_prompt(
    *,
    bug_id: str,
    dataset: str,
    tests: list,
    allowed_source_files: list[str],
    attempt: int,
    previous_attempt: Optional[dict] = None,
) -> str:
    context = {
        "bug_id": bug_id,
        "dataset": dataset,
        "attempt": attempt,
        "allowed_source_files": allowed_source_files,
        **_test_context(tests),
    }
    feedback = ""
    if previous_attempt:
        feedback = "\nKết quả attempt trước (hãy tránh lặp lại patch thất bại):\n" + _clip(
            json.dumps(previous_attempt, ensure_ascii=False, indent=2), 16000
        )

    return f"""Bạn là lõi Fault Localization (FL) + Automated Program Repair (APR).

Repository hiện tại là một worktree cách ly đã được dựng đúng buggy revision. Hãy
điều tra source/test trong repository, định vị nguyên nhân gốc và tạo một unified
diff nhỏ nhất có thể. Framework bên ngoài sẽ tự apply diff và validation.

Ràng buộc bắt buộc:
1. Không dùng ground truth, fixed revision, accepted patch, lịch sử commit hay internet.
2. Chỉ tạo diff cho ĐÚNG MỘT file production nằm trong `allowed_source_files` dưới đây.
3. Không sửa file trong worktree; không chạy lệnh git, không ghi file và không dùng
   tool apply patch. Chỉ đọc/điều tra source rồi trả diff trong JSON.
4. Không chạy full build/test suite. Framework bên ngoài sẽ validate bằng adapter chuẩn.
5. Giữ API/ABI và style hiện có; không refactor ngoài phạm vi lỗi.
6. Trước khi sửa, đọc đủ caller/callee và test liên quan để FL dựa trên bằng chứng.
7. Final response phải tuân thủ JSON Schema. `fault_localization` xếp giảm dần theo
   score 0..1; `path` là đường dẫn tương đối repo và `function` là symbol đầy đủ nếu có.
8. `repair.path` phải đúng file duy nhất trong diff.
9. `repair.diff` phải là unified diff raw, bắt đầu bằng `--- a/<path>` và
   `+++ b/<path>`, không bọc trong Markdown fence. Không trả full file thay cho diff.

Không có ground truth nào được cung cấp cho bạn. Dữ liệu lỗi chuẩn hóa:
{json.dumps(context, ensure_ascii=False, indent=2)}
{feedback}
Hãy bắt đầu bằng việc khám phá repository, sau đó trả JSON cuối cùng chứa FL và diff.
"""
