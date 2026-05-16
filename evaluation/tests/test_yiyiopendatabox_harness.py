import importlib.util
import os
import tempfile
import unittest


MODULE_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src", "agents", "yiyiopendatabox.py"))
spec = importlib.util.spec_from_file_location("yiyiopendatabox_harness", MODULE_PATH)
yiyi = importlib.util.module_from_spec(spec)
assert spec is not None and spec.loader is not None
spec.loader.exec_module(yiyi)


class YiYiOpenDataBoxHarnessTests(unittest.TestCase):
    def test_collect_outputs_prefers_expected_files_and_skips_trace(self):
        with tempfile.TemporaryDirectory() as td:
            out_dir = os.path.join(td, "model_output")
            os.makedirs(out_dir)
            expected = os.path.join(out_dir, "report.xlsx")
            trace = os.path.join(out_dir, "trace.txt")
            extra = os.path.join(out_dir, "notes.txt")
            for path in (expected, trace, extra):
                with open(path, "w", encoding="utf-8") as f:
                    f.write("x")

            self.assertEqual(yiyi._collect_outputs(out_dir, ["report.xlsx"]), [expected])

    def test_collect_outputs_skips_auxiliary_files_without_expected_names(self):
        with tempfile.TemporaryDirectory() as td:
            out_dir = os.path.join(td, "model_output")
            os.makedirs(out_dir)
            doc = os.path.join(out_dir, "report.doc")
            bak = os.path.join(out_dir, "generate.py.bak")
            for path in (doc, bak):
                with open(path, "w", encoding="utf-8") as f:
                    f.write("x")

            self.assertEqual(yiyi._collect_outputs(out_dir, []), [doc])

    def test_unset_placeholder_config_values_are_ignored(self):
        project_root = yiyi._default_project_root()
        self.assertEqual(
            yiyi._cargo_root(project_root, {"cargoRoot": "${YIYI_CARGO_ROOT}"}),
            os.path.join(project_root, "YiYi", "app", "src-tauri"),
        )

    def test_move_trace_files_removes_them_from_model_output(self):
        with tempfile.TemporaryDirectory() as td:
            out_dir = os.path.join(td, "model_output")
            raw_dir = os.path.join(td, "raw")
            os.makedirs(out_dir)
            os.makedirs(raw_dir)
            with open(os.path.join(out_dir, "trace.json"), "w", encoding="utf-8") as f:
                f.write("{}")

            yiyi._move_trace_files(out_dir, raw_dir)

            self.assertFalse(os.path.exists(os.path.join(out_dir, "trace.json")))
            self.assertTrue(os.path.exists(os.path.join(raw_dir, "yiyi_trace.json")))


if __name__ == "__main__":
    unittest.main()
