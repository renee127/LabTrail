"""LabTrail demo UI: upload -> confirm -> review -> timeline -> share.

Run with:  streamlit run app.py
Synthetic data only. Everything lives in this browser session's memory; nothing
is encrypted or stored on a server in this demo.
"""

import os
import sys
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from urllib.parse import quote
from uuid import UUID, uuid4

import altair as alt
import pandas as pd
import streamlit as st
from PIL import Image

from labtrail_algorithms import find_duplicate_observation_groups, partition_review_queue
from labtrail_extraction import extract_document_ocr_line_candidates
from labtrail_redaction import rasterize_pdf_page, redact_source_page
from labtrail_sharing import AccessDenied, ShareDecryptionError, ShareServer, StorageUnavailable, b64url, open_share, send_share, unb64url
from labtrail_vault import MIN_PASSPHRASE_LENGTH, VaultError, decrypt_record, encrypt_record
from labtrail_workflow import (
    ANALYTE_LABELS,
    COMPARABILITY_LABELS,
    RULE_VERSION,
    SIDEBAR_NOTICE,
    TIMELINE_NOTICE,
    accept_candidate,
    auto_reject_identification_lines,
    line_text,
    reopen_page_coverage,
    add_custom_analyte,
    all_analyte_labels,
    analyte_timeline,
    build_share,
    confirm_page_coverage,
    is_result_like,
    plan_chart,
    suggest_analyte_code,
    suggest_report_metadata,
)
from models import CandidateExtraction, ContextNote


