import unittest

from calc import add, clamp, multiply


class TestOps(unittest.TestCase):
    def test_add(self):
        self.assertEqual(add(2, 3), 5)
        self.assertEqual(add(-1, 1), 0)

    def test_multiply(self):
        self.assertEqual(multiply(4, 5), 20)

    def test_clamp(self):
        self.assertEqual(clamp(15, 0, 10), 10)
        self.assertEqual(clamp(-3, 0, 10), 0)
        self.assertEqual(clamp(4, 0, 10), 4)


if __name__ == "__main__":
    unittest.main()
