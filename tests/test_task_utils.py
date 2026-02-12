"""
Tests for pyruns.utils.task_utils — task name validation.
"""
import pytest

from pyruns.utils.task_utils import validate_task_name


class TestValidateTaskName:
    def test_valid_names(self):
        assert validate_task_name("hello") is None
        assert validate_task_name("my-experiment") is None
        assert validate_task_name("run_001") is None
        assert validate_task_name("中文任务名") is None
        assert validate_task_name("test 123") is None

    def test_empty_name(self):
        err = validate_task_name("")
        assert err is not None
        assert "empty" in err.lower()

    def test_whitespace_only(self):
        err = validate_task_name("   ")
        assert err is not None

    def test_too_long(self):
        err = validate_task_name("a" * 201)
        assert err is not None
        assert "long" in err.lower()

    def test_exactly_200(self):
        assert validate_task_name("a" * 200) is None

    def test_invalid_chars(self):
        for bad in ['a<b', 'a>b', 'a:b', 'a"b', 'a/b', 'a\\b', 'a|b', 'a?b', 'a*b']:
            err = validate_task_name(bad)
            assert err is not None, f"Should reject: {bad}"

    def test_dot_names(self):
        assert validate_task_name(".") is not None
        assert validate_task_name("..") is not None

    def test_dot_prefix_ok(self):
        # ".hidden" should be ok (only exactly "." and ".." are forbidden)
        assert validate_task_name(".hidden") is None

