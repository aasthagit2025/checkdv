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

    if "RespondentID" not in df.columns:
        st.error("Dataset must have a 'RespondentID' column")
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

    # --- Skip Logic Parser ---
    def get_skip_mask(condition, df):
        """
        Returns a boolean Series: True for respondents who SHOULD answer based on skip condition
        Supports AND/OR conditions
        """
        if not condition or "then" not in condition.lower():
            return pd.Series(True, index=df.index)  # default: everyone should answer

        if_part = re.split(r'\bthen\b', condition, flags=re.IGNORECASE, maxsplit=1)[0].strip()
        if if_part.lower().startswith("if"):
            if_part = if_part[2:].strip()

        # Split OR groups
        or_groups = re.split(r'\s+or\s+', if_part, flags=re.IGNORECASE)
        mask = pd.Series(False, index=df.index)

        for or_group in or_groups:
            and_parts = re.split(r'\s+and\s+', or_group, flags=re.IGNORECASE)
            sub_mask = pd.Series(True, index=df.index)
            for part in and_parts:
                part = part.strip().replace("<>", "!=")
                match = re.match(r"([A-Za-z0-9_]+)\s*(<=|>=|!=|=|<|>)\s*([\d\.\-]+)", part)
                if match:
                    col, op, val = match.groups()
                    if col not in df.columns:
                        sub_mask &= False
                        continue
                    val = float(val)
                    col_vals = pd.to_numeric(df[col], errors="coerce")
                    if op == "<=":
                        sub_mask &= col_vals <= val
                    elif op == ">=":
                        sub_mask &= col_vals >= val
                    elif op == "<":
                        sub_mask &= col_vals < val
                    elif op == ">":
                        sub_mask &= col_vals > val
                    elif op in ["=", "=="]:
                        sub_mask &= col_vals == val
                    elif op == "!=":
                        sub_mask &= col_vals != val
                else:
                    sub_mask &= False  # unrecognized format
            mask |= sub_mask
        return mask

    # --- Validation Loop ---
    for _, rule in rules_df.iterrows():
        q = str(rule["Question"]).strip()
        check_types = [c.strip() for c in str(rule["Check_Type"]).split(";")]
        conditions = [c.strip() for c in str(rule.get("Condition", "")).split(";")]

        # First determine skip mask if Skip exists
        skip_condition = None
        if "Skip" in check_types:
            idx = check_types.index("Skip")
            skip_condition = conditions[idx] if idx < len(conditions) else None
        skip_mask = get_skip_mask(skip_condition, df) if skip_condition else pd.Series(True, index=df.index)

        for i, check_type in enumerate(check_types):
            condition = conditions[i] if i < len(conditions) else None
            related_cols = [q] if q in df.columns else expand_prefix(q, df.columns)

            # 1️⃣ Range
            if check_type == "Range":
                for col in related_cols:
                    try:
                        if "-" not in str(condition):
                            raise ValueError("Invalid range format")
                        min_val, max_val = map(float, condition.split("-"))
                        mask = ~df[col].between(min_val, max_val) & skip_mask
                        offenders = df.loc[mask, "RespondentID"]
                        for rid in offenders:
                            report.append({"RespondentID": rid, "Question": col,
                                           "Check_Type": "Range",
                                           "Issue": f"Value out of range ({min_val}-{max_val})"})
                    except Exception as e:
                        report.append({"RespondentID": None, "Question": col,
                                       "Check_Type": "Range",
                                       "Issue": f"Invalid range condition ({condition}) -> {e}"})

            # 2️⃣ Missing / Should be answered
            if check_type == "Missing":
                for col in related_cols:
                    missing_mask = df[col].isna() & skip_mask
                    offenders = df.loc[missing_mask, "RespondentID"]
                    for rid in offenders:
                        report.append({"RespondentID": rid, "Question": col,
                                       "Check_Type": "Missing",
                                       "Issue": "Value is missing (should have answered)"})

            # 3️⃣ Skip check: also flag blank if respondent should answer
            if check_type == "Skip":
                for col in related_cols:
                    blank_mask = (df[col].isna() | (df[col].astype(str).str.strip() == "")) & skip_mask
                    offenders = df.loc[blank_mask, "RespondentID"]
                    for rid in offenders:
                        report.append({"RespondentID": rid, "Question": col,
                                       "Check_Type": "Skip",
                                       "Issue": "Blank but should be answered"})

    # --- Create Report ---
    report_df = pd.DataFrame(report)

    st.write("### Validation Report")
    st.dataframe(report_df)

    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        report_df.to_excel(writer, index=False, sheet_name="Validation Report")

    st.download_button("Download Validation Report",
                       data=output.getvalue(),
                       file_name="validation_report.xlsx",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
