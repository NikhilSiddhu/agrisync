"""The watchdog alerts on change only: a new status, or a new kind of problem."""
from scripts.check_devices import changes_since, problem_key

PLATE = "rain plate has read wet for 48 hours without a break: corroded or shorted"


def counter(n):
    return f"rain counter reads {n:,}: the input chatters and the count means nothing"


def dev(status, *problems):
    return [{"device_id": "box", "status": status, "problems": list(problems)}]


def test_numbers_do_not_make_a_new_problem():
    assert problem_key(counter(64663617)) == problem_key(counter(64700002))


def test_known_faults_alert_once():
    state, ch = changes_since({}, dev("degraded", PLATE, counter(1)))
    assert len(ch) == 1
    state, ch = changes_since(state, dev("degraded", PLATE, counter(99999)))
    assert ch == []


def test_a_new_fault_at_the_same_status_still_alerts():
    state, _ = changes_since({}, dev("degraded", PLATE))
    state, ch = changes_since(state, dev("degraded", PLATE, "bme280 not responding"))
    assert ch == [("box", "degraded", "degraded", ["bme280 not responding"])]


def test_a_cleared_fault_is_quiet_and_recovery_alerts():
    state, _ = changes_since({}, dev("degraded", PLATE, counter(5)))
    state, ch = changes_since(state, dev("degraded", PLATE))
    assert ch == []
    state, ch = changes_since(state, dev("ok"))
    assert ch == [("box", "degraded", "ok", [])]


def test_reads_the_old_state_file():
    _, ch = changes_since({"box": "degraded"}, dev("degraded", PLATE))
    assert ch == []
    _, ch = changes_since({"box": "ok"}, dev("degraded", PLATE))
    assert len(ch) == 1
