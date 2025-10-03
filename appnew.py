import streamlit as st
import pandas as pd
import pyreadstat
import io
import re

st.title("📊 Survey Data Validation Tool")

# --- File Upload ---
data_file = st.file_uploader("Upload your survey data file (CSV, Excel, or SPSS)", type=["csv", "xlsx", "sav"])
rules_file = st.file_uploader("Upload validation rules (Excel)", type=["xlsx"])

if data_file and rules_file:
    # --- Load Data ---
    if data_file.name.endswith(".csv"):
        df = pd.read_csv(data_file, encoding_errors="ignore")
    elif data_file.name.endswith(".xlsx"):
        df = pd.read_excel(data_file)
    elif data_file.name.endswith(".sav"):
        df, meta = pyreadstat.read_sav(data_file)
    else:
        st.error("Unsupported file type")
        st.stop()

    # --- Load Rules ---
    rules_df = pd.read_excel(rules_file)

    report = []

    # --- Utilities ---
    def expand_range(expr, df_cols):
        expr = expr.strip()
        if "to" in expr:
            start, end = [x.strip() for x in expr.split("to")]
            base = re.match(r"([A-Za-z0-9_]+?)(\d+)$", start)
            base2 = re.match(r"([A-Za-z0-9_]+?)(\d+)$", end)
            if base and base2 and base.group(1) == base2.group(1):
                prefix = base.group(1)
                start_num, end_num = int(base.group(2)), int(base2.group(2))
                return [f"{prefix}{i}" for i in range(start_num, end_num + 1) if f"{prefix}{i}" in df_cols]
        return [expr] if expr in df_cols else []

    def expand_prefix(prefix, df_cols):
        return [c for c in df_cols if c.startswith(prefix)]

    def get_skip_mask(condition_text, df):
        """Return a boolean mask of respondents who should answer based on skip condition"""
        condition_text = condition_text.strip()
        if condition_text.lower().startswith("if"):
            condition_text = condition_text[2:].strip()
        or_groups = re.split(r'\s+or\s+', condition_text, flags=re.IGNORECASE)
        mask = pd.Series(False, index=df.index)
        for or_group in or_groups:
            and_parts = re.split(r'\s+and\s+', or_group, flags=re.IGNORECASE)
            sub_mask = pd.Series(True, index=df.index)
            for part in and_parts:
                part = part.strip().replace("<>", "!=")
                for op in ["<=", ">=", "!=", "<>", "<", ">", "="]:
                    if op in part:
                        col, val = [p.strip() for p in part.split(op, 1)]
                        if col not in df.columns:
                            sub_mask &= False
                            break
                        if op in ["<=", ">=", "<", ">"]:
                            val = float(val)
                            col_vals = pd.to_numeric(df[col], errors="coerce")
                            if op == "<=": sub_mask &= col_vals <= val
                            elif op == ">=": sub_mask &= col_vals >= val
                            elif op == "<": sub_mask &= col_vals < val
                            elif op == ">": sub_mask &= col_vals > val
                        elif op in ["!=", "<>"]:
                            sub_mask &= df[col].astype(str).str.strip() != str(val)
                        elif op == "=":
                            sub_mask &= df[col].astype(str).str.strip() == str(val)
                        break
            mask |= sub_mask
        return mask

    # --- Main Validation Loop ---
    for _, rule in rules_df.iterrows():
        q = str(rule["Question"]).strip()
        check_types = [c.strip().lower() for c in str(rule["Check_Type"]).split(";")]
        conditions = [c.strip() for c in str(rule.get("Condition", "")).split(";")]
        related_cols = [q] if q in df.columns else expand_prefix(q, df.columns)

        skip_mask = None
        # Process Skip first to get valid respondents
        if "skip" in check_types:
            skip_index = check_types.index("skip")
            condition = conditions[skip_index]
            if "then" in condition.lower():
                if_part, then_part = condition.split("then", 1)
                skip_mask = get_skip_mask(if_part, df)

                then_expr = then_part.strip().split()[0]
                if "to" in then_part:
                    skip_target_cols = expand_range(then_part, df.columns)
                elif then_expr.endswith("_"):
                    skip_target_cols = expand_prefix(then_expr, df.columns)
                else:
                    skip_target_cols = [then_expr]

                for col in skip_target_cols:
                    if col not in df.columns:
                        report.append({
                            "RespondentID": None,
                            "Question": col,
                            "Check_Type": "Skip",
                            "Issue": f"Skip condition references missing variable '{col}'"
                        })
                        continue

                    # Respondents who should answer but are blank
                    blank_mask = (df[col].isna() | (df[col].astype(str).str.strip() == "")) & skip_mask
                    offenders = df.loc[blank_mask, "RespondentID"]
                    for rid in offenders:
                        report.append({"RespondentID": rid, "Question": col,
                                       "Check_Type": "Skip",
                                       "Issue": "Blank but should be answered"})

                    # Respondents who should skip but answered
                    answered_mask = (~df[col].isna() & (df[col].astype(str).str.strip() != "")) & (~skip_mask)
                    offenders = df.loc[answered_mask, "RespondentID"]
                    for rid in offenders:
                        report.append({"RespondentID": rid, "Question": col,
                                       "Check_Type": "Skip",
                                       "Issue": "Answered but should be skipped"})

        # Apply Range checks only for respondents who should answer
        if "range" in check_types:
            range_index = check_types.index("range")
            condition = conditions[range_index]
            for col in related_cols:
                try:
                    if "-" not in str(condition):
                        raise ValueError("Not a valid range format")
                    min_val, max_val = map(float, condition.split("-"))
                    effective_mask = skip_mask if skip_mask is not None else pd.Series(True, index=df.index)
                    out_of_range_mask = ~df[col].between(min_val, max_val) & effective_mask
                    offenders = df.loc[out_of_range_mask, "RespondentID"]
                    for rid in offenders:
                        report.append({"RespondentID": rid, "Question": col,
                                       "Check_Type": "Range",
                                       "Issue": f"Value out of range ({min_val}-{max_val})"})
                except Exception:
                    report.append({"RespondentID": None, "Question": col,
                                   "Check_Type": "Range",
                                   "Issue": f"Invalid range condition ({condition})"})

        # Straightliner check
        if "straightliner" in check_types:
            if len(related_cols) > 1:
                straightliners = df[related_cols].nunique(axis=1)
                offenders = df.loc[straightliners == 1, "RespondentID"]
                for rid in offenders:
                    report.append({
                        "RespondentID": rid,
                        "Question": ",".join(related_cols),
                        "Check_Type": "Straightliner",
                        "Issue": "Same response across all items"
                    })

        # Missing check
        if "missing" in check_types:
            for col in related_cols:
                missing_mask = df[col].isna()
                offenders = df.loc[missing_mask, "RespondentID"]
                for rid in offenders:
                    report.append({"RespondentID": rid, "Question": col,
                                   "Check_Type": "Missing",
                                   "Issue": "Value is missing"})

        # Multi-Select check
        if "multi-select" in check_types:
            for col in related_cols:
                offenders = df.loc[~df[col].isin([0, 1]), "RespondentID"]
                for rid in offenders:
                    report.append({"RespondentID": rid, "Question": col,
                                   "Check_Type": "Multi-Select",
                                   "Issue": "Invalid value (not 0/1)"})
            if len(related_cols) > 0:
                offenders = df.loc[df[related_cols].fillna(0).sum(axis=1) == 0, "RespondentID"]
                for rid in offenders:
                    report.append({"RespondentID": rid, "Question": q,
                                   "Check_Type": "Multi-Select",
                                   "Issue": "No options selected"})

        # OpenEnd_Junk check
        if "openend_junk" in check_types:
            for col in related_cols:
                junk_mask = df[col].astype(str).str.len() < 3
                offenders = df.loc[junk_mask, "RespondentID"]
                for rid in offenders:
                    report.append({"RespondentID": rid, "Question": col,
                                   "Check_Type": "OpenEnd_Junk",
                                   "Issue": "Open-end looks like junk/low-effort"})

        # Duplicate check
        if "duplicate" in check_types:
            for col in related_cols:
                duplicate_ids = df[df.duplicated(subset=[col], keep=False)]["RespondentID"]
                for rid in duplicate_ids:
                    report.append({"RespondentID": rid, "Question": col,
                                   "Check_Type": "Duplicate",
                                   "Issue": "Duplicate value found"})

    # --- Create Report ---
    report_df = pd.DataFrame(report)
    st.write("### Validation Report (detailed by Respondent)")
    st.dataframe(report_df)

    # --- Download Report ---
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        report_df.to_excel(writer, index=False, sheet_name="Validation Report")

    st.download_button(
        label="Download Validation Report",
        data=output.getvalue(),
        file_name="validation_report.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
