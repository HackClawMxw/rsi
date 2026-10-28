"""Four fixed CPU cases for transparent loader callbacks; no third-party deps."""
import importlib.util
import json
from pathlib import Path


def validate(wrap):
    records = []
    for name, args, kwargs, raises in (
        ("positional", ("parameter", "weights"), {}, False),
        ("loaded_weight_keyword", ("parameter",), {"loaded_weight": "weights"}, False),
        ("all_keywords", (), {"param": "parameter", "loaded_weight": "weights", "shard_id": 3}, False),
        ("exception_identity", ("parameter",), {"loaded_weight": "weights", "shard_id": 7}, True),
    ):
        calls, marker, expected_error = [], object(), ValueError("original loader failure")

        def loader(*actual_args, **actual_kwargs):
            calls.append((actual_args, actual_kwargs))
            if raises:
                raise expected_error
            return marker

        try:
            wrapped = wrap(loader)
            if raises:
                try:
                    wrapped(*args, **kwargs)
                except ValueError as error:
                    assert error is expected_error, "wrapper replaced the loader exception"
                else:
                    raise AssertionError("wrapper swallowed the loader exception")
            else:
                assert wrapped(*args, **kwargs) is marker, "wrapper changed the return value"
            assert calls == [(args, kwargs)], "wrapper altered or duplicated the loader invocation"
            records.append({"case": name, "passed": True})
        except Exception as error:
            records.append({"case": name, "passed": False,
                            "error": type(error).__name__ + ": " + str(error)})
    return records


def main():
    root = Path(__file__).resolve().parent
    spec = importlib.util.spec_from_file_location("candidate_observer", root / "observer.py")
    observer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(observer)
    cases = validate(observer.wrap)
    report = {"fixture": "loader-observer-v1", "passed": all(row["passed"] for row in cases), "cases": cases}
    (root / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report), flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
