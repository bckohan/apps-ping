import apps_ping


# -- Unittest style tests -----------------------------------------------------

# from unittest import TestCase

# class ExampleTests(TestCase):
#     """Example unit tests."""

#     def test_placeholder(self):
#         """Replace with real tests."""
#         self.assertTrue(True)

# -- Pytest functional style tests --------------------------------------------


def test_example():
    """Example pytest functional test."""
    assert apps_ping.__title__ == "apps-ping"
