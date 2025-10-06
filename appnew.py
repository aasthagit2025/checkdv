import streamlit as st
import pandas as pd
import pyreadstat
import io
import re

st.title("📊 Survey Data Validation Tool — Enhanced Skip Handling")

# --- File Upload ---
data_file = st.file_uploader("Upload survey data (CSV, Excel, or SAV)", type=["csv", "xlsx", "sav"])
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

    # Identify ID column
    id_col = next((c for c in ["RespondentID", "Password", "RespID", "RID"] if c in df.columns), None)
    if not id_col:
        st.error("No respondent ID column found (expected 'RespondentID' or 'Password').")
        st.stop()

    # --- Load Rules ---
    rules_df = pd.read_excel(rules_file)
    report = []

    # --- Utility Functions ---
    def expand_prefix(prefix, df_cols):
        return [c for c in df_cols if c.startswith(prefix)]

    def expand_range(expr, df_cols):
        expr = expr.strip()
        if "to" in expr:
            start, end = [x.strip() for x in expr.split("to")]
            base = re.match(r"([A-Za-z0-9_]+?)(\d+)$", start)
            base2 = re.match(r"([A-Za-z0-9_]+?)(\d+)$", end)
            if base and base2 and base.group(1) == base2.group(1):
                prefix = base.group(1)
                return [f"{prefix}{i}" for i in range(int(base.group(2)), int(base2.group(2)) + 1) if f"{prefix}{i}" in df_cols]
        return [expr] if expr in df_cols else []

    def get_condition_mask(cond_text, df):
        """Parse logical conditions like: If A1=1 and B2>3"""
        cond_text = cond_text.strip()
        if cond_text.lower().startswith("if"):
            cond_text = cond_text[2:].strip()

        or_groups = re.split(r'\s+or\s+', cond_text, flags=re.IGNORECASE)
        mask = pd.Series(False, index=df.index)

        for or_group in or_groups:
            and_parts = re.split(r'\s+and\s+', or_group, flags=re.IGNORECASE)
            sub_mask = pd.Series(True, index=df.index)

            for part in and_parts:
                part = part.strip().replace("<>", "!=")
                matched = False
                for op in ["<=", ">=", "!=", "<", ">", "="]:
                    if op in part:
                        col, val = [p.strip() for p in part.split(op, 1)]
                        if col not in df.columns:
                            sub_mask &= False
                            matched = True
                            break
                        # Attempt numeric comparison if possible
                        col_vals = df[col]
                        try:
                            val_num = float(val)
                            col_vals_num = pd.to_numeric(col_vals, errors='coerce')
                            if op == "<=":
                                sub_mask &= col_vals_num <= val_num
                            elif op == ">=":
                                sub_mask &= col_vals_num >= val_num
                            elif op == "<":
                                sub_mask &= col_vals_num < val_num
                            elif op == ">":
                                sub_mask &= col_vals_num > val_num
                            elif op == "=":
                                sub_mask &= col_vals_num == val_num
                        except ValueError:
                            val_str = str(val)
                            if op in ["!=", "<>"]:
                                sub_mask &= col_vals.astype(str).str.strip() != val_str
                            elif op == "=":
                                sub_mask &= col_vals.astype(str).str.strip() == val_str
                        matched = True
                        break
                if not matched:
                    sub_mask &= False
            mask |= sub_mask
        return mask

    # --- Main Validation Loop ---
    for _, rule in rules_df.iterrows():
        q = str(rule["Question"]).strip()
        check_types = [x.strip().lower() for x in str(rule["Check_Type"]).split(";")]
        conditions = [x.strip() for x in str(rule["Condition"]).split(";")]

        related_cols = [q] if q in df.columns else expand_prefix(q, df.columns)
        skip_mask = None

        # --- Step 1: Evaluate Skip first ---
        if "skip" in check_types:
            i = check_types.index("skip")
            condition = conditions[i] if i < len(conditions) else ""
            try:
                if "then" not in condition.lower():
                    raise ValueError("Invalid skip format")
                if_part, then_part = re.split(r'(?i)then', condition, maxsplit=1)
                skip_mask = get_condition_mask(if_part, df)

                then_expr = then_part.strip().split()[0]
                should_be_blank = "blank" in then_part.lower()
                if "to" in then_part:
                    target_cols = expand_range(then_part, df.columns)
                elif then_expr.endswith("_"):
                    target_cols = expand_prefix(then_expr, df.columns)
                else:
                    target_cols = [then_expr]

                for col in target_cols:
                    if col not in df.columns:
                        report.append({id_col: None, "Question": q, "Check_Type": "Skip", "Issue": f"Target variable '{col}' not found"})
                        continue
                    blank_mask = df[col].isna() | (df[col].astype(str).str.strip() == "")
                    not_blank_mask = ~blank_mask

                    # --- Flag offenders based on skip logic ---
                    # Respondent SHOULD answer
                    offenders_answered = df.loc[skip_mask & blank_mask, id_col]
                    # Respondent SHOULD be skipped
                    offenders_skipped = df.loc[~skip_mask & not_blank_mask, id_col]

                    for rid in offenders_answered:
                        report.append({id_col: rid, "Question": col, "Check_Type": "Skip", "Issue": "Blank but should be answered"})
                    for rid in offenders_skipped:
                        report.append({id_col: rid, "Question": col, "Check_Type": "Skip", "Issue": "Answered but should be blank"})
            except Exception as e:
                report.append({id_col: None, "Question": q, "Check_Type": "Skip", "Issue": f"Invalid skip rule: {e}"})

        # --- Step 2: Evaluate other checks only for respondents who should answer ---
        rows_to_check = skip_mask if skip_mask is not None else pd.Series(True, index=df.index)

        for i, check_type in enumerate(check_types):
            if check_type == "skip":
                continue
            condition = conditions[i] if i < len(conditions) else ""

            if check_type == "range":
                try:
                    min_val, max_val = map(float, condition.replace("to", "-").split("-"))
                    for col in related_cols:
                        col_vals = pd.to_numeric(df[col], errors="coerce")
                        valid_mask = col_vals.between(min_val, max_val)
                        offenders = df.loc[rows_to_check & ~valid_mask, id_col]
                        for rid in offenders:
                            report.append({id_col: rid, "Question": col, "Check_Type": "Range", "Issue": f"Value out of range ({min_val}-{max_val})"})
                except Exception:
                    report.append({id_col: None, "Question": q, "Check_Type": "Range", "Issue": f"Invalid range format ({condition})"})

            elif check_type == "missing":
                for col in related_cols:
                    blank_mask = df[col].isna() | (df[col].astype(str).str.strip() == "")
                    offenders = df.loc[rows_to_check & blank_mask, id_col]
                    for rid in offenders:
                        report.append({id_col: rid, "Question": col, "Check_Type": "Missing", "Issue": "Value is missing"})

            elif check_type == "straightliner":
                if len(related_cols) == 1:
                    related_cols = expand_prefix(related_cols[0], df.columns)
                if len(related_cols) > 1:
                    same_resp = df[related_cols].nunique(axis=1) == 1
                    offenders = df.loc[rows_to_check & same_resp, id_col]
                    for rid in offenders:
                        report.append({id_col: rid, "Question": ",".join(related_cols), "Check_Type": "Straightliner", "Issue": "Same response across all items"})

            elif check_type == "multi-select":
                related_cols = expand_prefix(q, df.columns)
                offenders = df.loc[rows_to_check & (df[related_cols].fillna(0).sum(axis=1) == 0), id_col]
                for rid in offenders:
                    report.append({id_col: rid, "Question": q, "Check_Type": "Multi-Select", "Issue": "No options selected"})

            elif check_type == "openend_junk":
                for col in related_cols:
                    junk = df[col].astype(str).str.len() < 3
                    offenders = df.loc[rows_to_check & junk, id_col]
                    for rid in offenders:
                        report.append({id_col: rid, "Question": col, "Check_Type": "OpenEnd_Junk", "Issue": "Open-end looks like junk"})

            elif check_type == "duplicate":
                for col in related_cols:
                    dupes = df.loc[rows_to_check & df.duplicated(subset=[col], keep=False), id_col]
                    for rid in dupes:
                        report.append({id_col: rid, "Question": col, "Check_Type": "Duplicate", "Issue": "Duplicate value found"})

    # --- Final Report ---
    report_df = pd.DataFrame(report)
    st.success(f"Validation completed! Total issues found: {len(report_df)}")
    st.dataframe(report_df)

    # --- Download Report ---
    out = io.BytesIO()
    with pd.ExcelWriter(out, engine="openpyxl") as writer:
        report_df.to_excel(writer, index=False, sheet_name="Validation Report")
    st.download_button(
        label="📥 Download Validation Report",
        data=out.getvalue(),
        file_name="validation_report.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
