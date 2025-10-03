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

    # --- Validation Report Logic ---
    report = []
    skip_pass_ids = set()

    # Utility: expand column ranges like "Q3_1 to Q3_13"
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

    # Utility: get all dataset columns starting with prefix
    def expand_prefix(prefix, df_cols):
        return [c for c in df_cols if c.startswith(prefix)]

    for _, rule in rules_df.iterrows():
        q = str(rule["Question"]).strip()
        check_types = [c.strip() for c in str(rule["Check_Type"]).split(";")]
        conditions = [c.strip() for c in str(rule.get("Condition", "")).split(";")]

        # --- Robust condition pairing ---
        # Repeat last condition if fewer conditions than check types
        if len(conditions) < len(check_types):
            conditions += [conditions[-1]] * (len(check_types) - len(conditions))
        # Truncate extra conditions if more than check types
        if len(conditions) > len(check_types):
            conditions = conditions[:len(check_types)]

        for i, check_type in enumerate(check_types):
            condition = conditions[i] if i < len(conditions) else None

            # 1️⃣ Straightliner
            if check_type == "Straightliner":
                related_cols = [q] if q in df.columns else expand_prefix(q, df.columns)
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
                else:
                    report.append({
                        "RespondentID": None,
                        "Question": q,
                        "Check_Type": "Straightliner",
                        "Issue": "No matching columns for straightliner"
                    })
                continue

            # 2️⃣ Missing
            if check_type == "Missing":
                related_cols = [q] if q in df.columns else expand_prefix(q, df.columns)
                for col in related_cols:
                    missing = df[col].isna().sum()
                    if missing > 0:
                        offenders = df.loc[df[col].isna(), "RespondentID"]
                        for rid in offenders:
                            if rid not in skip_pass_ids:
                                report.append({"RespondentID": rid, "Question": col,
                                               "Check_Type": "Missing", "Issue": "Value is missing"})

            # 3️⃣ Range
            elif check_type == "Range":
                related_cols = [q] if q in df.columns else expand_prefix(q, df.columns)
                for col in related_cols:
                    try:
                        if "-" not in str(condition):
                            raise ValueError("Not a valid range format")
                        min_val, max_val = map(float, condition.split("-"))
                        mask = ~df[col].between(min_val, max_val)
                        offenders = df.loc[mask & (~df["RespondentID"].isin(skip_pass_ids)), "RespondentID"]
                        for rid in offenders:
                            report.append({"RespondentID": rid, "Question": col,
                                           "Check_Type": "Range",
                                           "Issue": f"Value out of range ({min_val}-{max_val})"})
                    except Exception:
                        report.append({"RespondentID": None, "Question": col,
                                       "Check_Type": "Range",
                                       "Issue": f"Invalid range condition ({condition})"})

            # 4️⃣ Skip
            elif check_type == "Skip":
                try:
                    if "then" not in str(condition):
                        raise ValueError("Not a valid skip format")

                    if_part, then_part = condition.split("then", 1)
                    if_part, then_part = if_part.strip(), then_part.strip()

                    if if_part.lower().startswith("if"):
                        conds_text = if_part[2:].strip()
                    else:
                        conds_text = if_part

                    # Build IF mask with multiple AND/OR conditions
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

                    # Parse THEN part
                    then_expr = then_part.split()[0]
                    if "to" in then_part:
                        target_cols = expand_range(then_part.split()[0] + " " + then_part.split()[1] + " " + then_part.split()[2], df.columns)
                    elif then_expr.endswith("_"):
                        target_cols = expand_prefix(then_expr, df.columns)
                    else:
                        target_cols = [then_expr]

                    should_be_blank = "blank" in then_part.lower()

                    for then_q in target_cols:
                        if then_q not in df.columns:
                            report.append({
                                "RespondentID": None, "Question": q,
                                "Check_Type": "Skip",
                                "Issue": f"Skip condition references missing variable '{then_q}'"
                            })
                            continue

                        blank_mask = df[then_q].isna() | (df[then_q].astype(str).str.strip() == "")
                        not_blank_mask = ~blank_mask

                        if should_be_blank:
                            offenders = df.loc[mask & not_blank_mask, "RespondentID"]
                            for rid in offenders:
                                report.append({"RespondentID": rid, "Question": then_q,
                                               "Check_Type": "Skip",
                                               "Issue": "Answered but should be blank"})
                        else:
                            offenders = df.loc[mask & blank_mask, "RespondentID"]
                            for rid in offenders:
                                report.append({"RespondentID": rid, "Question": then_q,
                                               "Check_Type": "Skip",
                                               "Issue": "Blank but should be answered"})
                except Exception as e:
                    report.append({"RespondentID": None, "Question": q,
                                   "Check_Type": "Skip",
                                   "Issue": f"Invalid skip rule format ({condition}) -> {e}"})

            # 5️⃣ Multi-Select
            elif check_type == "Multi-Select":
                related_cols = expand_prefix(q, df.columns)
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

            # 6️⃣ OpenEnd_Junk
            elif check_type == "OpenEnd_Junk":
                related_cols = [q] if q in df.columns else expand_prefix(q, df.columns)
                for col in related_cols:
                    junk = df[col].astype(str).str.len() < 3
                    offenders = df.loc[junk, "RespondentID"]
                    for rid in offenders:
                        report.append({"RespondentID": rid, "Question": col,
                                       "Check_Type": "OpenEnd_Junk",
                                       "Issue": "Open-end looks like junk/low-effort"})

            # 7️⃣ Duplicate
            elif check_type == "Duplicate":
                related_cols = [q] if q in df.columns else expand_prefix(q, df.columns)
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
