"""Raw-session ingestion tests: real protocols, immutable evidence, no truncation."""
import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from rsi_service.sessions import ingest_session, resolve_source, verify_session


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root / "source.jsonl"
        self.destination = self.root / "bundle"

    def tearDown(self):
        self.temporary.cleanup()

    def write(self, rows, trailing=True, path=None):
        path = path or self.source
        path.parent.mkdir(parents=True, exist_ok=True)
        text = "\n".join(json.dumps(row, ensure_ascii=False) for row in rows)
        path.write_text(text + ("\n" if trailing else ""), encoding="utf-8")
        return path

    def ingest(self, **kwargs):
        return ingest_session({"path": str(self.source), "format": "auto"}, self.destination, **kwargs)

    def test_rollout_complete_records_and_source_offsets(self):
        rows = [
            {"type": "session_meta", "payload": {"id": "session-1", "cwd": "/project"}},
            {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "修复失败"}]}},
            {"type": "response_item", "payload": {"type": "function_call", "call_id": "call-1", "name": "exec_command", "arguments": '{"cmd":"false"}'}},
            {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "call-1", "output": '{"exit_code":1,"output":"failed"}'}},
            {"type": "event_msg", "payload": {"type": "agent_message", "message": "observed"}},
            {"type": "future_record", "nested": {"anything": [1, None, "preserved"]}},
        ]
        self.write(rows, trailing=False)
        bundle = self.ingest(chunk_chars=300)
        self.assertTrue(verify_session(bundle))
        self.assertEqual(bundle["source"]["session_id"], "session-1")
        self.assertEqual(bundle["stats"]["events"], 6)
        self.assertEqual(bundle["stats"]["tool_calls"], 1)
        self.assertEqual(bundle["stats"]["failed_tools"], 1)
        raw = (self.destination / "raw.jsonl").read_bytes()
        self.assertEqual(raw, self.source.read_bytes())
        events = [json.loads(line) for line in Path(bundle["events_path"]).read_text().splitlines()]
        for event, record in zip(events, rows):
            self.assertEqual(event["record"], record)
            self.assertEqual(json.loads(raw[event["raw_start"]:event["raw_end"]]), record)

    def test_exec_and_unstructured_stdout_and_deduplicated_tools(self):
        self.write([
            {"type": "thread.started", "thread_id": "thread-1"},
            {"type": "item.started", "item": {"id": "item-1", "type": "command_execution", "command": "false", "status": "in_progress"}},
            {"type": "item.completed", "item": {"id": "item-1", "type": "command_execution", "exit_code": 1, "aggregated_output": "failure", "status": "failed"}},
            {"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 10}},
        ])
        self.source.write_bytes(b"startup warning\n" + self.source.read_bytes() + b"plain tail without newline")
        bundle = self.ingest()
        self.assertEqual(bundle["stats"]["unstructured"], 2)
        self.assertEqual(bundle["stats"]["tool_calls"], 1)
        self.assertEqual(bundle["stats"]["failed_tools"], 1)
        self.assertTrue(bundle["stats"]["warnings"])
        self.assertTrue(verify_session(bundle))

    def test_long_event_is_split_and_losslessly_reconstructed(self):
        self.write([{"type": "session_meta", "payload": {"id": "long"}},
                    {"type": "response_item", "payload": {"type": "message", "content": "终端输出\\\"\n" * 10000}}])
        bundle = self.ingest(chunk_chars=997)
        collected = {}
        for chunk in bundle["chunks"]:
            for fragment in json.loads(Path(chunk["path"]).read_text())["events"]:
                self.assertLessEqual(len(fragment["text"]), 997)
                collected.setdefault(fragment["event_id"], []).append(fragment)
        normalized = [json.loads(line) for line in Path(bundle["events_path"]).read_text().splitlines()]
        for event in normalized:
            fragments = collected[event["event_id"]]
            self.assertEqual(len(fragments), fragments[0]["part_count"])
            self.assertEqual([f["part_index"] for f in fragments], list(range(len(fragments))))
            self.assertEqual(json.loads("".join(f["text"] for f in fragments)), event)
        self.assertTrue(verify_session(bundle))

    def test_different_raw_sessions_have_disjoint_event_ids(self):
        self.write([{"type": "thread.started", "thread_id": "first"}])
        first = self.ingest()
        self.write([{"type": "thread.started", "thread_id": "second"}])
        second = ingest_session({"path": str(self.source)}, self.root / "second-bundle")
        self.assertFalse(set(first["event_ids"]) & set(second["event_ids"]))
        self.assertTrue(first["event_ids"][0].startswith("s" + first["source"]["sha256"][:16] + ":e"))
        self.assertTrue(verify_session(first))
        self.assertTrue(verify_session(second))

    def test_exact_discovery_in_sessions_and_archives(self):
        active, archived = self.root / "sessions", self.root / "archived_sessions"
        self.write([{"type": "session_meta", "payload": {"id": "wanted-longer"}}], path=active / "wanted.jsonl")
        actual = self.write([{"type": "thread.started", "thread_id": "wanted"}], path=archived / "unrelated-name.ndjson")
        source = {"session_id": "wanted", "roots": [str(active), str(archived)]}
        self.assertEqual(resolve_source(source), actual)
        self.assertEqual(ingest_session(source, self.destination)["source"]["session_id"], "wanted")
        with self.assertRaises(ValueError):
            resolve_source(dict(source, session_id="want"))
        self.write([{"type": "session_meta", "payload": {"id": "wanted"}}], path=active / "duplicate.jsonl")
        with self.assertRaisesRegex(ValueError, "multiple"):
            resolve_source(source)

    def test_overlapping_roots_do_not_create_false_conflict(self):
        nested = self.root / "sessions" / "nested"
        actual = self.write([{"type": "session_meta", "payload": {"id": "same"}}], path=nested / "a.jsonl")
        self.assertEqual(resolve_source({"session_id": "same", "roots": [str(nested.parent), str(nested)]}), actual)

    def test_discovery_rejects_conflicting_ids_within_one_file(self):
        self.write([{"type": "session_meta", "payload": {"id": "a"}},
                    {"type": "session_meta", "payload": {"id": "b"}}])
        with self.assertRaisesRegex(ValueError, "conflicting"):
            resolve_source({"session_id": "a", "roots": [str(self.root)]})

    def test_parent_and_fork_coexist_without_ancestor_matching(self):
        parent = self.write([{"type": "session_meta", "payload": {"id": "parent"}},
                             {"type": "event_msg", "payload": {"type": "user_message", "message": "parent history"}}],
                            path=self.root / "sessions" / "rollout-date-parent.jsonl")
        fork_rows = [
            {"type": "session_meta", "payload": {"id": "fork", "forked_from_id": "parent",
             "source": {"subagent": {"thread_spawn": {"parent_thread_id": "parent"}}}}},
            {"type": "session_meta", "payload": {"id": "parent"}},
            {"type": "event_msg", "payload": {"type": "user_message", "message": "parent history"}},
            {"type": "event_msg", "payload": {"type": "agent_message", "message": "fork continuation"}},
        ]
        fork = self.write(fork_rows, path=self.root / "sessions" / "custom-fork-name.jsonl")
        roots = [str(parent.parent)]
        self.assertEqual(resolve_source({"session_id": "parent", "roots": roots}), parent)
        self.assertEqual(resolve_source({"session_id": "fork", "roots": roots}), fork)
        bundle = ingest_session({"session_id": "fork", "roots": roots}, self.destination)
        self.assertEqual(bundle["source"]["session_id"], "fork")
        self.assertEqual((self.destination / "raw.jsonl").read_bytes(), fork.read_bytes())
        events = [json.loads(line) for line in Path(bundle["events_path"]).read_text().splitlines()]
        self.assertEqual([event["record"] for event in events], fork_rows)
        self.assertTrue(verify_session(bundle))
        parent.unlink()
        with self.assertRaisesRegex(ValueError, "no session"):
            resolve_source({"session_id": "parent", "roots": roots})

    def test_nested_fork_lineage_keeps_root_identity(self):
        self.write([
            {"type": "session_meta", "payload": {"id": "child", "forked_from_id": "parent"}},
            {"type": "session_meta", "payload": {"id": "parent", "forked_from_id": "grandparent"}},
            {"type": "session_meta", "payload": {"id": "grandparent"}},
            {"type": "event_msg", "payload": {"type": "agent_message", "message": "inherited history"}},
        ])
        bundle = self.ingest()
        self.assertEqual(bundle["source"]["session_id"], "child")
        self.assertEqual(bundle["stats"]["events"], 4)
        self.assertTrue(verify_session(bundle))

    def test_undeclared_late_identity_and_cyclic_forks_fail_closed(self):
        for rows in ([
            {"type": "session_meta", "payload": {"id": "child", "forked_from_id": "parent"}},
            {"type": "event_msg", "payload": {"type": "agent_message", "message": "history"}},
            {"type": "session_meta", "payload": {"id": "unrelated"}},
        ], [
            {"type": "session_meta", "payload": {"id": "child", "forked_from_id": "parent"}},
            {"type": "session_meta", "payload": {"id": "parent", "forked_from_id": "child"}},
        ]):
            self.write(rows)
            with self.assertRaises(ValueError):
                self.ingest()
            self.assertFalse(self.destination.exists())

    def test_discovery_reads_headers_not_all_tool_bodies_and_checks_filename(self):
        from rsi_service import sessions
        wanted = self.write([{"type": "session_meta", "payload": {"id": "wanted"}},
                             {"type": "event_msg", "payload": {"text": "tail"}}],
                            path=self.root / "custom.jsonl")
        unrelated = self.write([{"type": "session_meta", "payload": {"id": "different"}}]
                               + [{"type": "event_msg", "payload": {"text": "body"}}] * 1000,
                               path=self.root / "rollout-date-wanted.jsonl")
        with mock.patch.object(sessions, "_loads", wraps=sessions._loads) as parser:
            self.assertEqual(resolve_source({"session_id": "wanted", "roots": [str(self.root)]}), wanted)
            self.assertLessEqual(parser.call_count, 4)
        self.assertTrue(unrelated.exists())
        self.write([{"type": "session_meta", "payload": {"id": "wanted"}}], path=unrelated)
        with self.assertRaisesRegex(ValueError, "multiple"):
            resolve_source({"session_id": "wanted", "roots": [str(self.root)]})

    def test_legacy_tool_metadata_exit_code_and_custom_tool(self):
        self.write([{"type": "session_meta", "payload": {"id": "a"}},
                    {"type": "response_item", "payload": {"type": "custom_tool_call", "call_id": "c1", "input": "patch"}},
                    {"type": "response_item", "payload": {"type": "custom_tool_call_output", "call_id": "c1", "output": '{"metadata":{"exit_code":2}}'}}])
        bundle = self.ingest()
        self.assertEqual(bundle["stats"]["tool_calls"], 1)
        self.assertEqual(bundle["stats"]["failed_tools"], 1)

    def test_idempotence_and_changed_source_rejected(self):
        self.write([{"type": "session_meta", "payload": {"id": "a"}}])
        bundle = self.ingest()
        before = {path: Path(path).stat().st_mtime_ns for path in bundle["artifacts"]}
        self.assertEqual(self.ingest(), bundle)
        self.assertEqual(before, {path: Path(path).stat().st_mtime_ns for path in bundle["artifacts"]})
        self.source.write_text(self.source.read_text() + '{"type":"extra"}\n')
        with self.assertRaisesRegex(ValueError, "source differs"):
            self.ingest()
        self.assertTrue(verify_session(bundle))  # copy remains independently verifiable

    def test_existing_bundle_format_conflict_and_incomplete_destination_rejected(self):
        self.write([{"type": "thread.started", "thread_id": "a"}])
        self.ingest()
        with self.assertRaisesRegex(ValueError, "format conflicts"):
            ingest_session({"path": str(self.source), "format": "rollout"}, self.destination)
        incomplete = self.root / "incomplete"
        incomplete.mkdir()
        with self.assertRaises((OSError, ValueError)):
            ingest_session({"path": str(self.source)}, incomplete)
        self.assertEqual(list(incomplete.iterdir()), [])

    def test_tampering_rejected_for_every_artifact(self):
        self.write([{"type": "session_meta", "payload": {"id": "a"}}])
        bundle = self.ingest()
        for name in bundle["artifacts"]:
            path = Path(name)
            original = path.read_bytes()
            path.write_bytes(original + b" ")
            self.assertFalse(verify_session(bundle), name)
            with self.assertRaises((ValueError, OSError)):
                self.ingest()
            path.write_bytes(original)
        altered = copy.deepcopy(bundle)
        altered["event_ids"] = ["e99999999"]
        self.assertFalse(verify_session(altered))
        self.source.unlink()
        self.assertTrue(verify_session(bundle))

    def test_malformed_and_truncated_inputs_publish_nothing(self):
        bad = [b'{"type":"session_meta","payload":{"id":"a"}}\n{"truncated":',
               b'{"type":"session_meta","payload":{"id":"a"}}\nwarning\n',
               b'{"type":"thread.started","thread_id":"a"}\n{"broken":\n',
               b'{"duplicate":1,"duplicate":2}\n', b'{"value":NaN}\n',
               b'{"value":1e999}\n', b'{"value":"\\ud800"}\n', b'\xff\n', b'']
        for raw in bad:
            with self.subTest(raw=raw):
                self.source.write_bytes(raw)
                with self.assertRaises(ValueError):
                    self.ingest()
                self.assertFalse(self.destination.exists())
                self.assertFalse(list(self.root.glob(".bundle-*")))

    def test_exec_explicit_and_unknown_json_records(self):
        self.source.write_text("warning without metadata\n")
        with self.assertRaises(ValueError):
            self.ingest()
        bundle = ingest_session({"path": str(self.source), "format": "exec"}, self.destination)
        self.assertTrue(verify_session(bundle))
        shutil_destination = self.root / "unknown"
        self.write([{"type": "new_unknown_type", "data": [False, None]}, ["unknown", "array"],
                    {"type": {"unexpected": "preserved"}}, {"type": ["unknown"]}])
        unknown = ingest_session({"path": str(self.source)}, shutil_destination)
        self.assertEqual(unknown["stats"]["events"], 4)

    def test_limits_and_invalid_source_specs(self):
        self.write([{"type": "session_meta", "payload": {"id": "a"}}])
        for options in ({"max_bytes": 1}, {"max_bytes": False}, {"chunk_chars": 0}, {"chunk_chars": 1.1}):
            with self.assertRaises(ValueError):
                self.ingest(**options)
        for source in ({}, {"path": str(self.source), "session_id": "a"}, {"path": str(self.source), "format": "bad"},
                       {"session_id": "a", "roots": []}, {"path": str(self.root)}):
            with self.assertRaises(ValueError):
                resolve_source(source)

    def test_symlinks_and_special_files_rejected(self):
        self.write([{"type": "session_meta", "payload": {"id": "a"}}])
        link = self.root / "linked.jsonl"
        link.symlink_to(self.source)
        with self.assertRaises(ValueError):
            ingest_session({"path": str(link)}, self.destination)
        directory_link = self.root / "linked-dir"
        directory_link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            ingest_session({"path": str(directory_link / self.source.name)}, self.destination)
        fifo = self.root / "fifo.jsonl"
        os.mkfifo(fifo)
        with self.assertRaises(ValueError):
            ingest_session({"path": str(fifo)}, self.destination)
        bundle = self.ingest()
        raw = self.destination / "raw.jsonl"
        raw.unlink()
        raw.symlink_to(self.source)
        self.assertFalse(verify_session(bundle))

    def test_source_change_during_ingestion_is_rejected(self):
        self.write([{"type": "session_meta", "payload": {"id": "a"}}])
        from rsi_service import sessions
        original = sessions._tool_details
        changed = [False]

        def mutate(*args):
            if not changed[0]:
                changed[0] = True
                with self.source.open("ab") as stream:
                    stream.write(b'{"type":"extra"}\n')
            return original(*args)

        with mock.patch.object(sessions, "_tool_details", side_effect=mutate):
            with self.assertRaises(ValueError):
                self.ingest(max_bytes=200)
        self.assertFalse(self.destination.exists())

    def test_conflicting_formats_and_session_ids_rejected(self):
        for rows in ([{"type": "session_meta", "payload": {"id": "a"}}, {"type": "thread.started", "thread_id": "a"}],
                     [{"type": "session_meta", "payload": {"id": "a"}}, {"type": "session_meta", "payload": {"id": "b"}}]):
            self.write(rows)
            with self.assertRaises(ValueError):
                self.ingest()
        self.write([{"type": "session_meta", "payload": {"id": "a"}}])
        with self.assertRaises(ValueError):
            ingest_session({"path": str(self.source), "format": "exec"}, self.destination)


if __name__ == "__main__":
    unittest.main()
