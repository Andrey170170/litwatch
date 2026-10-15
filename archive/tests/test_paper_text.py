"""A paper's text from its LaTeX source: the main file with its inputs, and its version."""

import tempfile
import unittest
from pathlib import Path

from arxiv_archive.source_text import NoTextSource, latex_text
from arxiv_archive.store import Store
from test_paper_store import tar_bytes


class LatexTextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.package = Path(self.tmp.name) / "package"

    def tearDown(self):
        self.tmp.cleanup()

    def text(self, members, compress=True):
        self.package.write_bytes(tar_bytes(members, compress))
        return latex_text(self.package, 10_000_000)

    def test_main_file_with_inputs_inlined_and_comments_dropped(self):
        text = self.text(
            [
                ("sections/intro.tex", b"Intro text. % a stale claim\n\\input{sections/detail}\n"),
                ("sections/detail.tex", b"Detail 50\\% better.\n"),
                ("appendix.tex", b"Appendix body.\n"),
                ("style.sty", b"\\newcommand{\\x}{y}\n"),
                (
                    "paper.tex",
                    b"\\documentclass{article}\n\\begin{document}\n\\input{sections/intro}\n"
                    b"\\include{appendix}\n% \\input{unused}\n\\end{document}\n",
                ),
            ]
        )
        self.assertIn("Intro text.", text)
        self.assertIn("Detail 50\\% better.", text)
        self.assertIn("Appendix body.", text)
        self.assertNotIn("stale claim", text)
        self.assertNotIn("\\input", text)
        self.assertNotIn("newcommand", text)  # a style file is not the paper
        self.assertLess(text.index("Intro text."), text.index("Appendix body."))

    def test_the_main_file_is_the_one_that_begins_the_document(self):
        text = self.text(
            [
                ("a_template.tex", b"\\documentclass{article}\n% no document here\n"),
                ("z_main.tex", b"\\documentclass{article}\n\\begin{document}Body\\end{document}\n"),
            ]
        )
        self.assertIn("Body", text)
        self.assertNotIn("no document here", text)

    def test_bibliography_takes_the_bbl_and_an_input_is_read_once(self):
        text = self.text(
            [
                (
                    "main.tex",
                    b"\\documentclass{article}\\begin{document}\\input{a}\\input{a}\n"
                    b"\\bibliography{refs}\\end{document}\n",
                ),
                ("a.tex", b"Once.\n"),
                (
                    "main.bbl",
                    b"\\begin{thebibliography}{1}\\bibitem{x} Vaswani et al.\\end{thebibliography}\n",
                ),
            ]
        )
        self.assertEqual(text.count("Once."), 1)
        self.assertIn("Vaswani et al.", text)
        self.assertNotIn("\\bibliography{refs}", text)

    def test_inputs_that_loop_or_are_missing_end(self):
        text = self.text(
            [
                (
                    "main.tex",
                    b"\\documentclass{article}\\begin{document}\\input{b}\\input{gone}\\end{document}",
                ),
                ("b.tex", b"B \\input{main} \\input{b}\n"),
            ]
        )
        self.assertIn("B", text)
        self.assertEqual(text.count("\\documentclass"), 1)

    def test_bare_inputs_and_paths_out_of_a_subdirectory_are_inlined(self):
        text = self.text(
            [
                (
                    "paper/main.tex",
                    b"\\documentclass{article}\\begin{document}\n\\input sections/one\n"
                    b"\\input{../shared/two}\n\\input{../../outside}\n\\inputencoding{utf8}\n"
                    b"\\end{document}",
                ),
                ("paper/sections/one.tex", b"Bare input.\n"),
                ("shared/two.tex", b"From the parent.\n"),
            ]
        )
        self.assertIn("Bare input.", text)
        self.assertIn("From the parent.", text)
        self.assertNotIn("\\input sections", text)
        self.assertIn("\\inputencoding{utf8}", text)  # not an input

    def test_import_and_subimport_are_inlined(self):
        text = self.text(
            [
                ("main.tex", b"\\documentclass{article}\\begin{document}\\import{chapters/}{one}\\end{document}"),
                ("chapters/one.tex", b"One \\subimport*{parts/}{two.tex}\n"),
                ("chapters/parts/two.tex", b"Two\n"),
            ]
        )
        self.assertIn("One", text)
        self.assertIn("Two", text)

    def test_a_commented_out_document_is_not_the_main_file(self):
        text = self.text(
            [
                ("a_template.tex", b"% \\documentclass{article}\n% \\begin{document}\nTemplate\n"),
                ("main.tex", b"\\documentclass{article}\\begin{document}Paper\\end{document}\n"),
            ]
        )
        self.assertIn("Paper", text)
        self.assertNotIn("Template", text)

    def test_the_entry_point_wins_over_a_preamble_it_inputs(self):
        text = self.text(
            [
                ("main.tex", b"\\input{preamble}\n\\begin{document}Body\\end{document}\n"),
                ("preamble.tex", b"\\documentclass{article}\n\\usepackage{amsmath}\n"),
            ]
        )
        self.assertIn("Body", text)
        self.assertIn("\\usepackage{amsmath}", text)

    def test_a_subfile_with_its_own_documentclass_is_not_the_entry_point(self):
        text = self.text(
            [
                ("main.tex", b"\\documentclass{article}\\begin{document}Main \\subfile{intro}\\end{document}"),
                ("intro.tex", b"\\documentclass[main.tex]{subfiles}\\begin{document}Intro\\end{document}"),
            ]
        )
        self.assertIn("Main", text)
        self.assertIn("Intro", text)

    def test_a_comment_after_a_line_break_is_dropped_and_an_escaped_percent_kept(self):
        text = self.text(
            [("main.tex", b"\\documentclass{article}\\begin{document}a & b \\\\% hidden claim\n"
                          b"50\\% better \\\\\\% kept % hidden\n\\end{document}")]
        )
        self.assertNotIn("hidden", text)
        self.assertIn("a & b \\\\\n", text)
        self.assertIn("50\\% better \\\\\\% kept", text)

    def test_without_a_main_file_every_latex_file_in_order_and_without_latex_none(self):
        self.assertEqual(
            self.text([("b.tex", b"Second"), ("a.tex", b"First")]), "First\n\nSecond\n"
        )
        self.assertEqual(
            self.text([("only.tex", b"Bare file \xe9t\xe9")], compress=False).strip(),
            "Bare file \u00e9t\u00e9",
        )  # latin-1, as old submissions are
        with self.assertRaises(NoTextSource):
            self.text([("refs.bib", b"@article{x,}")])


class StoreTextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "work").mkdir()
        (root / "archive").mkdir()
        self.store = Store(root / "work", root / "archive", guard=False)
        self.input = root / "input"

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def put(self, version, body):
        self.input.write_bytes(tar_bytes([("main.tex", body)]))
        self.store.put("2609.12345", version, "source", self.input)

    def test_text_names_the_version_it_is_of(self):
        self.put(1, b"\\documentclass{article}\\begin{document}First version\\end{document}")
        self.put(2, b"\\documentclass{article}\\begin{document}Second version\\end{document}")
        self.assertEqual(self.store.text("2609.12345", 1)["version"], 1)
        self.assertIn("First version", self.store.text("2609.12345", 1)["text"])
        newest = self.store.text("2609.12345")
        self.assertEqual(newest["version"], 2)
        self.assertIn("Second version", newest["text"])
        with self.assertRaises(FileNotFoundError):
            self.store.text("2609.12345", 3)

    def test_historical_latex_has_no_known_version(self):
        import pyarrow as pa
        import pyarrow.parquet as pq

        shard = self.store.archive / "hf/latex/example.parquet"
        shard.parent.mkdir(parents=True)
        body = b"\\documentclass{article}\\begin{document}Historical\\end{document}"
        pq.write_table(
            pa.table(
                {
                    "paper_id": ["2609.12345"],
                    "path": ["main.tex"],
                    "size": [len(body)],
                    "content": [body],
                }
            ),
            shard,
        )
        self.store.catalog.db.execute(
            "INSERT INTO latex_locations VALUES (?,?,?)",
            ("2609.12345", "hf/latex/example.parquet", 0),
        )
        self.store.catalog.db.commit()
        found = self.store.text("2609.12345")
        self.assertIsNone(found["version"])
        self.assertIn("Historical", found["text"])
        with self.assertRaises(FileNotFoundError):
            self.store.text("2609.12345", 1)  # an explicit version is never the historical LaTeX
        self.put(2, b"\\documentclass{article}\\begin{document}Second version\\end{document}")
        self.assertEqual(self.store.text("2609.12345")["version"], 2)
        historical = self.store.text("2609.12345", 0)  # asked for by name, beside a newer source
        self.assertIsNone(historical["version"])
        self.assertIn("Historical", historical["text"])


if __name__ == "__main__":
    unittest.main()
