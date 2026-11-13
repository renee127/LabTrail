"""LabTrail demo UI: upload -> confirm -> review -> timeline -> share.

Run with:  streamlit run app.py
Synthetic data only. Everything lives in this browser session's memory; nothing
is encrypted or stored on a server in this demo.
"""

from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

import altair as alt
import pandas as pd
import streamlit as st
from PIL import Image

from labtrail_algorithms import find_duplicate_observation_groups, partition_review_queue
from labtrail_extraction import extract_document_ocr_line_candidates
from labtrail_redaction import rasterize_pdf_page, redact_source_page
from labtrail_workflow import (
    ANALYTE_LABELS,
    COMPARABILITY_LABELS,
    RULE_VERSION,
    accept_candidate,
    analyte_timeline,
    build_share,
    is_result_like,
    suggest_analyte_code,
    suggest_report_metadata,
)
from models import CandidateExtraction, ContextNote


SAMPLES_DIR = Path(__file__).with_name("samples")
RASTER_DPI = 150
MIME_BY_SUFFIX = {".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}
NOTE_CATEGORIES = ["MEDICATION_CHANGE", "DIET", "ILLNESS", "ACTIVITY", "FREE_TEXT"]
REVOCATION_DISCLOSURE = (
    "Revoking a share stops future access, but it cannot erase anything the recipient "
    "has already viewed, saved, printed, or photographed."
)

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
    return ss


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

def _crop(report: dict, candidate: CandidateExtraction) -> Image.Image | None:
    if candidate.bounding_box is None:
        return None
    page = Image.open(BytesIO(report["pages"][candidate.page_number]))
    w, h = page.size
    x0, y0, x1, y1 = candidate.bounding_box
    pad = 8
    return page.crop((max(0, int(x0 * w) - pad), max(0, int(y0 * h) - pad),
                      min(w, int(x1 * w) + pad), min(h, int(y1 * h) + pad)))


def _decide(candidate: CandidateExtraction, status: str) -> None:
    candidate.lifecycle_status = status


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

    analyte_options = ["(choose)"] + list(ANALYTE_LABELS)
    for report_id, report in confirmed:
        mandatory, standard = partition_review_queue(
            ss.candidates[c.candidate_id] for c in report["result"].candidates
        )
        pending = mandatory + standard
        results = [c for c in pending if is_result_like(c)]
        other = [c for c in pending if not is_result_like(c)]
        meta = report["confirmed"]
        st.subheader(f"{meta['lab']} · {meta['collected']}")
        if not pending:
            st.success("Everything on this report has been reviewed.")
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
                    crop = _crop(report, c)
                    if crop is not None:
                        st.image(crop, caption=f"Source: page {c.page_number}")
                    st.caption(f"OCR confidence {c.field_confidences['value']:.0%}")
                with form_col, st.form(f"review-{c.candidate_id}"):
                    suggested = suggest_analyte_code(c.raw_analyte_name)
                    a, v, u, r = st.columns([3, 2, 2, 2])
                    code = a.selectbox(
                        f"Analyte (read as “{c.raw_analyte_name}”)",
                        analyte_options,
                        index=analyte_options.index(suggested) if suggested else 0,
                        format_func=lambda k: ANALYTE_LABELS.get(k, k),
                    )
                    value = v.text_input("Value", value=c.raw_value or "")
                    unit = u.text_input("Unit", value=c.raw_unit or "")
                    ref = r.text_input("Reference range", value=c.reference_interval or "")
                    accept_col, reject_col = st.columns(2)
                    accept = accept_col.form_submit_button("Accept", type="primary", width="stretch")
                    reject = reject_col.form_submit_button("Reject", width="stretch")
                if accept:
                    try:
                        observation = accept_candidate(
                            c, analyte_code=code, value=value, unit=unit,
                            reference_interval=ref, source_lab=meta["lab"],
                            collected=meta["collected"], accepted_at=_now(),
                        )
                    except ValueError as error:
                        st.error(str(error))
                    else:
                        ss.observations.append(observation)
                        _decide(c, "ACCEPTED")
                        how = "corrected" if observation.provenance_flag == "CORRECTED_BY_PATIENT" else "accepted"
                        _log(f"{how.capitalize()} {ANALYTE_LABELS[code]} {value} {unit} from {meta['lab']}")
                        st.rerun()
                if reject:
                    _decide(c, "REJECTED")
                    _log(f"Rejected “{c.raw_analyte_name}” on {report['filename']}")
                    st.rerun()


# ---------------------------------------------------------------- timeline

def page_timeline() -> None:
    ss = _state()
    st.header("3 · Timeline")
    st.caption(
        "Accepted values from every lab, converted to one unit where a versioned rule exists "
        f"(rules {RULE_VERSION}). LabTrail shows what changed between results; it does not "
        "say whether a value is good or bad."
    )
    active = [o for o in ss.observations if o.lifecycle_status == "ACCEPTED"]
    if not active:
        st.info("Accept some results on the **Review** page first.")
        return

    available = [code for code in ANALYTE_LABELS if any(o.analyte_code == code for o in active)]
    code = st.radio("Analyte", available, format_func=ANALYTE_LABELS.get, horizontal=True)
    points = analyte_timeline(ss.observations, code)
    plotted = [p for p in points if p.observation.canonical_value is not None]
    retained = [p for p in points if p.observation.canonical_value is None]

    if plotted:
        unit = plotted[0].observation.canonical_unit
        df = pd.DataFrame([
            {
                "date": pd.Timestamp(p.observation.collection_datetime),
                "value": float(p.observation.canonical_value),
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
            y=alt.Y("value:Q", title=f"{ANALYTE_LABELS[code]} ({unit})", scale=alt.Scale(zero=False)),
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
        st.caption("Dashed lines are your context notes. Hover a point for its source details.")

    st.write("**All results for this analyte**")
    for p in points:
        o = p.observation
        cols = st.columns([2, 3, 2, 2, 2, 3, 1])
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
            _log(f"Retracted {ANALYTE_LABELS[o.analyte_code]} {o.original_value} {o.original_unit} ({o.collection_datetime})")
            st.rerun()
        if o.provenance_flag == "CORRECTED_BY_PATIENT":
            cols[1].caption("value corrected by you")

    if retained:
        st.warning(
            "Not plotted: " + "; ".join(
                f"{p.observation.original_value} {p.observation.original_unit} on {p.observation.collection_datetime} "
                f"({p.observation.source_lab})" for p in retained
            ) + ". No supported conversion to the chart's unit exists, so the value is kept exactly as reported."
        )

    duplicates = find_duplicate_observation_groups(active)
    if duplicates:
        st.warning(f"{len(duplicates)} possible duplicate result(s): same analyte, date, and lab. Review and retract one if needed.")

    with st.expander("Add a context note"):
        with st.form("note"):
            note_date = st.date_input("Date", value=date.today(), format="YYYY-MM-DD")
            category = st.selectbox("Category", NOTE_CATEGORIES, format_func=lambda c: c.replace("_", " ").title())
            analytes = st.multiselect("Related analytes", available, default=[code], format_func=ANALYTE_LABELS.get)
            body = st.text_input("Note", placeholder="e.g. Started metformin 500 mg")
            if st.form_submit_button("Add note") and body.strip() and analytes:
                now = _now()
                ss.notes.append(ContextNote(uuid4(), category, note_date, body.strip(), analytes, now, now))
                _log(f"Added note on {note_date}: {body.strip()}")
                st.rerun()


# ---------------------------------------------------------------- share

def page_share() -> None:
    ss = _state()
    st.header("4 · Prepare a share")
    st.caption(
        "Choose exactly which analytes and dates a clinician may see. Only accepted results "
        "and in-scope notes are included."
    )
    active = [o for o in ss.observations if o.lifecycle_status == "ACCEPTED"]
    if not active:
        st.info("Accept some results first.")
        return
    dates = [o.collection_datetime for o in active]
    available = [code for code in ANALYTE_LABELS if any(o.analyte_code == code for o in active)]

    with st.form("share"):
        analytes = st.multiselect("Analytes", available, default=available[:1], format_func=ANALYTE_LABELS.get)
        start, end = st.columns(2)
        range_start = start.date_input("From", value=min(dates), format="YYYY-MM-DD")
        range_end = end.date_input("To", value=max(dates), format="YYYY-MM-DD")
        name, email, days = st.columns([2, 2, 1])
        recipient = name.text_input("Recipient name", value="Dr. Synthetic Clinician")
        recipient_email = email.text_input("Recipient email", value="clinician@example.org")
        valid_days = days.number_input("Valid for (days)", min_value=1, max_value=365, value=14)
        st.info(REVOCATION_DISCLOSURE)
        submitted = st.form_submit_button("Prepare share", type="primary")
    if not submitted:
        return
    try:
        now = _now()
        grant, selection, payload = build_share(
            observations=ss.observations, notes=ss.notes, analytes=analytes,
            range_start=range_start, range_end=range_end, recipient_name=recipient,
            recipient_email=recipient_email, now=now, valid_for=timedelta(days=int(valid_days)),
        )
    except ValueError as error:
        st.error(str(error))
        return
    _log(f"Prepared share for {recipient}: {len(selection.observations)} results")

    st.success(
        f"{len(selection.observations)} result(s) and {len(selection.context_notes)} note(s) selected "
        f"for {recipient}, expiring {grant.expires_at:%Y-%m-%d}."
    )
    st.download_button(
        "Download structured share (JSON, schema v1)", payload,
        file_name=f"labtrail-share-{grant.grant_id}.json", mime="application/json",
    )

    st.write("**Source pages**")
    st.caption(
        "Source pages are shared only as flattened images with every ungranted row masked. "
        "If LabTrail cannot prove it found every result row on a page, the page is withheld."
    )
    reasons = {
        "ROW_COVERAGE_UNVERIFIED": "withheld: the OCR prototype cannot prove it found every row",
        "NO_GRANTED_OBSERVATION": "withheld: nothing on this page is in scope",
    }
    for plan in selection.source_pages:
        report = ss.reports[plan.report_id]
        outcome = redact_source_page(
            page_raster=report["pages"][plan.page_number], report_id=plan.report_id,
            page_number=plan.page_number, extraction_result=report["result"],
            observations=ss.observations, grant=grant, as_of=now,
            region_confidence_threshold=0.8, mask_padding_pixels=4,
        )
        label = reasons.get(outcome.withheld_reason or "", outcome.withheld_reason or "redacted image")
        st.write(f"- {report['filename']} p.{plan.page_number}: {label}")
        if outcome.redacted_png:
            st.image(outcome.redacted_png)

    with st.expander("Preview share JSON"):
        st.code(payload.decode("utf-8")[:4000], language="json")
    st.caption(
        "Demo limits: the share is not encrypted, there is no recipient viewer, and revocation "
        "is not implemented. These are designed in the report but not built."
    )


# ---------------------------------------------------------------- layout

def main() -> None:
    ss = _state()
    st.sidebar.title("🧪 LabTrail")
    st.sidebar.caption("Patient-controlled lab history · demo")
    st.sidebar.warning("Synthetic data only. LabTrail organizes results; it does not diagnose or give medical advice.")
    pages = {"Upload": page_upload, "Review": page_review, "Timeline": page_timeline, "Share": page_share}
    choice = st.sidebar.radio("Step", list(pages))
    pending = sum(
        1 for r in ss.reports.values() if r["confirmed"]
        for c in r["result"].candidates if ss.candidates[c.candidate_id].lifecycle_status == "CANDIDATE"
    )
    st.sidebar.metric("Rows awaiting review", pending)
    st.sidebar.metric("Accepted results", sum(o.lifecycle_status == "ACCEPTED" for o in ss.observations))
    with st.sidebar.expander("Activity log"):
        for when, message in reversed(ss.activity[-30:]):
            st.caption(f"{when} · {message}")
    pages[choice]()


main()
