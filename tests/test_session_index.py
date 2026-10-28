import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from rsi_service.autonomous_models import prompt_for
from rsi_service import session_index
from rsi_service.session_index import build_index, read_events, verify_index
from rsi_service.sessions import ingest_session, verify_session


class SessionIndexTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="rsi-timeline-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root / "session.jsonl"

    def bundle(self, records):
        self.source.write_text("".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records), encoding="utf-8")
        return ingest_session({"path": str(self.source), "format": "auto"}, self.root / "bundle", chunk_chars=500)

    def rows(self, index):
        return [row for chunk in index["chunks"] for row in json.loads(Path(chunk["path"]).read_text())["events"]]

    def test_all_events_have_exact_byte_locations_and_utf8_full_lookup(self):
        records = [
            {"type": "session_meta", "payload": {"id": "complete"}},
            {"type": "response_item", "payload": {"type": "message", "role": "user",
                "content": [{"type": "input_text", "text": "修复包含中文与 emoji 🧪 的输入"}]}},
            {"type": "response_item", "payload": {"type": "function_call", "name": "exec_command",
                "arguments": '{"cmd":"false"}', "call_id": "tool-1"}},
            {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "tool-1",
                "output": '{"exit_code":7,"output":"failure"}'}},
            {"type": "future_event", "unrecognized": {"text": "Must not be dropped"}},
        ]
        bundle = self.bundle(records)
        before = Path(bundle["events_path"]).read_bytes()
        index = build_index(bundle, self.root / "index", chunk_chars=1100, preview_chars=40)
        self.assertTrue(verify_index(index))
        self.assertEqual(index["events"], len(records))
        rows = self.rows(index)
        self.assertEqual([row["event_id"] for row in rows], bundle["event_ids"])
        self.assertEqual(rows[1]["role"], "user")
        self.assertEqual(rows[2]["tool"], "exec_command")
        self.assertEqual(rows[3]["exit"], 7)
        self.assertEqual(rows[4]["type"], "future_event")
        offset = 0
        for row, original in zip(rows, records):
            self.assertEqual(row["events_offset"], offset)
            encoded = before[offset:offset + row["events_bytes"]]
            self.assertEqual(json.loads(encoded)["record"], original)
            self.assertEqual(row["full_chars"], len(encoded.decode("utf-8").rstrip("\n")))
            offset += row["events_bytes"]
        full = read_events(index, [rows[-1]["event_id"], rows[1]["event_id"]])
        self.assertEqual([json.loads(value)["record"] for value in full], [records[-1], records[1]])
        self.assertEqual(before, Path(bundle["events_path"]).read_bytes())
        self.assertTrue(verify_session(bundle))

    def test_long_payload_is_previewed_once_and_returned_without_truncation(self):
        text = "完整原文\n" * 20000 + "TAIL_SENTINEL"
        bundle = self.bundle([{"type": "thread.started", "thread_id": "long"},
                              {"type": "item.completed", "item": {"type": "agent_message", "text": text}}])
        index = build_index(bundle, self.root / "index", preview_chars=32)
        rows = self.rows(index)
        self.assertEqual(len(rows), 2)
        self.assertTrue(rows[1]["preview_truncated"])
        self.assertIn("[truncated]", rows[1]["preview"])
        self.assertNotIn("TAIL_SENTINEL", rows[1]["preview"])
        self.assertGreater(len(bundle["chunks"]), len(index["chunks"]) * 10)
        result = json.loads(read_events(index, [rows[1]["event_id"]])[0])
        self.assertEqual(result["record"]["item"]["text"], text)

    def test_zero_preview_keeps_complete_events_accessible(self):
        original = {"type": "thread.started", "thread_id": "no-preview"}
        bundle = self.bundle([original])
        index = build_index(bundle, self.root / "index", preview_chars=0)
        self.assertTrue(verify_index(index))
        self.assertEqual(json.loads(read_events(index, bundle["event_ids"])[0])["record"], original)
        self.assertEqual(build_index(bundle, self.root / "index", preview_chars=0), index)

    def test_duplicate_metadata_keeps_every_row_and_links_the_original(self):
        metadata = {"type": "turn_context", "payload": {"instructions": "Repeated template " * 500}}
        bundle = self.bundle([{"type": "session_meta", "payload": {"id": "duplicate"}}, metadata, metadata,
                              {"type": "future_metadata", "data": "not filtered"}])
        index = build_index(bundle, self.root / "index")
        rows = self.rows(index)
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[2]["duplicate_of"], rows[1]["event_id"])
        self.assertEqual(rows[2]["content_sha256"], rows[1]["content_sha256"])
        self.assertEqual(json.loads(read_events(index, [rows[2]["event_id"]])[0])["record"], metadata)

    def test_chunk_budget_counts_encoded_rows_and_never_splits_a_row(self):
        bundle = self.bundle([{"type": "thread.started", "thread_id": "bounds"}] +
                             [{"type": "future_event", "text": "x" * 200, "ordinal": i} for i in range(20)])
        for budget in (1, 1000):
            index = build_index(bundle, self.root / ("index-" + str(budget)), chunk_chars=budget, preview_chars=70)
            flattened = []
            for chunk in index["chunks"]:
                content = json.loads(Path(chunk["path"]).read_text())
                rows = content["events"]
                size = sum(len(json.dumps(row, ensure_ascii=False, sort_keys=True, allow_nan=False)) for row in rows)
                self.assertTrue(size <= budget or len(rows) == 1)
                self.assertEqual(content["raw_events_path"], bundle["events_path"])
                self.assertEqual(content["view"], "timeline")
                self.assertEqual(chunk["event_ids"], [row["event_id"] for row in rows])
                flattened.extend(chunk["event_ids"])
            self.assertEqual(flattened, bundle["event_ids"])
            self.assertTrue(verify_index(index))

    def test_idempotence_binds_source_parameters_and_manifest(self):
        bundle = self.bundle([{"type": "thread.started", "thread_id": "idempotent"}])
        destination = self.root / "index"
        first = build_index(bundle, destination)
        mtimes = {path: Path(path).stat().st_mtime_ns for path in first["artifacts"]}
        self.assertEqual(first, build_index(bundle, destination))
        self.assertEqual(mtimes, {path: Path(path).stat().st_mtime_ns for path in first["artifacts"]})
        with self.assertRaisesRegex(ValueError, "differs"):
            build_index(bundle, destination, preview_chars=241)
        altered = copy.deepcopy(first)
        altered["events"] += 1
        self.assertFalse(verify_index(altered))

    def test_changed_source_chunk_manifest_and_checksum_fail_closed(self):
        bundle = self.bundle([{"type": "thread.started", "thread_id": "tamper"}, {"type": "unknown", "text": "evidence"}])
        index = build_index(bundle, self.root / "index")
        targets = [Path(bundle["events_path"]), Path(index["chunks"][0]["path"]), Path(index["manifest_path"]),
                   Path(index["manifest_path"]).with_name("manifest.sha256"), Path(bundle["manifest_path"])]
        for target in targets:
            with self.subTest(target=target.name):
                original = target.read_bytes()
                target.write_bytes(original + b" ")
                self.assertFalse(verify_index(index))
                with self.assertRaises(ValueError):
                    read_events(index, [bundle["event_ids"][0]])
                target.write_bytes(original)
        self.assertTrue(verify_index(index))

    def test_publication_failure_leaves_no_partial_index(self):
        bundle = self.bundle([{"type": "thread.started", "thread_id": "atomic"}])
        destination = self.root / "index"
        with mock.patch("rsi_service.session_index.os.rename", side_effect=OSError("simulated publish failure")):
            with self.assertRaisesRegex(OSError, "publish failure"):
                build_index(bundle, destination)
        self.assertFalse(destination.exists())
        self.assertEqual(list(self.root.glob(".index-*")), [])
        self.assertTrue(verify_index(build_index(bundle, destination)))

    def test_bad_coverage_and_source_hash_do_not_publish(self):
        bundle = self.bundle([{"type": "thread.started", "thread_id": "identity"}])
        for mutate in ("id", "hash"):
            altered = copy.deepcopy(bundle)
            if mutate == "id":
                altered["event_ids"][0] = "unknown-id"
            else:
                altered["artifacts"][bundle["events_path"]] = "0" * 64
            destination = self.root / mutate
            with self.assertRaises(ValueError):
                build_index(altered, destination)
            self.assertFalse(destination.exists())

    def test_unknown_events_and_symlinks_are_rejected(self):
        bundle = self.bundle([{"type": "thread.started", "thread_id": "links"}])
        index = build_index(bundle, self.root / "index")
        with self.assertRaisesRegex(ValueError, "not present"):
            read_events(index, ["unknown-event"])
        chunk = Path(index["chunks"][0]["path"])
        moved = self.root / "moved.json"
        chunk.rename(moved)
        chunk.symlink_to(moved)
        self.assertFalse(verify_index(index))
        destination = self.root / "link-index"
        destination.symlink_to(self.root / "index", target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            build_index(bundle, destination)

    def test_cli_reads_selected_full_events_and_emits_nothing_on_integrity_failure(self):
        records = [{"type": "thread.started", "thread_id": "cli"}, {"type": "unknown", "text": "全文" * 500}]
        bundle = self.bundle(records)
        index = build_index(bundle, self.root / "index", preview_chars=10)
        argv = [sys.executable, "-m", "rsi_service.session_index", "read", "--index", index["manifest_path"],
                "--event", bundle["event_ids"][1], "--event", bundle["event_ids"][0]]
        result = subprocess.run(argv, capture_output=True, cwd=Path(__file__).resolve().parents[1], timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual([json.loads(row)["record"] for row in result.stdout.splitlines()], records[::-1])
        Path(index["manifest_path"]).write_text("{}\n")
        result = subprocess.run(argv, capture_output=True, cwd=Path(__file__).resolve().parents[1], timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"")

    def test_multi_event_lookup_verifies_the_large_source_once_per_call(self):
        bundle = self.bundle([{"type": "thread.started", "thread_id": "one-pass"}] +
                             [{"type": "unknown", "text": str(i)} for i in range(10)])
        index = build_index(bundle, self.root / "index", chunk_chars=1)
        original = session_index._open_regular
        with mock.patch("rsi_service.session_index._open_regular", wraps=original) as opened:
            records = read_events(index, bundle["event_ids"])
        self.assertEqual(len(records), len(bundle["event_ids"]))
        self.assertEqual(sum(str(call.args[0]) == bundle["events_path"] for call in opened.call_args_list), 1)

    def test_timeline_prompt_does_not_claim_full_payload_coverage(self):
        context = {"skill_dir": "/skill", "result_path": "/job/output/result.json"}
        prompt = prompt_for({}, "analyze", context, Path("/job/context.json"))
        self.assertIn("previews are routing aids, NOT the complete event payloads", prompt)
        self.assertIn("Cover all timeline rows", prompt)
        self.assertIn("retrieve the original normalized events", prompt)
        self.assertIn("session_index read", prompt)
        self.assertIn("For view full", prompt)


if __name__ == "__main__":
    unittest.main()
