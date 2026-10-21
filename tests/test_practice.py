import unittest

from touken.flows.practice import PracticeMixin


class _Maa:
    def __init__(self, rows):
        self.rows = iter(rows)

    def ocr_all(self, roi):
        return [(text, None) for text in next(self.rows)]


class _Flow(PracticeMixin):
    def __init__(self, rows):
        self.device = _Maa(rows)


class PracticeResultTests(unittest.TestCase):
    def test_existing_result_stamps_are_counted(self):
        flow = _Flow([
            ["胜利", "特"],
            ["败北"],
            ["胜利优"],
            [],
            ["胜利", "良"],
        ])

        self.assertEqual(flow._scan_existing_results(), {
            0: "win", 1: "lose", 2: "win", 4: "win",
        })

    def test_unclear_text_is_not_treated_as_a_result(self):
        flow = _Flow([["胜"], ["挑战"], [], ["利"], ["完全"]])

        self.assertEqual(flow._scan_existing_results(), {})


if __name__ == "__main__":
    unittest.main()
