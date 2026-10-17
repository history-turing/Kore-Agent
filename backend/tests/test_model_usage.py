from kore_next.agent import _model_usage, _openai_cache_usage


def test_qwen_and_deepseek_compatible_cache_fields():
    qwen = _openai_cache_usage(
        {"prompt_tokens_details": {"cached_tokens": 30, "cache_creation_input_tokens": 4}},
        "prompt_tokens_details",
    )
    usage = _model_usage(100, 20, 120, **qwen)
    assert usage["cached_input_tokens"] == 30
    assert usage["uncached_input_tokens"] == 70
    assert usage["cache_write_input_tokens"] == 4

    deepseek = _openai_cache_usage(
        {"prompt_cache_hit_tokens": 80, "prompt_cache_miss_tokens": 20},
        "prompt_tokens_details",
    )
    assert _model_usage(100, 10, 110, **deepseek)["uncached_input_tokens"] == 20


def test_anthropic_cache_tokens_are_included_in_input_total():
    base_input, cache_read, cache_write, output = 15, 7, 3, 2
    input_tokens = base_input + cache_read + cache_write
    usage = _model_usage(
        input_tokens,
        output,
        input_tokens + output,
        cached_input_tokens=cache_read,
        cache_write_input_tokens=cache_write,
        uncached_input_tokens=base_input + cache_write,
    )
    assert usage == {
        "input_tokens": 25,
        "output_tokens": 2,
        "total_tokens": 27,
        "cached_input_tokens": 7,
        "cache_read_input_tokens": 7,
        "uncached_input_tokens": 18,
        "cache_write_input_tokens": 3,
    }
