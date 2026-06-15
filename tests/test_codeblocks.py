from mooring.ai.codeblocks import extract_code_blocks


def test_extracts_multiple_python_blocks():
    text = "First:\n```python\na = 1\n```\nand\n```py\nb = 2\n```"
    assert extract_code_blocks(text) == ["a = 1", "b = 2"]


def test_strips_language_tag_and_unlabelled():
    assert extract_code_blocks("```\nx = 1\n```") == ["x = 1"]


def test_prefers_python_over_other_languages():
    text = "```sql\nSELECT 1\n```\n```python\ndf = pl.read_parquet('x')\n```"
    assert extract_code_blocks(text) == ["df = pl.read_parquet('x')"]


def test_empty_when_no_fence():
    assert extract_code_blocks("just prose, no code") == []
    assert extract_code_blocks("") == []
