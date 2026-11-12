# Steerable runtime evaluation

Offline deterministic run using the real Agent loop and a scripted LLM.

| metric | value |
| --- | --- |
| scenario_count | 18 |
| event_delivery_success_rate | 1.0 |
| event_order_violation_count | 0 |
| steering_ack_p50_ms | 0.036 |
| steering_ack_p95_ms | 0.075 |
| steering_apply_p50_ms | 0.773 |
| steering_apply_p95_ms | 201.274 |
| cancelled_before_side_effect_rate | 1.0 |
| orphan_tool_message_count | 0 |
| duplicate_tool_execution_rate | 0.0 |
| time_to_first_progress_p50_ms | 0.002 |
| time_to_first_progress_p95_ms | 11.977 |
| run_completion_rate | 1.0 |
| deadlock_count | 0 |
| sensitive_payload_leak_count | 0 |
| cache_hits | 1 |

## Hard gates

| gate | actual | expected | passed |
| --- | --- | --- | --- |
| orphan_tool_message_count | 0 | 0 | True |
| deadlock_count | 0 | 0 | True |
| sensitive_payload_leak_count | 0 | 0 | True |
| event_order_violation_count | 0 | 0 | True |
