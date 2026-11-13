"""End-to-end smoke test of the Streamlit demo using the synthetic samples."""

from streamlit.testing.v1 import AppTest


def _click(at: AppTest, label_prefix: str, index: int = 0) -> None:
    matches = [b for b in at.button if b.label.startswith(label_prefix)]
    matches[index].click()
    at.run(timeout=120)


def test_upload_confirm_review_timeline_share() -> None:
    at = AppTest.from_file("../app.py", default_timeout=120)
    at.run()
    _click(at, "Load 3 synthetic sample reports")
    assert len(at.session_state["reports"]) == 3

    for _ in range(3):
        at.checkbox[0].check()
        _click(at, "Confirm report")
    assert all(r["confirmed"] for r in at.session_state["reports"].values())

    at.sidebar.radio[0].set_value("Review")
    at.run()
    while any(b.label == "Accept" for b in at.button):
        _click(at, "Accept")
    while any(b.label.startswith("Reject these") for b in at.button):
        _click(at, "Reject these")
    assert not at.exception
    observations = at.session_state["observations"]
    assert len(observations) == 9
    assert sum(o.conversion_outcome == "UNCONVERTIBLE_RETAINED" for o in observations) == 1
    assert at.sidebar.metric[0].value == "0"

    for _ in range(3):
        at.checkbox[0].check()
        at.run()
        _click(at, "Confirm page 1 is complete")
    assert all(
        inv.inventory_status == "COMPLETE"
        for r in at.session_state["reports"].values()
        for inv in r["result"].page_inventories
    )

    at.sidebar.radio[0].set_value("Timeline")
    at.run()
    assert not at.exception
    assert any("different reported unit" in m.value for m in at.markdown)

    at.sidebar.radio[0].set_value("Share")
    at.run()
    _click(at, "Prepare share")
    assert not at.exception
    assert any("3 result(s)" in s.value for s in at.success)
    assert not any("withheld" in m.value for m in at.markdown)
    assert len(at.get("image")) >= 3


def test_encrypted_record_round_trip_through_the_ui() -> None:
    at = AppTest.from_file("../app.py", default_timeout=120)
    at.run()
    _click(at, "Load 3 synthetic sample reports")
    at.checkbox[0].check()
    _click(at, "Confirm report")
    at.sidebar.text_input(key="vault-save-pass").input("correct horse battery")
    at.run()
    _click(at, "Encrypt record")
    vault_file = at.session_state["vault_file"]
    assert vault_file and b"Jordan" not in vault_file

    fresh = AppTest.from_file("../app.py", default_timeout=120)
    fresh.run()
    assert not fresh.session_state["reports"]
    from labtrail_vault import decrypt_record
    record = decrypt_record(vault_file, "correct horse battery")
    assert len(record["reports"]) == 3
    assert sum(r["confirmed"] is not None for r in record["reports"].values()) == 1
