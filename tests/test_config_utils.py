"""
Tests for pyruns.utils.config_utils — core config & batch generation logic.
"""
import os
import json
import yaml
import pytest

from pyruns.utils.config_utils import (
    safe_filename,
    parse_value,
    flatten_dict,
    unflatten_dict,
    load_yaml,
    save_yaml,
    load_task_info,
    save_task_info,
    list_yaml_files,
    list_template_files,
    preview_config_line,
    _parse_pipe_value,
    generate_batch_configs,
    count_batch_configs,
    strip_batch_pipes,
)


# ═══════════════════════════════════════════════════════════════
#  safe_filename
# ═══════════════════════════════════════════════════════════════

class TestSafeFilename:
    def test_normal(self):
        assert safe_filename("hello-world") == "hello-world"

    def test_spaces_replaced(self):
        assert safe_filename("my file name") == "my_file_name"

    def test_special_chars_stripped(self):
        assert safe_filename("a/b:c*d") == "abcd"

    def test_empty_string(self):
        assert safe_filename("") == "config"

    def test_only_bad_chars(self):
        assert safe_filename("***") == "config"


# ═══════════════════════════════════════════════════════════════
#  parse_value
# ═══════════════════════════════════════════════════════════════

class TestParseValue:
    def test_int(self):
        assert parse_value("42") == 42

    def test_float(self):
        assert parse_value("3.14") == 3.14

    def test_bool_true(self):
        assert parse_value("True") is True
        assert parse_value("true") is True

    def test_bool_false(self):
        assert parse_value("False") is False
        assert parse_value("false") is False

    def test_list(self):
        assert parse_value("[1, 2, 3]") == [1, 2, 3]

    def test_string(self):
        assert parse_value("hello world") == "hello world"

    def test_pipe_string_stays_string(self):
        """Pipe syntax should NOT be parsed as a value — stays as string."""
        result = parse_value("0.001 | 0.01 | 0.1")
        assert isinstance(result, str)
        assert result == "0.001 | 0.01 | 0.1"


# ═══════════════════════════════════════════════════════════════
#  flatten / unflatten
# ═══════════════════════════════════════════════════════════════

class TestFlattenUnflatten:
    def test_flat_dict(self):
        d = {"a": 1, "b": 2}
        assert flatten_dict(d) == {"a": 1, "b": 2}

    def test_nested(self):
        d = {"model": {"name": "resnet", "layers": 50}, "lr": 0.01}
        flat = flatten_dict(d)
        assert flat == {"model.name": "resnet", "model.layers": 50, "lr": 0.01}

    def test_roundtrip(self):
        original = {"a": {"b": {"c": 1}}, "x": 2}
        assert unflatten_dict(flatten_dict(original)) == original

    def test_unflatten_simple(self):
        flat = {"a.b": 1, "a.c": 2, "d": 3}
        assert unflatten_dict(flat) == {"a": {"b": 1, "c": 2}, "d": 3}


# ═══════════════════════════════════════════════════════════════
#  YAML / JSON I/O
# ═══════════════════════════════════════════════════════════════

class TestYamlIO:
    def test_save_and_load(self, tmp_dir):
        data = {"lr": 0.01, "model": {"name": "vgg"}}
        path = str(tmp_dir / "test.yaml")
        save_yaml(path, data)
        loaded = load_yaml(path)
        assert loaded == data

    def test_load_nonexistent(self, tmp_dir):
        assert load_yaml(str(tmp_dir / "nope.yaml")) == {}

    def test_load_non_dict_yaml(self, tmp_dir):
        path = str(tmp_dir / "list.yaml")
        with open(path, "w") as f:
            f.write("- a\n- b\n")
        assert load_yaml(path) == {}

    def test_list_yaml_files(self, tmp_dir):
        for name in ["a.yaml", "b.yml", "c.txt"]:
            (tmp_dir / name).write_text("x: 1")
        result = list_yaml_files(str(tmp_dir))
        assert "a.yaml" in result
        assert "b.yml" in result
        assert "c.txt" not in result

    def test_list_yaml_files_missing_dir(self):
        assert list_yaml_files("/nonexistent/dir") == []


class TestTaskInfoIO:
    def test_save_and_load(self, tmp_dir):
        info = {"name": "test", "status": "pending", "env": {"CUDA": "0"}}
        save_task_info(str(tmp_dir), info)
        loaded = load_task_info(str(tmp_dir))
        assert loaded == info

    def test_load_missing(self, tmp_dir):
        assert load_task_info(str(tmp_dir)) == {}


