# Evaluation test fixtures

Every file here is deterministic, illustrative test data. `Fixture A/B/C`, the
risk weights, populations, expected tiers and allocation bids are invented
test inputs; they are not measured observations, published findings, or
scientific validation results. Risk records use `source="synthetic"`.

`messages.csv` includes an illustrative `event_type="risk_input_received"`
marker solely to exercise the injected latency selector. That marker and its
pairing with village/month are not yet part of the shared ontology. Alert rows
follow the provisional alert and suppression schemas. Allocation rows follow
Member C's documented CSV columns.
