# Evaluation test fixtures

Every file here is deterministic illustrative test data. Fixture divisions,
risk records, weights, populations, expected tiers and allocation bids are not
observations, actual team outputs, published findings, or scientific
validation results.

`messages.csv` follows the current seven-column logger format and typed
ontology. Its matching received `risk_record` and sent `alert` use the same
thread only to test the evaluator's explicit correlation rule; this is not a
claim that the running alert agent currently preserves that thread. The
negotiation rows are similarly illustrative and may be counted only when a
test explicitly declares the history complete. Allocation fixtures remain
mechanics examples and are not treated as team results by default.