# ═══════════════════════════════════════════════════════════════
#  list_template_files
# ═══════════════════════════════════════════════════════════════

class TestListTemplateFiles:
    def test_with_config_default(self, tasks_dir):
        result = list_template_files(tasks_dir)
        assert "config_default.yaml" in result

    def test_with_task_subfolder(self, tasks_dir):
        # Create a task subfolder with config.yaml
        task_dir = os.path.join(tasks_dir, "my-task")
        os.makedirs(task_dir)
        save_yaml(os.path.join(task_dir, "config.yaml"), {"x": 1})

        result = list_template_files(tasks_dir)
        assert "config_default.yaml" in result
        assert os.path.join("my-task", "config.yaml") in result

    def test_skips_dot_dirs(self, tasks_dir):
        trash = os.path.join(tasks_dir, ".trash")
        os.makedirs(trash)
        save_yaml(os.path.join(trash, "config.yaml"), {"x": 1})

        result = list_template_files(tasks_dir)
        # .trash should be skipped
        for key in result:
            assert ".trash" not in key

    def test_nonexistent_dir(self):
        assert list_template_files("/nonexistent") == {}


# ═══════════════════════════════════════════════════════════════
#  preview_config_line
# ═══════════════════════════════════════════════════════════════

class TestPreviewConfigLine:
    def test_basic(self):
        cfg = {"lr": 0.01, "bs": 32, "opt": "adam"}
        line = preview_config_line(cfg)
        assert "lr=0.01" in line
        assert "bs=32" in line

    def test_skips_dicts(self):
        cfg = {"lr": 0.01, "model": {"name": "resnet"}}
        line = preview_config_line(cfg)
        assert "model" not in line

    def test_max_items(self):
        cfg = {f"k{i}": i for i in range(10)}
        line = preview_config_line(cfg, max_items=2)
        assert line.count("=") == 2

    def test_non_dict_input(self):
        assert preview_config_line("not a dict") == ""


# ═══════════════════════════════════════════════════════════════
#  _parse_pipe_value
# ═══════════════════════════════════════════════════════════════

class TestParsePipeValue:
    def test_product_syntax(self):
        result = _parse_pipe_value("0.001 | 0.01 | 0.1")
        assert result is not None
        parts, mode = result
        assert mode == "product"
        assert parts == ["0.001", "0.01", "0.1"]

    def test_zip_syntax(self):
        result = _parse_pipe_value("(a | b | c)")
        assert result is not None
        parts, mode = result
        assert mode == "zip"
        assert parts == ["a", "b", "c"]

    def test_no_pipe(self):
        assert _parse_pipe_value("hello") is None
        assert _parse_pipe_value("0.001") is None

    def test_non_string(self):
        assert _parse_pipe_value(42) is None
        assert _parse_pipe_value([1, 2]) is None
        assert _parse_pipe_value(True) is None

    def test_single_value_in_parens(self):
        """(single_value) with no pipe should return None."""
        assert _parse_pipe_value("(only_one)") is None

    def test_single_value_with_pipe_in_parens(self):
        result = _parse_pipe_value("(a | b)")
        assert result is not None
        assert result[1] == "zip"

    def test_whitespace(self):
        result = _parse_pipe_value("  a | b | c  ")
        assert result is not None
        parts, mode = result
        assert mode == "product"
        assert parts == ["a", "b", "c"]


# ═══════════════════════════════════════════════════════════════
#  generate_batch_configs
# ═══════════════════════════════════════════════════════════════

