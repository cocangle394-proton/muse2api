"""Offline parser regression tests; standard library only, no CDP import."""
import ast
from pathlib import Path
import re
import unicodedata
import unittest

SOURCE = Path(__file__).resolve().parents[1] / "engine.py"
SOURCE_BYTES = SOURCE.read_bytes()
tree = ast.parse(SOURCE_BYTES, filename=str(SOURCE))
engine = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "MuseEngine")
parser = next(n for n in engine.body if isinstance(n, ast.FunctionDef) and n.name == "_parse_quota")
# Compile the actual method unchanged. Avoid importing browser dependencies or
# constructing an engine, which could otherwise create profile directories.
subset = ast.Module(body=[ast.ClassDef(name="MuseEngine", bases=[], keywords=[],
                                     body=[parser], decorator_list=[])], type_ignores=[])
namespace = {"re": re, "unicodedata": unicodedata}
exec(compile(ast.fix_missing_locations(subset), str(SOURCE), "exec"), namespace)
parse = namespace["MuseEngine"]._parse_quota


class QuotaLocalizationTests(unittest.TestCase):
    def fields(self, text, **expected):
        result = parse(text)
        for key, value in expected.items():
            self.assertEqual(result.get(key), value, (key, result))
        return result

    def test_traditional_example(self):
        text = ("Settings\nGeneral\nMeta account\nUsage\nFree plan\n"
                "Weekly limit resets on Oct 30\n7% used\nAdditional tokens\nNever expires\n"
                "0% used (2B tokens left)\nUpgrade\nLanguage\nAppearance")
        result = self.fields(text, found=True, plan="Free plan",
                             weekly_reset="Oct 30", weekly_used_pct=7,
                             extra_used_pct=0, extra_expires="never",
                             extra_left="2B tokens left")
        self.assertNotIn("Meta account", result["raw"])
        self.assertNotIn("Language", result["raw"])

    def test_original_english(self):
        self.fields("Settings\nGeneral\nUsage\nFree plan\nWeekly limit resets on Sep 30\n"
                    "1% used\nAdditional tokens\nNever expires\n0% used (2B tokens left)\nLanguage",
                    found=True, plan="Free plan", weekly_reset="Sep 30",
                    weekly_used_pct=1, extra_used_pct=0, extra_expires="never",
                    extra_left="2B tokens left")

    def test_simplified_example(self):
        self.fields("Settings\nUsage\nFree plan\nWeekly limit resets on Oct 22\n12.5% used\n"
                    "Additional tokens\nNever expires\n0% used (2B tokens left)\nLanguage",
                    found=True, plan="Free plan", weekly_reset="Oct 22",
                    weekly_used_pct=12.5, extra_used_pct=0, extra_expires="never",
                    extra_left="2B tokens left")

    def test_zero_is_success_and_integer(self):
        for text in ["0% used", "0% used", "0% used", "０％ used"]:
            with self.subTest(text=text):
                result = self.fields(text, found=True, weekly_used_pct=0)
                self.assertIs(type(result["weekly_used_pct"]), int)

    def test_decimal_percentages(self):
        for text in ["12.75% used", "12.75% used", "１２．７５％ used"]:
            with self.subTest(text=text):
                self.fields(text, found=True, weekly_used_pct=12.75)

    def test_plan_only_is_not_success(self):
        for plan in ["Free plan", "Free plan", "Free plan", "Free tier"]:
            with self.subTest(plan=plan):
                result = self.fields(plan, found=False, plan=plan)
                self.assertNotIn("weekly_used_pct", result)

    def test_extra_only_is_not_weekly_usage(self):
        for text in ["Usage\nFree plan\nAdditional tokens\nNever expires\n0% used (2B tokens left)",
                     "Usage\nFree plan\nAdditional tokens\nNever expires\n0% used (2B tokens left)",
                     "Usage\nFree plan\nAdditional tokens\nNever expires\n20% used (1B tokens left)",
                     "Usage\nExtra tokens\n7.5% used (10 tokens left)"]:
            with self.subTest(text=text):
                result = self.fields(text, found=False)
                self.assertNotIn("weekly_used_pct", result)
                self.assertIn("extra_used_pct", result)

    def test_unrelated_and_empty_text(self):
        for text in ["", None, "Settings\nGeneral", "Upgrade\n100% discount"]:
            with self.subTest(text=text):
                self.fields(text, found=False)

    def test_profile_and_language_are_excluded(self):
        result = self.fields("Profile\n99% used\nUsage\nFree plan\n0% used\nLanguage\n88% used",
                             found=True, weekly_used_pct=0)
        self.assertNotIn("99%", result["raw"])
        self.assertNotIn("88%", result["raw"])

    def test_percentage_upper_bound(self):
        self.fields("101% used", found=False)
        self.fields("100% used", found=True, weekly_used_pct=100)


if __name__ == "__main__":
    unittest.main(verbosity=2)
