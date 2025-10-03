import streamlit as st
import pandas as pd
import pyreadstat
import io
import re

st.title("📊 Survey Data Validation Tool - Multi Skip & Range Enabled")

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
            base1 = re.match(r"([A-Za-z0-9_]+?)(\d+)$", start)
            base2 = re.match(r"([A-Za-z0-9_]+?)(\d+)$", end)
            if base1 and base2 and base1.group(1) == base2.group(1):
                prefix = base1.group(1)
                start_num, end_num = int(base1.group(2)), int(base2.group(2))
                return [f"{prefix}{i}" for i in range(start_num, end_num + 1) if f"{prefix}{i}" in df_cols]
        return [expr] if expr in df_cols else []

    def expand_prefix(prefix, df_cols):
        return [c for c in df_cols if c.startswith(prefix)]

    def get_skip_mask(skip_conditions, df):
        """Return combined mask of all skip conditions for a question"""
        combined_mask = pd.Series(False, index=df.index)
        for cond in skip_conditions:
            if "then" not in cond.lower():
                continue
            if_part, _ = cond.split("then", 1)
            conds_text = if_part.lower().lstrip("if").strip()
            or_groups = re.split(r'\s+or\s+', conds_text, flags=re.IGNORECASE)
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
            combined_mask |= mask
        # If no skip conditions, all respondents should answer
        if combined_mask.sum() == 0:
            combined_mask = pd.Series(True, index=df.index)
        return combined_mask

    # --- Main Validation Loop ---
    for _, rule in rules_df.iterrows():
        q = str(rule["Question"]).strip()
        check_types = [c.strip().lower() for c in str(rule["Check_Type"]).split(";")]
        conditions = [c.strip() for c in str(rule.get("Condition", "")).split(";")]
        related_cols = [q] if q in df.columns else expand_prefix(q, df.columns)

        # Collect skip rules for this question
        skip_conditions = [conditions[i] for i, ct in enumerate(check_types) if ct == "skip"]

        combined_skip_mask = get_skip_mask(skip_conditions, df)

        # Apply Skip Validations
        for cond in skip_conditions:
            if "then" not in cond.lower():
                continue
            then_part = cond.split("then", 1)[1].strip()
            then_expr = then_part.split()[0]
            if "to" in then_part:
                target_cols = expand_range(then_part, df.columns)
            elif then_expr.endswith("_"):
                target_cols = expand_prefix(then_expr, df.columns)
            else:
                target_cols = [then_expr]

            for col in target_cols:
                if col not in df.columns:
                    report.append({
                        "RespondentID": None,
                        "Question": col,
                        "Check_Type": "Skip",
                        "Issue": f"Skip condition references missing variable '{col}'"
                    })
                    continue

                blank_mask = (df[col].isna() | (df[col].astype(str).str.strip() == "")) & combined_skip_mask
                offenders = df.loc[blank_mask, "RespondentID"]
                for rid in offenders:
                    report.append({"RespondentID": rid, "Question": col,
                                   "Check_Type": "Skip",
                                   "Issue": "Blank but should be answered"})

                answered_mask = (~df[col].isna() & (df[col].astype(str).str.strip() != "")) & (~combined_skip_mask)
                offenders = df.loc[answered_mask, "RespondentID"]
                for rid in offenders:
                    report.append({"RespondentID": rid, "Question": col,
                                   "Check_Type": "Skip",
                                   "Issue": "Answered but should be skipped"})

        # Apply Range Validations only for respondents who should answer
        for idx, ctype in enumerate(check_types):
            if ctype != "range":
                continue
            condition = conditions[idx]
            for col in related_cols:
                try:
                    if "-" not in str(condition):
                        raise ValueError("Not a valid range format")
                    min_val, max_val = map(float, condition.split("-"))
                    out_of_range_mask = ~df[col].between(min_val, max_val) & combined_skip_mask
                    offenders = df.loc[out_of_range_mask, "RespondentID"]
                    for rid in offenders:
                        report.append({"RespondentID": rid, "Question": col,
                                       "Check_Type": "Range",
                                       "Issue": f"Value out of range ({min_val}-{max_val})"})
                except Exception:
                    report.append({"RespondentID": None, "Question": col,
                                   "Check_Type": "Range",
                                   "Issue": f"Invalid range condition ({condition})"})

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
