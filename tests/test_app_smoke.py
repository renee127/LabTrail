"""End-to-end smoke test of the Streamlit demo using the synthetic samples."""

from streamlit.testing.v1 import AppTest

from labtrail_workflow import ANALYTE_LABELS, SIDEBAR_NOTICE, find_interpretive_language


def _click(at: AppTest, label_prefix: str, index: int = 0) -> None:
    matches = [b for b in at.button if b.label.startswith(label_prefix)]
    matches[index].click()
    at.run(timeout=120)


def _visible_text(at: AppTest) -> str:
    """Every string the app rendered: markdown, captions, alerts, labels, metrics."""
    parts: list[str] = []

    def walk(node) -> None:
        for attr in ("value", "label", "body", "help", "placeholder"):
            value = getattr(node, attr, None)
            if isinstance(value, str):
                parts.append(value)
        for child in getattr(node, "children", {}).values():
            walk(child)

    walk(at._tree)
    return "\n".join(parts)


def _assert_no_interpretation(at: AppTest, page: str) -> None:
    text = _visible_text(at)
    assert SIDEBAR_NOTICE in text  # proves the walker reaches sidebar alerts
    found = find_interpretive_language(text)
    assert not found, f"Interpretive language on {page}: {found}"


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

    _assert_no_interpretation(at, "Review")

    at.sidebar.radio[0].set_value("Timeline")
    at.run()
    assert not at.exception
    assert any("different reported unit" in m.value for m in at.markdown)
    _assert_no_interpretation(at, "Timeline")
    assert "different reported unit" in _visible_text(at)
    for analyte in ANALYTE_LABELS:
        at.radio[0].set_value(analyte)
        at.run()
        _assert_no_interpretation(at, f"Timeline/{analyte}")

    images_before = len(at.get("image"))
    _click(at, "Source")
    assert not at.exception
    assert len(at.get("image")) > images_before
    assert any(e.label == "Whole page" for e in at.expander)
    _click(at, "Source")
    assert not any(e.label == "Whole page" for e in at.expander)

    at.sidebar.radio[0].set_value("Share")
    at.run()
    _click(at, "Prepare share")
    assert not at.exception
    assert any("3 result(s)" in s.value for s in at.success)
    assert not any("withheld" in m.value for m in at.markdown)
    assert len(at.get("image")) >= 3
    _assert_no_interpretation(at, "Share")
    assert not find_interpretive_language(at.get("code")[0].value)


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
