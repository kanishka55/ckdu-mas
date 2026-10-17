import csv
import json
import multiprocessing as mp

import pytest

from src.core import msg_log, ontology


def _cfp(**over):
    body = {"type": "cfp", "month_index": 0, "round": 1, "resource": "RO_unit",
            "units_available": 1, "deadline_s": 5.0}
    body.update(over)
    return body


def test_round_trip():
    text = ontology.encode(ontology.CFP, _cfp())
    assert ontology.decode(ontology.CFP, text) == _cfp()


def test_every_schema_has_a_legal_performative():
    for allowed, _ in ontology.SCHEMAS.values():
        assert allowed <= ontology.PERFORMATIVES


@pytest.mark.parametrize("body, match", [
    ({k: v for k, v in _cfp().items() if k != "round"}, "missing"),
    (_cfp(extra=1), "unexpected"),
    (_cfp(round="1"), "round"),
    (_cfp(units_available=True), "bool"),
    (_cfp(resource="helicopter"), "resource"),
    (_cfp(deadline_s=0), "deadline"),
    ({"type": "nonsense"}, "unknown message type"),
])
def test_invalid_bodies_rejected(body, match):
    with pytest.raises(ontology.SchemaError, match=match):
        ontology.validate(ontology.CFP, body)


def test_type_must_match_performative():
    with pytest.raises(ontology.SchemaError, match="not allowed"):
        ontology.validate(ontology.PROPOSE, _cfp())


def test_decode_rejects_non_json():
    with pytest.raises(ontology.SchemaError):
        ontology.decode(ontology.CFP, "not json")


def test_risk_record_allows_null_wqi():
    ontology.validate(ontology.INFORM, {"type": "risk_record", "month_index": 0,
                                        "ds_division": "Padaviya", "wqi": None,
                                        "tier": "unknown", "drivers": []})


def test_log_writes_header_and_rows(tmp_path):
    path = tmp_path / "messages.csv"
    msg_log.log_message("a@x", "b@x", "cfp", "t1", _cfp(), "sent", path=path)
    msg_log.log_message("a@x", "b@x", "cfp", "t1", json.dumps(_cfp()), "received", path=path)
    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    assert list(rows[0]) == msg_log.COLUMNS
    assert [r["direction"] for r in rows] == ["sent", "received"]
    assert json.loads(rows[0]["payload"]) == _cfp()
    with pytest.raises(ValueError):
        msg_log.log_message("a", "b", "cfp", None, "{}", "sideways", path=path)


def _writer(path, n):
    for i in range(n):
        msg_log.log_message("p@x", "q@x", "inform", f"t{i}", {"i": i, "text": "a, \"quoted\"\nline"},
                            "sent", path=path)


def test_concurrent_processes_do_not_corrupt_log(tmp_path):
    path = tmp_path / "messages.csv"
    procs = [mp.Process(target=_writer, args=(path, 50)) for _ in range(4)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=60)
    assert all(p.exitcode == 0 for p in procs)
    rows = list(csv.DictReader(path.open(encoding="utf-8", newline="")))
    assert len(rows) == 200
    assert all(json.loads(r["payload"])["text"] == "a, \"quoted\"\nline" for r in rows)
