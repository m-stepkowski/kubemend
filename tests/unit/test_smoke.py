"""Hello-world test: the package imports and reports a version.

Exists so M0's CI has something real to run. Replaced in substance by the
FakeLLM loop tests in M1.
"""

import kubemend


def test_version_is_populated() -> None:
    assert isinstance(kubemend.__version__, str)
    assert kubemend.__version__
