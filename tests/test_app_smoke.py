"""End-to-end smoke test of the Streamlit demo using the synthetic samples."""

from streamlit.testing.v1 import AppTest

from labtrail_workflow import SIDEBAR_NOTICE, find_interpretive_language


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


def test_upload_confirm_review_timeline_share(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LABTRAIL_SERVER_DIR", str(tmp_path))
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
    for analyte in sorted({o.analyte_code for o in at.session_state["observations"]}):
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

    # --- Send now: needs consent, then shows link + code and an active entry
    send = next(b for b in at.button if b.label == "Send now")
    assert send.disabled
    next(c for c in at.checkbox if c.key == "send-consent").check()
    at.run()
    _click(at, "Send now")
    assert not at.exception
    sent = at.session_state["last_sent"]
    assert "?share=" in sent["link"] and len(sent["code"]) == 6
    assert any("Active" in m.value for m in at.markdown)
    _assert_no_interpretation(at, "Share/sent")

    # --- Clinician opens the link in a separate session
    query = dict(part.split("=", 1) for part in sent["link"].split("?", 1)[1].split("&"))
    viewer = AppTest.from_file("../app.py", default_timeout=120)
    viewer.query_params.update(query)
    viewer.run()
    assert not viewer.sidebar.radio  # no patient navigation in the recipient view
    viewer.text_input[0].input("clinician@example.org")
    viewer.text_input[1].input("000000" if sent["code"] != "000000" else "111111")
    viewer.button[0].click()
    viewer.run()
    assert any("not correct" in e.value for e in viewer.error)
    viewer.text_input[1].input(sent["code"])
    viewer.button[0].click()
    viewer.run()
    assert not viewer.exception
    assert any(s.value == "Glucose" for s in viewer.subheader)
    assert len(viewer.get("image")) == 3
    assert len(viewer.dataframe) == 1
    _assert_no_interpretation(viewer, "Recipient view")

    # --- Patient revokes; the open viewer loses access on its next refresh
    _click(at, "Revoke")
    assert any("Revoked" in m.value for m in at.markdown)
    viewer.run()
    assert any("has ended" in e.value for e in viewer.error)
    assert not viewer.dataframe


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


def test_patient_can_add_a_test_name_that_persists(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LABTRAIL_SERVER_DIR", str(tmp_path))
    at = AppTest.from_file("../app.py", default_timeout=120)
    at.run()
    _click(at, "Load 3 synthetic sample reports")
    at.checkbox[0].check()
    _click(at, "Confirm report")
    at.sidebar.radio[0].set_value("Review")
    at.run()

    # A failed accept (misread value) must not add the name.
    first = next(t for t in at.text_input if t.key and t.key.startswith("newname-"))
    first.input("Homocysteine")
    next(t for t in at.text_input if t.label == "Value").input("1O4")
    _click(at, "Accept")
    assert any("not a number" in e.value for e in at.error)
    assert at.session_state["custom_analytes"] == {}

    # Fix the value: the row is accepted under the new name, which is kept.
    next(t for t in at.text_input if t.label == "Value").input("11.2")
    first = next(t for t in at.text_input if t.key and t.key.startswith("newname-"))
    first.input("Homocysteine")
    _click(at, "Accept")
    assert not at.exception
    assert list(at.session_state["custom_analytes"].values()) == ["Homocysteine"]
    code = next(iter(at.session_state["custom_analytes"]))
    assert at.session_state["observations"][-1].analyte_code == code
    assert any(e.label.startswith("Tests you added (1)") for e in at.expander)
    assert "Homocysteine" in at.selectbox[0].options  # offered for every later row

    # The name is saved inside the encrypted record.
    at.sidebar.text_input(key="vault-save-pass").input("correct horse battery")
    at.run()
    _click(at, "Encrypt record")
    from labtrail_vault import decrypt_record
    record = decrypt_record(at.session_state["vault_file"], "correct horse battery")
    assert record["custom_analytes"] == {code: "Homocysteine"}
    assert b"Homocysteine" not in at.session_state["vault_file"]


def test_identification_lines_are_rejected_on_upload_and_restorable(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LABTRAIL_SERVER_DIR", str(tmp_path))
    at = AppTest.from_file("../app.py", default_timeout=120)
    at.run()
    _click(at, "Load 3 synthetic sample reports")
    for _ in range(3):
        at.checkbox[0].check()
        _click(at, "Confirm report")
    candidates = at.session_state["candidates"]
    rejected = at.session_state["auto_rejected"]
    texts = {candidates[cid].raw_analyte_name: why for cid, why in rejected.items()}
    assert texts == {
        "Patient: Jordan Sample": "name",
        "Collected: 2026-02-10": "report date",
        "Collected: 2026-05-18": "report date",
        "Collected: 2026-09-03": "report date",
    }
    assert len(rejected) == 6  # two lines on each of three reports

    at.sidebar.radio[0].set_value("Review")
    at.run()
    assert sum(e.label.startswith("2 identification lines rejected") for e in at.expander) == 3

    # Finish the first report and confirm its page, then restore a line.
    while any(b.label == "Accept" for b in at.button):
        _click(at, "Accept")
    while any(b.label.startswith("Reject these") for b in at.button):
        _click(at, "Reject these")
    at.checkbox[0].check()
    at.run()
    _click(at, "Confirm page 1 is complete")
    _click(at, "Restore")
    assert len(at.session_state["auto_rejected"]) == 5
    restored = [c for c in candidates.values() if c.lifecycle_status == "CANDIDATE"]
    assert len(restored) == 1
    report = at.session_state["reports"][restored[0].report_id]
    assert report["result"].page_inventories[0].inventory_status == "INCOMPLETE"
