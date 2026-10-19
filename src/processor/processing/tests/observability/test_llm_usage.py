from processing.models.classification import LLMUsage
from processing.observability.llm_usage import LLMUsageCollector


def test_llm_usage_collector_accumulates_calls_within_one_scope():
    collector = LLMUsageCollector()

    with collector.capture():
        collector.record_call(input_tokens=11, output_tokens=7)
        collector.record_call(input_tokens=13, output_tokens=5)
        usage = collector.current_usage()

    assert usage == LLMUsage(input_tokens=24, output_tokens=12)
