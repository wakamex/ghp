import pathlib
import unittest


class PackagingTests(unittest.TestCase):
    def test_pyproject_declares_ghp_package_and_script(self):
        pyproject = pathlib.Path(__file__).resolve().parents[1] / "pyproject.toml"
        data = pyproject.read_text(encoding="utf-8")

        self.assertIn('name = "ghp"', data)
        self.assertIn('ghp = "ghp.cli:main"', data)


if __name__ == "__main__":
    unittest.main()