class TestGenerateBatchConfigs:
    def test_no_pipes_returns_original(self, sample_config):
        """No pipe syntax → returns [original_config]."""
        configs = generate_batch_configs(sample_config)
        assert len(configs) == 1
        assert configs[0] is sample_config

    def test_product_only(self, sample_config_with_pipes):
        """Product: 3 × 2 = 6 configs."""
        configs = generate_batch_configs(sample_config_with_pipes)
        assert len(configs) == 6
        # Each config should have typed values, not pipe strings
        for c in configs:
            assert isinstance(c["lr"], (int, float))
            assert isinstance(c["batch_size"], (int, float))
            assert c["optimizer"] == "adam"  # fixed value preserved

    def test_product_values_correct(self, sample_config_with_pipes):
        configs = generate_batch_configs(sample_config_with_pipes)
        lr_values = sorted(set(c["lr"] for c in configs))
        bs_values = sorted(set(c["batch_size"] for c in configs))
        assert lr_values == [0.001, 0.01, 0.1]
        assert bs_values == [32, 64]

    def test_mixed_product_and_zip(self, sample_config_mixed):
        """Product(3 × 2) × Zip(3) = 18."""
        configs = generate_batch_configs(sample_config_mixed)
        assert len(configs) == 18

    def test_zip_only(self):
        cfg = {
            "seed": "(1 | 2 | 3)",
            "tag": "(a | b | c)",
            "fixed": "hello",
        }
        configs = generate_batch_configs(cfg)
        assert len(configs) == 3
        seeds = [c["seed"] for c in configs]
        tags = [c["tag"] for c in configs]
        assert seeds == [1, 2, 3]
        assert tags == ["a", "b", "c"]
        assert all(c["fixed"] == "hello" for c in configs)

    def test_zip_length_mismatch_raises(self):
        cfg = {
            "a": "(1 | 2 | 3)",
            "b": "(x | y)",  # length 2 != 3
        }
        with pytest.raises(ValueError, match="equal length"):
            generate_batch_configs(cfg)

    def test_nested_dict_product(self):
        cfg = {
            "model": {"name": "resnet | vgg", "layers": 50},
            "lr": "0.01 | 0.1",
        }
        configs = generate_batch_configs(cfg)
        assert len(configs) == 4  # 2 × 2
        names = set(c["model"]["name"] for c in configs)
        assert names == {"resnet", "vgg"}

    def test_meta_desc_present(self, sample_config_with_pipes):
        configs = generate_batch_configs(sample_config_with_pipes)
        for c in configs:
            assert "_meta_desc" in c
            assert isinstance(c["_meta_desc"], str)
            assert len(c["_meta_desc"]) > 0

    def test_fixed_values_preserved(self, sample_config_with_pipes):
        configs = generate_batch_configs(sample_config_with_pipes)
        for c in configs:
            assert c["optimizer"] == "adam"
            assert c["model"]["name"] == "resnet"
            assert c["model"]["layers"] == 50


# ═══════════════════════════════════════════════════════════════
#  count_batch_configs
# ═══════════════════════════════════════════════════════════════

class TestCountBatchConfigs:
    def test_no_pipes(self, sample_config):
        assert count_batch_configs(sample_config) == 1

    def test_product_only(self, sample_config_with_pipes):
        assert count_batch_configs(sample_config_with_pipes) == 6  # 3 × 2

    def test_mixed(self, sample_config_mixed):
        assert count_batch_configs(sample_config_mixed) == 18  # 3 × 2 × 3

    def test_zip_only(self):
        cfg = {"a": "(1 | 2 | 3)", "b": "(x | y | z)"}
        assert count_batch_configs(cfg) == 3

    def test_zip_mismatch_returns_zero(self):
        cfg = {"a": "(1 | 2 | 3)", "b": "(x | y)"}
        assert count_batch_configs(cfg) == 0

    def test_count_matches_generate_length(self, sample_config_mixed):
        """count should exactly match len(generate)."""
        n = count_batch_configs(sample_config_mixed)
        configs = generate_batch_configs(sample_config_mixed)
        assert n == len(configs)


# ═══════════════════════════════════════════════════════════════
#  strip_batch_pipes
# ═══════════════════════════════════════════════════════════════

class TestStripBatchPipes:
    def test_keeps_first_product_value(self):
        cfg = {"lr": "0.001 | 0.01 | 0.1", "bs": "32 | 64"}
        result = strip_batch_pipes(cfg)
        assert result["lr"] == 0.001
        assert result["bs"] == 32

    def test_keeps_first_zip_value(self):
        cfg = {"seed": "(1 | 2 | 3)", "tag": "(a | b | c)"}
        result = strip_batch_pipes(cfg)
        assert result["seed"] == 1
        assert result["tag"] == "a"

    def test_no_pipes_unchanged(self, sample_config):
        result = strip_batch_pipes(sample_config)
        assert result == sample_config

    def test_nested_pipes(self):
        cfg = {"model": {"name": "resnet | vgg"}, "lr": 0.01}
        result = strip_batch_pipes(cfg)
        assert result["model"]["name"] == "resnet"
        assert result["lr"] == 0.01