SAMPLES_DIR = Path(__file__).with_name("samples")
RASTER_DPI = 150
MIME_BY_SUFFIX = {".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}
NOTE_CATEGORIES = ["MEDICATION_CHANGE", "DIET", "ILLNESS", "ACTIVITY", "FREE_TEXT"]

st.set_page_config(page_title="LabTrail demo", page_icon="🧪", layout="wide")


# ---------------------------------------------------------------- state

def _state():
    ss = st.session_state
    ss.setdefault("reports", {})        # report_id -> dict
    ss.setdefault("candidates", {})     # candidate_id -> CandidateExtraction
    ss.setdefault("observations", [])   # AcceptedObservation
    ss.setdefault("notes", [])          # ContextNote
    ss.setdefault("activity", [])       # (timestamp, message)
    ss.setdefault("confirmed_identities", set())  # (lab, exact printed name)
    ss.setdefault("sent_shares", [])    # dicts: grant id, recipient, link, code
    ss.setdefault("custom_analytes", {})  # code -> test name the patient added
    ss.setdefault("auto_rejected", {})    # candidate id -> identification category
    return ss


def _labels() -> dict[str, str]:
    """Reference tests plus tests this patient added."""
    return all_analyte_labels(_state().custom_analytes)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _log(message: str) -> None:
    _state().activity.append((_now().strftime("%H:%M:%S"), message))


# ---------------------------------------------------------------- upload

def ingest(filename: str, data: bytes, mime_type: str) -> None:
    ss = _state()
    digest = sha256(data).hexdigest()
    if any(r["sha256"] == digest for r in ss.reports.values()):
        st.warning(f"{filename} is identical to a report already uploaded, so it was skipped.")
        return
    report_id = uuid4()
    with st.spinner(f"Reading {filename}…"):
        result = extract_document_ocr_line_candidates(
            source_bytes=data,
            mime_type=mime_type,
            report_id=report_id,
            extraction_job_id=uuid4(),
            extracted_at=_now(),
            dpi=RASTER_DPI,
        )
        if mime_type == "application/pdf":
            pages = {
                inv.page_number: rasterize_pdf_page(data, inv.page_number, dpi=RASTER_DPI)
                for inv in result.page_inventories
            }
        else:
            pages = {1: data}
    ss.reports[report_id] = {
        "filename": filename,
        "sha256": digest,
        "result": result,
        "pages": pages,
        "suggestion": suggest_report_metadata(result),
        "confirmed": None,
    }
    for candidate in result.candidates:
        ss.candidates[candidate.candidate_id] = candidate
    _log(f"Uploaded {filename}: {len(result.candidates)} text lines found")
    rejected = auto_reject_identification_lines(result, ss.custom_analytes)
    ss.auto_rejected.update(rejected)
    if rejected:
        _log(f"Rejected {len(rejected)} identification lines on {filename} automatically")


def page_upload() -> None:
    ss = _state()
    st.header("1 · Upload and confirm reports")
    st.caption(
        "Upload a PDF, PNG, or JPEG lab report. LabTrail reads the text and proposes "
        "result rows, but nothing enters your record until you confirm the report and review each row."
    )
    col_a, col_b = st.columns([3, 2])
    with col_a:
        files = st.file_uploader(
            "Lab reports", type=["pdf", "png", "jpg", "jpeg"], accept_multiple_files=True
        )
        if files and st.button("Process uploaded files"):
            for f in files:
                try:
                    ingest(f.name, f.getvalue(), MIME_BY_SUFFIX[Path(f.name).suffix.lower()])
                except ValueError as error:
                    st.error(f"{f.name}: {error}")
    with col_b:
        samples = sorted(SAMPLES_DIR.glob("*.pdf"))
        st.write("**Demo data**")
        if st.button(f"Load {len(samples)} synthetic sample reports", disabled=not samples):
            for path in samples:
                ingest(path.name, path.read_bytes(), "application/pdf")
        if not samples:
            st.caption("Run `python3 scripts/make_synthetic_reports.py` first.")

    pending = [(rid, r) for rid, r in ss.reports.items() if r["confirmed"] is None]
    if not pending:
        if ss.reports:
            st.success("All uploaded reports are confirmed. Continue to **Review**.")
        return

    st.subheader("Confirm report details")
    st.caption(
        "Check what LabTrail read from each report. You must confirm the report is yours "
        "before its results can be reviewed."
    )
    for report_id, report in pending:
        s = report["suggestion"]
        with st.container(border=True):
            left, right = st.columns([2, 3])
            with left:
                st.image(report["pages"][1], caption=f"{report['filename']} · page 1")
            with right:
                with st.form(f"confirm-{report_id}"):
                    lab = st.text_input("Laboratory", value=s.source_lab or "")
                    collected = st.date_input(
                        "Collection date", value=s.collected or date.today(), format="YYYY-MM-DD"
                    )
                    printed_name = s.patient_name or "(no name found)"
                    known = (lab, printed_name) in ss.confirmed_identities
                    st.write(f"Name printed on report: **{printed_name}**")
                    mine = st.checkbox(
                        "This name refers to me" + (" (confirmed before for this lab)" if known else ""),
                        value=known,
                    )
                    if st.form_submit_button("Confirm report", type="primary"):
                        if not lab.strip():
                            st.error("Enter the laboratory name.")
                        elif not mine:
                            st.error("Confirm the name refers to you, or remove this report.")
                        else:
                            report["confirmed"] = {"lab": lab.strip(), "collected": collected}
                            ss.confirmed_identities.add((lab.strip(), printed_name))
                            _log(f"Confirmed {report['filename']} ({lab.strip()}, {collected})")
                            st.rerun()


# ---------------------------------------------------------------- review

def _crop(
    report: dict, page_number: int, bounding_box: tuple[float, float, float, float] | None
) -> Image.Image | None:
    if bounding_box is None:
        return None
    page = Image.open(BytesIO(report["pages"][page_number]))
    w, h = page.size
    x0, y0, x1, y1 = bounding_box
    pad = 8
    return page.crop((max(0, int(x0 * w) - pad), max(0, int(y0 * h) - pad),
                      min(w, int(x1 * w) + pad), min(h, int(y1 * h) + pad)))


def _decide(candidate: CandidateExtraction, status: str) -> None:
    candidate.lifecycle_status = status


def _coverage_confirmation(report_id: UUID, report: dict) -> None:
    """After every line is decided, let the patient attest each page's line list."""
    result = report["result"]
    unconfirmed = [
        inv.page_number for inv in result.page_inventories if inv.inventory_status != "COMPLETE"
    ]
    if not unconfirmed:
        st.success("Reviewed, and every page confirmed complete. Pages can be shared as redacted images.")
        return
    st.success("Everything on this report has been reviewed.")
    for page_number in unconfirmed:
        with st.expander(f"Optional: confirm page {page_number} is complete so it can be shared as a redacted image"):
            left, right = st.columns([3, 2])
            left.image(report["pages"][page_number], caption=f"Page {page_number}")
            with right:
                st.write("Lines LabTrail found on this page:")
                for c in result.candidates:
                    if c.page_number == page_number:
                        mark = "✅" if c.lifecycle_status == "ACCEPTED" else "✖️"
                        st.caption(f"{mark} {c.raw_analyte_name} {c.raw_value or ''} {c.raw_unit or ''}")
                st.caption(
                    "Compare with the page image. When you share, every line except the results "
                    "you choose is covered with a black box. Text missing from this list would not "
                    "be covered."
                )
                ok = st.checkbox(
                    "Every line of text on this page appears in the list",
                    key=f"cov-ok-{report_id}-{page_number}",
                )
                if st.button(
                    f"Confirm page {page_number} is complete",
                    key=f"cov-{report_id}-{page_number}",
                    disabled=not ok,
                ):
                    report["result"] = confirm_page_coverage(result, page_number)
                    _log(f"Confirmed page {page_number} of {report['filename']} is complete")
                    st.rerun()


def _auto_rejected_panel(report_id: UUID, report: dict) -> None:
    """Show identification lines rejected on upload, each restorable for review."""
    ss = _state()
    lines = [c for c in report["result"].candidates if c.candidate_id in ss.auto_rejected]
    if not lines:
        return
    with st.expander(
        f"{len(lines)} identification lines rejected automatically (names, dates, IDs, addresses)"
    ):
        st.caption(
            "These look like report details, not lab results, so they were rejected for you. "
            "Restore any line that is actually a result."
        )
        for c in lines:
            text_col, why_col, button_col = st.columns([5, 2, 1])
            text_col.text(line_text(c))
            why_col.caption(ss.auto_rejected[c.candidate_id])
            if button_col.button("Restore", key=f"restore-{c.candidate_id}"):
                c.lifecycle_status = "CANDIDATE"
                del ss.auto_rejected[c.candidate_id]
                report["result"] = reopen_page_coverage(report["result"], c.page_number)
                _log(f"Restored “{line_text(c)}” for review")
                st.rerun()


def page_review() -> None:
    ss = _state()
    st.header("2 · Review extracted results")
    st.caption(
        "Every row needs your decision. Compare each proposed value with the cropped image "
        "from the original report, correct anything that is wrong, then accept or reject it."
    )
    confirmed = [(rid, r) for rid, r in ss.reports.items() if r["confirmed"]]
    if not confirmed:
        st.info("Confirm at least one report on the **Upload** page first.")
        return

    labels = _labels()
    analyte_options = ["(choose)"] + sorted(labels, key=lambda c: labels[c].casefold())
    if ss.custom_analytes:
        with st.expander(f"Tests you added ({len(ss.custom_analytes)})"):
            st.caption("These stay in your list for future uploads and are saved with your encrypted record.")
            for name in sorted(ss.custom_analytes.values(), key=str.casefold):
                st.write(f"- {name}")
    for report_id, report in confirmed:
        mandatory, standard = partition_review_queue(
            ss.candidates[c.candidate_id] for c in report["result"].candidates
        )
        pending = mandatory + standard
        results = [c for c in pending if is_result_like(c)]
        other = [c for c in pending if not is_result_like(c)]
        meta = report["confirmed"]
        st.subheader(f"{meta['lab']} · {meta['collected']}")
        _auto_rejected_panel(report_id, report)
        if not pending:
            _coverage_confirmation(report_id, report)
            continue

        if other:
            with st.expander(f"{len(other)} lines with no value and unit (headers, names, dates)"):
                for c in other:
                    st.text(c.raw_analyte_name)
                if st.button(f"Reject these {len(other)} lines", key=f"rej-other-{report_id}"):
                    for c in other:
                        _decide(c, "REJECTED")
                    _log(f"Rejected {len(other)} non-result lines on {report['filename']}")
                    st.rerun()

        for c in results:
            with st.container(border=True):
                crop_col, form_col = st.columns([2, 3])
                with crop_col:
                    crop = _crop(report, c.page_number, c.bounding_box)
                    if crop is not None:
                        st.image(crop, caption=f"Source: page {c.page_number}")
                    st.caption(f"OCR confidence {c.field_confidences['value']:.0%}")
                with form_col, st.form(f"review-{c.candidate_id}"):
                    suggested = suggest_analyte_code(c.raw_analyte_name, ss.custom_analytes)
                    a, v, u, r = st.columns([3, 2, 2, 2])
                    code = a.selectbox(
                        f"Analyte (read as “{c.raw_analyte_name}”)",
                        analyte_options,
                        index=analyte_options.index(suggested) if suggested else 0,
                        format_func=lambda k: labels.get(k, k),
                    )
                    value = v.text_input("Value", value=c.raw_value or "")
                    unit = u.text_input("Unit", value=c.raw_unit or "")
                    ref = r.text_input("Reference range", value=c.reference_interval or "")
                    new_name = st.text_input(
                        "Not in the list? Enter the test name",
                        key=f"newname-{c.candidate_id}",
                        placeholder="" if suggested else c.raw_analyte_name,
                        help="If filled in, this name is used instead of the list above, and it "
                        "stays in your list for future uploads.",
                    )
                    accept_col, reject_col = st.columns(2)
                    accept = accept_col.form_submit_button("Accept", type="primary", width="stretch")
                    reject = reject_col.form_submit_button("Reject", width="stretch")
                if accept:
                    # Work on a copy so a failed accept never leaves a stray new test name.
                    custom = dict(ss.custom_analytes)
                    created = False
                    try:
                        if new_name.strip():
                            code, created = add_custom_analyte(new_name, custom)
                        observation = accept_candidate(
                            c, analyte_code=code, value=value, unit=unit,
                            reference_interval=ref, source_lab=meta["lab"],
                            collected=meta["collected"], accepted_at=_now(),
                            custom_analytes=custom,
                        )
                    except ValueError as error:
                        st.error(str(error))
                    else:
                        ss.custom_analytes = custom
                        if created:
                            _log(f"Added “{custom[code]}” to your test list")
                        ss.observations.append(observation)
                        _decide(c, "ACCEPTED")
                        how = "corrected" if observation.provenance_flag == "CORRECTED_BY_PATIENT" else "accepted"
                        _log(f"{how.capitalize()} {_labels()[code]} {value} {unit} from {meta['lab']}")
                        st.rerun()
                if reject:
                    _decide(c, "REJECTED")
                    _log(f"Rejected “{c.raw_analyte_name}” on {report['filename']}")
                    st.rerun()


# ---------------------------------------------------------------- timeline

def _show_source(observation) -> None:
    """FR-10: show the exact source region of an accepted value, or say it is unavailable."""
    report = _state().reports.get(observation.report_id)
    if report is None:
        st.warning("The source report for this value is not in this record.")
        return
    where = f"{report['filename']}, page {observation.page_number}"
    with st.container(border=True):
        crop = _crop(report, observation.page_number, observation.bounding_box)
        if crop is not None:
            st.image(crop, caption=f"Source: {where}")
            with st.expander("Whole page"):
                st.image(report["pages"][observation.page_number])
        else:
            st.info(f"Region unavailable for this value. Showing the whole page: {where}.")
            st.image(report["pages"][observation.page_number])


def _chart_row(observation) -> dict:
    return {
        "canonical_value": observation.canonical_value,
        "canonical_unit": observation.canonical_unit,
        "original_value": observation.original_value,
        "original_unit": observation.original_unit,
        "unconvertible_reason": observation.unconvertible_reason,
    }


def _axis_title(code: str, unit: str | None, as_reported: bool, labels: dict[str, str]) -> str:
    label = labels.get(code, code)
    if not unit:
        return label
    return f"{label} ({unit}{', as reported' if as_reported else ''})"


def page_timeline() -> None:
    ss = _state()
    st.header("3 · Timeline")
    st.caption(
        "Accepted values from every lab, converted to one unit where a versioned rule exists "
        f"(rules {RULE_VERSION})."
    )
    st.caption(TIMELINE_NOTICE)
    active = [o for o in ss.observations if o.lifecycle_status == "ACCEPTED"]
    if not active:
        st.info("Accept some results on the **Review** page first.")
        return

    labels = _labels()
    available = sorted({o.analyte_code for o in active}, key=lambda c: labels.get(c, c).casefold())
    if len(available) <= 6:
        code = st.radio("Analyte", available, format_func=lambda c: labels.get(c, c), horizontal=True)
    else:
        code = st.selectbox("Analyte", available, format_func=lambda c: labels.get(c, c))
    points = analyte_timeline(ss.observations, code)
    plan = plan_chart([_chart_row(p.observation) for p in points])
    plotted = [points[i] for i in plan.plotted]
    retained = [points[i] for i in plan.listed]

    if plotted:
        unit = plan.unit
        df = pd.DataFrame([
            {
                "date": pd.Timestamp(p.observation.collection_datetime),
                "value": float(p.observation.original_value if plan.as_reported else p.observation.canonical_value),
                "lab": p.observation.source_lab,
                "as reported": f"{p.observation.original_value} {p.observation.original_unit}",
                "reference range": p.observation.reference_interval or "—",
                "comparability": ", ".join(COMPARABILITY_LABELS[c] for c in p.changes_from_previous) or "—",
            }
            for p in plotted
        ])
        tooltip = ["date:T", "value:Q", "lab:N", "as reported:N", "reference range:N", "comparability:N"]
        line = alt.Chart(df).mark_line(color="#888").encode(
            x=alt.X("date:T", title="Collection date"),
            y=alt.Y("value:Q", title=_axis_title(code, unit, plan.as_reported, labels), scale=alt.Scale(zero=False)),
        )
        dots = alt.Chart(df).mark_point(size=120, filled=True).encode(
            x="date:T", y="value:Q", shape=alt.Shape("lab:N", title="Lab"), tooltip=tooltip
        )
        layers = [line, dots]
        notes = [n for n in ss.notes if code in n.analyte_codes]
        if notes:
            nd = pd.DataFrame([{"date": pd.Timestamp(n.note_date), "note": n.body, "category": n.category} for n in notes])
            layers.append(alt.Chart(nd).mark_rule(strokeDash=[4, 4], color="#c77d00").encode(
                x="date:T", tooltip=["date:T", "category:N", "note:N"]))
        st.altair_chart(alt.layer(*layers).properties(height=320), width="stretch")
        st.caption(
            "Dashed lines are your context notes. Hover a point for its source details."
            + (" Plotted in the unit printed on every report; no conversion rule exists for this test." if plan.as_reported else "")
        )

    st.write("**All results for this analyte**")
    widths = [2, 3, 2, 2, 2, 3, 1, 1]
    for col, label in zip(st.columns(widths), (
        "Collected", "Lab", "As reported", "Converted", "Reference range", "Compared with previous", "", "",
    )):
        col.caption(label)
    for p in points:
        o = p.observation
        cols = st.columns(widths)
        cols[0].write(str(o.collection_datetime))
        cols[1].write(o.source_lab)
        cols[2].write(f"{o.original_value} {o.original_unit}")
        cols[3].write(f"{o.canonical_value.normalize():f} {o.canonical_unit}" if o.canonical_value is not None else "not converted")
        cols[4].write(o.reference_interval or "—")
        cols[5].write(
            ("⚠ " + ", ".join(COMPARABILITY_LABELS[c] for c in p.changes_from_previous))
            if p.changes_from_previous else "—"
        )
        if cols[6].button("Retract", key=f"retract-{o.observation_id}", help="Remove from timeline and future shares"):
            o.lifecycle_status = "RETRACTED_BY_PATIENT"
            _log(f"Retracted {labels.get(o.analyte_code, o.analyte_code)} {o.original_value} {o.original_unit} ({o.collection_datetime})")
            st.rerun()
        if cols[7].button("Source", key=f"source-{o.observation_id}", help="Show where this value came from"):
            ss.source_view = None if ss.get("source_view") == o.observation_id else o.observation_id
            st.rerun()
        if o.provenance_flag == "CORRECTED_BY_PATIENT":
            cols[1].caption("value corrected by you")
        if ss.get("source_view") == o.observation_id:
            _show_source(o)

    if retained:
        st.warning(
            "Not plotted: " + "; ".join(
                f"{p.observation.original_value} {p.observation.original_unit} on {p.observation.collection_datetime} "
                f"({p.observation.source_lab})" for p in retained
            ) + ". Kept exactly as reported. Text results are never plotted, and values are drawn "
            "together only when they share a unit or a versioned conversion rule exists."
        )

    duplicates = find_duplicate_observation_groups(active)
    if duplicates:
        st.warning(f"{len(duplicates)} possible duplicate result(s): same analyte, date, and lab. Review and retract one if needed.")

    with st.expander("Add a context note"):
        with st.form("note"):
            note_date = st.date_input("Date", value=date.today(), format="YYYY-MM-DD")
            category = st.selectbox("Category", NOTE_CATEGORIES, format_func=lambda c: c.replace("_", " ").title())
            analytes = st.multiselect("Related analytes", available, default=[code], format_func=lambda c: labels.get(c, c))
            body = st.text_input("Note", placeholder="e.g. Started metformin 500 mg")
            if st.form_submit_button("Add note") and body.strip() and analytes:
                now = _now()
                ss.notes.append(ContextNote(uuid4(), category, note_date, body.strip(), analytes, now, now))
                _log(f"Added note on {note_date}: {body.strip()}")
                st.rerun()


# ---------------------------------------------------------------- share

_WITHHELD_REASONS = {
    "ROW_COVERAGE_UNVERIFIED": "withheld: page not yet confirmed complete on the Review page",
    "NO_GRANTED_OBSERVATION": "withheld: nothing on this page is in scope",
    "OCR_LEAK_DETECTED": "withheld: hidden text was still readable after masking",
    "OCR_VERIFICATION_FAILED": "withheld: the masked page could not be verified",
    "REGION_UNRELIABLE": "withheld: a row's location on the page is uncertain",
    "REGION_UNAVAILABLE": "withheld: a row's location on the page is unknown",
    "REDACTION_LABEL_DOES_NOT_FIT": "withheld: a masked row is too small to label",
}
SEND_DISCLOSURE = (
    "**Before you send:** only the analytes and dates you selected are included, and source "
    "pages are flattened images with every other result masked. You can revoke this share at "
    "any time; LabTrail then destroys its half of the decryption key and the link stops "
    "working. Revoking cannot undo what the recipient already opened: they may have viewed, "
    "printed, photographed, or saved a copy."
)
_STATUS_LABELS = {
    "ACTIVE": "🟢 Active",
    "REVOKED": "⛔ Revoked",
    "EXPIRED": "⌛ Expired",
    "LOCKED": "🔒 Locked after 5 wrong codes",
}


@st.cache_resource(show_spinner=False)
def _cached_server(database_url: str | None, server_dir: str | None) -> ShareServer:
    """One grant service (and database connection pool) for the app's lifetime."""
    return ShareServer(root=Path(server_dir) if server_dir else None, database_url=database_url)


def _server() -> ShareServer:
    """The grant service, or a clear message and a stop if its database is unreachable.

    Only the Share page and the recipient view need it, so the rest of the app keeps
    working. A failed connection is not cached; the next interaction retries.
    """
    # On Streamlit Community Cloud, top-level secrets are also environment variables.
    try:
        return _cached_server(
            os.environ.get("LABTRAIL_DATABASE_URL"), os.environ.get("LABTRAIL_SERVER_DIR")
        )
    except StorageUnavailable as problem:
        print(f"LabTrail share database unavailable: {problem.__cause__!r}", file=sys.stderr)
        st.error(f"**Sharing is unavailable: the share database could not be reached.** {problem.hint}")
        st.stop()


def _base_url() -> str:
    url = getattr(st.context, "url", None) or "http://localhost:8501"
    return url.split("?", 1)[0].rstrip("/")


def _prepare(analytes, range_start, range_end, recipient, recipient_email, valid_days) -> None:
    ss = _state()
    now = _now()
    grant, selection, payload = build_share(
        observations=ss.observations, notes=ss.notes, analytes=analytes,
        range_start=range_start, range_end=range_end, recipient_name=recipient,
        recipient_email=recipient_email, now=now, valid_for=timedelta(days=int(valid_days)),
    )
    pages, withheld = [], []
    for plan in selection.source_pages:
        report = ss.reports[plan.report_id]
        outcome = redact_source_page(
            page_raster=report["pages"][plan.page_number], report_id=plan.report_id,
            page_number=plan.page_number, extraction_result=report["result"],
            observations=ss.observations, grant=grant, as_of=now,
            region_confidence_threshold=0.8, mask_padding_pixels=4,
        )
        label = f"{report['filename']} p.{plan.page_number}"
        if outcome.redacted_png:
            pages.append({"label": label, "png": outcome.redacted_png})
        else:
            reason = _WITHHELD_REASONS.get(outcome.withheld_reason, f"withheld: {outcome.withheld_reason}")
            withheld.append({"label": label, "reason": reason})
    ss.prepared_share = {
        "grant": grant, "email": recipient_email, "payload": payload,
        "pages": pages, "withheld": withheld,
        "results": len(selection.observations), "notes": len(selection.context_notes),
    }
    ss.last_sent = None
    _log(f"Prepared share for {recipient}: {len(selection.observations)} results")


def _send_prepared() -> None:
    ss = _state()
    prepared = ss.prepared_share
    grant = prepared["grant"]
    sent = send_share(
        grant=grant, recipient_email=prepared["email"], share_payload=prepared["payload"],
        redacted_pages=prepared["pages"], withheld_pages=prepared["withheld"],
        server=_server(), now=_now(),
        analyte_names={a: _labels().get(a, a) for a in grant.granted_analytes},
    )
    link = f"{_base_url()}/?share={sent.grant_id}&k={b64url(sent.recipient_share)}"
    record = {
        "grant_id": str(sent.grant_id),
        "recipient": grant.recipient_name,
        "email": prepared["email"],
        "analytes": [_labels().get(a, a) for a in grant.granted_analytes],
        "range": f"{grant.range_start} to {grant.range_end}",
        "expires": f"{grant.expires_at:%Y-%m-%d}",
        "sent_at": f"{_now():%Y-%m-%d %H:%M}",
        "link": link,
        "code": sent.access_code,
    }
    ss.sent_shares.append(record)
    ss.last_sent = record
    ss.prepared_share = None
    _log(f"Sent encrypted share to {grant.recipient_name}")


def _show_sent(record: dict) -> None:
    st.success(f"Sent to {record['recipient']}. Give them both of these, through different channels:")
    st.write("**1. The link**")
    st.code(record["link"], language=None)
    st.write("**2. The access code** (tell them by phone or text message, not in the same email)")
    st.code(record["code"], language=None)
    subject = quote("Lab results shared with you through LabTrail")
    body = quote(
        f"Hello {record['recipient']},\n\nI have shared some of my lab results with you. "
        f"Open this link:\n\n{record['link']}\n\nI will send the access code separately. "
        f"The link expires on {record['expires']}.\n"
    )
    st.link_button("✉️ Email the link (opens your email app)", f"mailto:{record['email']}?subject={subject}&body={body}")


def _sent_shares_panel() -> None:
    ss = _state()
    if not ss.sent_shares:
        return
    st.subheader("Sent shares")
    server, now = _server(), _now()
    expired = server.sweep_expired(now)
    if expired:
        _log(f"{expired} expired share(s) closed; their keys were destroyed")
    st.caption(f"Shares are stored in a {server.store.description}.")
    for record in reversed(ss.sent_shares):
        grant_id = UUID(record["grant_id"])
        try:
            status = server.status(grant_id, now)
            events = server.events(grant_id)
        except AccessDenied:
            status, events = "NOT_FOUND", []
        with st.container(border=True):
            top, button = st.columns([5, 1])
            top.write(
                f"**{record['recipient']}** · {', '.join(record['analytes'])} · {record['range']} · "
                f"sent {record['sent_at']} · expires {record['expires']} · "
                f"{_STATUS_LABELS.get(status, status)}"
            )
            if status == "ACTIVE" and button.button("Revoke", key=f"revoke-{grant_id}"):
                server.revoke(grant_id, _now())
                _log(f"Revoked share for {record['recipient']}")
                st.rerun()
            opened = sum(e["event_type"] == "ACCESS_GRANTED" for e in events)
            denied = sum(e["event_type"] == "ACCESS_DENIED" for e in events)
            with st.expander(f"Access log · opened {opened}× · denied {denied}×"):
                for e in events:
                    st.caption(f"{e['occurred_at'][:19].replace('T', ' ')} · {e['event_type'].replace('_', ' ').lower()}")
                if events:
                    intact = ShareServer.verify_event_chain(events)
                    st.caption("Log integrity: " + ("hash chain verified ✓" if intact else "⚠ chain broken"))
                if status == "ACTIVE":
                    st.caption("Link and code:")
                    st.code(f"{record['link']}\ncode: {record['code']}", language=None)


def page_share() -> None:
    ss = _state()
    st.header("4 · Share with a clinician")
    st.caption(
        "Choose exactly which analytes and dates a clinician may see. Only accepted results "
        "and in-scope notes are included."
    )
    active = [o for o in ss.observations if o.lifecycle_status == "ACCEPTED"]
    if not active:
        st.info("Accept some results first.")
        _sent_shares_panel()
        return
    dates = [o.collection_datetime for o in active]
    labels = _labels()
    available = sorted({o.analyte_code for o in active}, key=lambda c: labels.get(c, c).casefold())

    with st.form("share"):
        analytes = st.multiselect("Analytes", available, default=available[:1], format_func=lambda c: labels.get(c, c))
        start, end = st.columns(2)
        range_start = start.date_input("From", value=min(dates), format="YYYY-MM-DD")
        range_end = end.date_input("To", value=max(dates), format="YYYY-MM-DD")
        name, email, days = st.columns([2, 2, 1])
        recipient = name.text_input("Recipient name", value="Dr. Synthetic Clinician")
        recipient_email = email.text_input("Recipient email", value="clinician@example.org")
        valid_days = days.number_input("Valid for (days)", min_value=1, max_value=365, value=14)
        submitted = st.form_submit_button("Prepare share", type="primary")
    if submitted:
        try:
            _prepare(analytes, range_start, range_end, recipient, recipient_email, valid_days)
        except ValueError as error:
            st.error(str(error))

    prepared = ss.get("prepared_share")
    if prepared:
        grant = prepared["grant"]
        st.success(
            f"{prepared['results']} result(s) and {prepared['notes']} note(s) ready for "
            f"{grant.recipient_name}, expiring {grant.expires_at:%Y-%m-%d}."
        )
        st.write("**Source pages**")
        st.caption(
            "Source pages are shared only as flattened images with every ungranted row masked. "
            "A page is withheld unless you confirmed it complete and the masked image passes an "
            "OCR check that no hidden text is still readable."
        )
        for page in prepared["pages"]:
            st.write(f"- {page['label']}: redacted image")
            st.image(page["png"], width=600)
        for page in prepared["withheld"]:
            st.write(f"- {page['label']}: {page['reason']}")
        with st.expander("Preview share JSON"):
            st.code(prepared["payload"].decode("utf-8")[:4000], language="json")
        st.download_button(
            "Download structured share (JSON, schema v1)", prepared["payload"],
            file_name=f"labtrail-share-{grant.grant_id}.json", mime="application/json",
        )

        st.subheader("Send to the clinician")
        st.info(SEND_DISCLOSURE)
        agreed = st.checkbox("I understand what sharing and revoking can and cannot do", key="send-consent")
        if st.button("Send now", type="primary", disabled=not agreed):
            _send_prepared()
            st.rerun()

    if ss.get("last_sent"):
        _show_sent(ss.last_sent)
    _sent_shares_panel()
    st.caption(
        "Demo limits: the grant service runs inside this app, the link carries its key half "
        "in a query parameter instead of the URL fragment, and nothing is emailed automatically."
    )


# ---------------------------------------------------------------- recipient view

def page_recipient(grant_param: str, key_param: str) -> None:
    """Read-only view a clinician sees after opening a share link."""
    ss = _state()
    st.title("🧪 LabTrail · Shared lab results")
    st.caption("Read-only. Shared by the patient; only the results they chose are included.")
    st.warning(SIDEBAR_NOTICE)
    try:
        grant_id = UUID(grant_param)
        recipient_share = unb64url(key_param)
        if len(recipient_share) != 32:
            raise ValueError
    except (ValueError, TypeError):
        st.error("This link is incomplete or damaged. Ask the patient to send it again.")
        return

    server = _server()
    bundle = ss.get("recipient_bundles", {}).get(grant_id)
    if bundle is not None:
        try:
            status = server.status(grant_id, _now())
        except AccessDenied:
            status = "NOT_FOUND"
        if status != "ACTIVE":
            ss.recipient_bundles.pop(grant_id, None)
            bundle = None
            st.error("The patient's access for this link has ended (revoked or expired).")
            return

    if bundle is None:
        with st.form("open-share"):
            email = st.text_input("Your email address (the one the patient shared with)")
            code = st.text_input("Access code from the patient", max_chars=6)
            submitted = st.form_submit_button("Open", type="primary")
        if not submitted:
            return
        messages = {
            "WRONG_CREDENTIALS": "That email address or code is not correct.",
            "LOCKED": "This link is locked after too many wrong attempts. Ask the patient for a new share.",
            "REVOKED": "The patient has revoked this share.",
            "EXPIRED": "This share has expired.",
            "NOT_FOUND": "This share does not exist.",
        }
        try:
            bundle = open_share(
                grant_id=grant_id, recipient_share=recipient_share, recipient_email=email,
                access_code=code, server=server, now=_now(),
            )
        except AccessDenied as denied:
            st.error(messages.get(denied.reason, "Access denied."))
            return
        except ShareDecryptionError:
            st.error("This share could not be verified, so nothing was shown. The link may be damaged.")
            return
        ss.setdefault("recipient_bundles", {})[grant_id] = bundle
        st.rerun()

    payload = bundle["payload"]
    names = {**ANALYTE_LABELS, **bundle.get("analyte_names", {})}
    st.write(
        f"For **{bundle['recipient_name']}** · results from {payload['range_start']} to "
        f"{payload['range_end']} · access until {bundle['expires_at'][:10]}"
    )
    by_analyte: dict[str, list[dict]] = {}
    for o in payload["observations"]:
        by_analyte.setdefault(o["analyte_code"], []).append(o)
    for code, rows in by_analyte.items():
        st.subheader(names.get(code, code))
        plan = plan_chart(rows)
        plotted = [rows[i] for i in plan.plotted]
        if plotted:
            value_key = "original_value" if plan.as_reported else "canonical_value"
            df = pd.DataFrame([
                {"date": pd.Timestamp(r["collection_datetime"]), "value": float(r[value_key]),
                 "lab": r["source_lab"]} for r in plotted
            ])
            chart = alt.Chart(df).mark_line(point=alt.OverlayMarkDef(size=90, filled=True), color="#888").encode(
                x=alt.X("date:T", title="Collection date"),
                y=alt.Y("value:Q", title=_axis_title(code, plan.unit, plan.as_reported, names),
                        scale=alt.Scale(zero=False)),
                tooltip=["date:T", "value:Q", "lab:N"],
            )
            st.altair_chart(chart.properties(height=260), width="stretch")
        st.dataframe(pd.DataFrame([
            {
                "Collected": r["collection_datetime"][:10],
                "Lab": r["source_lab"],
                "As reported": f"{r['original_value']} {r['original_unit']}",
                "Converted": f"{r['canonical_value']} {r['canonical_unit']}" if r["canonical_value"] else "not converted",
                "Reference range": r["reference_interval"] or "—",
                "Compared with previous": ", ".join(COMPARABILITY_LABELS[c] for c in (r["comparability_changes"] or [])) or "—",
                "Entered by": "patient-corrected" if r["provenance_flag"] == "CORRECTED_BY_PATIENT" else "as printed",
            }
            for r in rows
        ]), hide_index=True, width="stretch")
    if payload["context_notes"]:
        st.subheader("Patient notes")
        for n in payload["context_notes"]:
            st.write(f"- {n['note_date']} · {n['category'].replace('_', ' ').title()}: {n['body']}")
    st.subheader("Source pages")
    for page in bundle["pages"]:
        st.image(page["png"], caption=page["label"], width=650)
    for page in bundle["withheld_pages"]:
        st.caption(f"{page['label']}: {page['reason']}")


# ---------------------------------------------------------------- vault

_VAULT_KEYS = (
    "reports", "observations", "notes", "activity", "confirmed_identities", "sent_shares",
    "custom_analytes", "auto_rejected",
)
_VAULT_DEFAULTS = {"sent_shares": list, "custom_analytes": dict, "auto_rejected": dict}


def vault_panel() -> None:
    """Save the record to, or restore it from, a passphrase-encrypted file."""
    ss = _state()
    with st.sidebar.expander("🔒 Save or open your record"):
        st.caption(
            "Your record lives only in this browser tab. Save it as a file encrypted with "
            "your passphrase (Argon2id + AES-256-GCM). Without the passphrase the file "
            "cannot be opened, by anyone, including LabTrail."
        )
        save_pass = st.text_input("Passphrase", type="password", key="vault-save-pass")
        if st.button("Encrypt record", disabled=not ss.reports):
            try:
                ss.vault_file = encrypt_record({k: ss[k] for k in _VAULT_KEYS}, save_pass)
            except ValueError as error:
                st.error(str(error))
            else:
                _log("Encrypted the record for download")
        if ss.get("vault_file"):
            st.download_button(
                "Download encrypted record", ss.vault_file,
                file_name="labtrail-record.vault", mime="application/octet-stream",
            )
        st.divider()
        upload = st.file_uploader("Open a saved record", type=["vault"], key="vault-upload")
        open_pass = st.text_input("Passphrase", type="password", key="vault-open-pass")
        if st.button("Open record", disabled=upload is None):
            try:
                record = decrypt_record(upload.getvalue(), open_pass)
            except VaultError as error:
                st.error(str(error))
            else:
                for key in _VAULT_KEYS:
                    ss[key] = record[key] if key in record else _VAULT_DEFAULTS[key]()
                ss.candidates = {
                    c.candidate_id: c
                    for r in ss.reports.values()
                    for c in r["result"].candidates
                }
                ss.vault_file = None
                _log(f"Opened saved record ({len(ss.observations)} results)")
                st.rerun()
        st.caption(f"Passphrase: at least {MIN_PASSPHRASE_LENGTH} characters. It cannot be recovered.")


# ---------------------------------------------------------------- layout

def main() -> None:
    ss = _state()
    params = st.query_params
    if "share" in params:
        page_recipient(params.get("share", ""), params.get("k", ""))
        return
    st.sidebar.title("🧪 LabTrail")
    st.sidebar.caption("Patient-controlled lab history · demo")
    st.sidebar.warning(SIDEBAR_NOTICE)
    pages = {"Upload": page_upload, "Review": page_review, "Timeline": page_timeline, "Share": page_share}
    choice = st.sidebar.radio("Step", list(pages))
    pending = sum(
        1 for r in ss.reports.values() if r["confirmed"]
        for c in r["result"].candidates if ss.candidates[c.candidate_id].lifecycle_status == "CANDIDATE"
    )
    st.sidebar.metric("Rows awaiting review", pending)
    st.sidebar.metric("Accepted results", sum(o.lifecycle_status == "ACCEPTED" for o in ss.observations))
    vault_panel()
    with st.sidebar.expander("Activity log"):
        for when, message in reversed(ss.activity[-30:]):
            st.caption(f"{when} · {message}")
    pages[choice]()


main()
