import streamlit as st
import pandas as pd
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import json
import datetime

# -----------------------------
# Google Sheets Setup & Helpers
# -----------------------------
# Update these with your own spreadsheet URL and credentials file.
SHEET_URL = "https://docs.google.com/spreadsheets/d/1CiMpAvxTVXpNhUk8I8wa_ILjS-y9zxLv9lrGIR5ULn0/edit?gid=0#gid=0"  # Replace with your Google Sheet URL
CREDENTIALS_FILE = "bill-splitter-450203-3209d2741974.json"

def authenticate_google_sheets():
    """Authenticate with Google Sheets using service account credentials."""
    try:
        scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        creds = ServiceAccountCredentials.from_json_keyfile_name(CREDENTIALS_FILE, scope)
        client = gspread.authorize(creds)
        return client
    except Exception as e:
        st.error("Error authenticating with Google Sheets: " + str(e))
        return None

def load_google_sheet(sheet_url, worksheet_index=0):
    """
    Load data from a specific worksheet (by index) in the Google Sheet.
    worksheet_index=0 loads Sheet1 (Expenses), index=1 loads Sheet2 (Payments).
    """
    client = authenticate_google_sheets()
    if client is None:
        return pd.DataFrame()
    try:
        sheet = client.open_by_url(sheet_url).get_worksheet(worksheet_index)
        data = sheet.get_all_records()
        return pd.DataFrame(data)
    except Exception as e:
        st.error(f"Error loading data from Google Sheets (worksheet index {worksheet_index}): " + str(e))
        return pd.DataFrame()

def append_to_google_sheet(sheet_url, new_data_row, worksheet_index=0):
    """
    Append a new record (as a dict) to the specified worksheet.
    For expenses (worksheet_index=0), columns should be:
      Date, Payer, Amount, Participants, Split Type, Percentages, Notes
    For payments (worksheet_index=1), columns should be:
      Date, Payer, Amount, Payee, Notes
    """
    client = authenticate_google_sheets()
    if client is None:
        return
    try:
        sheet = client.open_by_url(sheet_url).get_worksheet(worksheet_index)
        if worksheet_index == 0:
            columns = ["Date", "Payer", "Amount", "Participants", "Split Type", "Percentages", "Notes"]
        else:
            columns = ["Date", "Payer", "Amount", "Payee", "Notes"]
        row = []
        for col in columns:
            value = new_data_row.get(col, "")
            # For expense records, if Percentages is a dict, store it as a JSON string.
            if col == "Percentages" and isinstance(value, dict):
                value = json.dumps(value)
            row.append(value if value is not None else "")
        sheet.append_row(row)
    except Exception as e:
        st.error(f"Error appending data to Google Sheets (worksheet index {worksheet_index}): " + str(e))

# -----------------------------
# Data Processing & Settlement Functions
# -----------------------------
def format_percentages(percentages, split_type):
    """
    Format the percentages field for display.
    This function converts the stored JSON string (or dict) into a neat string like:
       "Alice: 30%, Bob: 70%"
    For Equal splits or empty percentages, it returns an empty string.
    """
    if split_type == "Equal" or percentages in (None, "", [], {}):
        return ""
    try:
        if isinstance(percentages, str):
            parsed = json.loads(percentages)
        elif isinstance(percentages, dict):
            parsed = percentages
        else:
            return ""
        if not isinstance(parsed, dict):
            return ""
        return ", ".join([f"{str(name)}: {float(value)}%" for name, value in parsed.items()])
    except Exception:
        return ""

def compute_expense_balances(expenses_df):
    """
    Compute net balances from expense records.
    For each expense:
      - The payer’s balance increases by the full amount.
      - Each participant's balance decreases by an equal share.
    (This example uses an equal split for computation; you can extend it to incorporate custom splits if desired.)
    """
    net_balance = {}
    persons = set()
    for _, row in expenses_df.iterrows():
        payer = str(row.get("Payer", "")).strip()
        participants = row.get("Participants", "")
        participant_list = [p.strip() for p in str(participants).split(",") if p.strip()]
        try:
            amount = float(row.get("Amount", 0))
        except:
            amount = 0
        if payer:
            persons.add(payer)
        for p in participant_list:
            persons.add(p)
    for person in persons:
        net_balance[person] = 0.0
    for _, row in expenses_df.iterrows():
        payer = str(row.get("Payer", "")).strip()
        participants = row.get("Participants", "")
        participant_list = [p.strip() for p in str(participants).split(",") if p.strip()]
        try:
            amount = float(row.get("Amount", 0))
        except:
            amount = 0
        share = amount / len(participant_list) if participant_list else 0
        net_balance[payer] += amount
        for p in participant_list:
            net_balance[p] -= share
    return net_balance

def adjust_for_payments(net_balance, payments_df):
    """
    Adjust the net balances using payment records.
    For each payment:
      - The payer's balance increases by the payment amount (i.e. they reduce their debt).
      - The payee's balance decreases by the payment amount.
    """
    for _, row in payments_df.iterrows():
        try:
            amount = float(row.get("Amount", 0))
        except:
            amount = 0
        payer = str(row.get("Payer", "")).strip()   # person making the payment
        payee = str(row.get("Payee", "")).strip()     # person receiving the payment
        if payer:
            net_balance[payer] += amount
        if payee:
            net_balance[payee] -= amount
    return net_balance

def settle_debts(net_balance):
    """
    Given net balances, generate a list of transactions to settle debts.
    Returns a list of tuples: (Debtor, Creditor, Amount)
    """
    creditors = [(p, amt) for p, amt in net_balance.items() if amt > 1e-9]
    debtors = [(p, amt) for p, amt in net_balance.items() if amt < -1e-9]
    creditors.sort(key=lambda x: x[1], reverse=True)
    debtors.sort(key=lambda x: x[1])
    transactions = []
    i, j = 0, 0
    while i < len(debtors) and j < len(creditors):
        debtor, d_amt = debtors[i]
        creditor, c_amt = creditors[j]
        settlement = min(c_amt, -d_amt)
        transactions.append((debtor, creditor, settlement))
        d_amt += settlement
        c_amt -= settlement
        debtors[i] = (debtor, d_amt)
        creditors[j] = (creditor, c_amt)
        if abs(d_amt) < 1e-9:
            i += 1
        if abs(c_amt) < 1e-9:
            j += 1
    return transactions

# -----------------------------
# Display Charts (Always Visible)
# -----------------------------
st.title("Bill Splitting & Payment Settlement")
st.write("Below are the expense and payment records")

# Load data from two worksheets:
df_expenses = load_google_sheet(SHEET_URL, worksheet_index=0)  # Expense records from Sheet1
df_payments  = load_google_sheet(SHEET_URL, worksheet_index=1)  # Payment records from Sheet2

# Create a display copy for expenses: reformat the Percentages column.
df_exp_disp = df_expenses.copy()
if "Percentages" in df_exp_disp.columns:
    df_exp_disp["Percentages"] = df_exp_disp.apply(lambda row: format_percentages(row.get("Percentages", ""), row.get("Split Type", "Equal")), axis=1)

st.subheader("All Expense Records")
if df_exp_disp.empty:
    st.write("No expense records found.")
else:
    st.dataframe(df_exp_disp, use_container_width=True, hide_index=True)

st.subheader("All Payment Records")
if df_payments.empty:
    st.write("No payment records found.")
else:
    st.dataframe(df_payments, use_container_width=True, hide_index=True)

# Compute Settlement:
net_balance = compute_expense_balances(df_expenses)
net_balance = adjust_for_payments(net_balance, df_payments)
transactions = settle_debts(net_balance)
st.subheader("Settlement Chart")
if transactions:
    settlement_df = pd.DataFrame(transactions, columns=["Debtor", "Creditor", "Amount"])
    st.dataframe(settlement_df, hide_index=True)
else:
    st.write("No settlement needed! Everyone is even.")

# -----------------------------
# Expense Entry UI (Restored to Original Expense UI)
# -----------------------------
st.subheader("Enter a New Expense")
exp_date = st.date_input("Date", value=datetime.date.today(), key="exp_date")
exp_payer = st.text_input("Payer", key="exp_payer")
exp_amount = st.number_input("Amount", min_value=0.01, format="%.2f", step=0.01, key="exp_amount")
exp_participants = st.text_area("Participants (comma-separated)", key="exp_participants")
exp_part_list = [p.strip() for p in exp_participants.split(",") if p.strip()]
exp_split_type = st.selectbox("Split Type", ["Equal", "Custom"], key="exp_split_type")
if exp_split_type == "Custom" and exp_part_list:
    st.write("Enter custom percentages for each participant (they must sum to 100):")
    exp_percentages = {}
    for participant in exp_part_list:
        default_pct = st.session_state.get(f"exp_pct_{participant}", 0.0)
        exp_percentages[participant] = st.number_input(
            f"{participant}'s Percentage (%)", min_value=0.0, max_value=100.0,
            value=default_pct, step=0.1, key=f"exp_pct_{participant}")
else:
    exp_percentages = {}
exp_notes = st.text_input("Notes", key="exp_notes")
submit_expense = st.button("Add Expense")

if submit_expense:
    exp_error = ""
    if not exp_payer:
        exp_error = "Please enter the payer's name."
    elif exp_amount <= 0:
        exp_error = "Amount must be greater than 0."
    elif not exp_part_list:
        exp_error = "Please enter at least one participant."
    elif exp_split_type == "Custom" and abs(sum(exp_percentages.values()) - 100.0) > 0.01:
        exp_error = "For custom splits, the total percentage must equal 100%."
    if exp_error:
        st.error(exp_error)
    else:
        new_expense = {
            "Record Type": "Expense",  # (Not used further here, but you could include it)
            "Date": exp_date.strftime("%m/%d/%Y"),
            "Payer": exp_payer,
            "Amount": exp_amount,
            "Participants": exp_participants,
            "Split Type": exp_split_type,
            "Percentages": exp_percentages if exp_split_type == "Custom" else "",
            "Notes": exp_notes
        }
        append_to_google_sheet(SHEET_URL, new_expense, worksheet_index=0)
        st.success("Expense added successfully!")
        # Clear session state keys for expense UI.
        for key in ["exp_date", "exp_payer", "exp_amount", "exp_participants", "exp_split_type", "exp_notes"]:
            st.session_state.pop(key, None)
        for participant in exp_part_list:
            st.session_state.pop(f"exp_pct_{participant}", None)
        st.rerun()

# -----------------------------
# Payment Entry UI
# -----------------------------
st.subheader("Enter a New Payment")
pay_date = st.date_input("Payment Date", value=datetime.date.today(), key="pay_date")
pay_payer = st.text_input("Payer (who pays)", key="pay_payer")
pay_amount = st.number_input("Amount", min_value=0.01, format="%.2f", step=0.01, key="pay_amount")
pay_payee = st.text_input("Payee (who receives)", key="pay_payee")
pay_notes = st.text_input("Notes", key="pay_notes")
submit_payment = st.button("Add Payment")

if submit_payment:
    pay_error = ""
    if not pay_payer:
        pay_error = "Please enter the payer's name."
    elif not pay_payee:
        pay_error = "Please enter the payee's name."
    elif pay_amount <= 0:
        pay_error = "Amount must be greater than 0."
    if pay_error:
        st.error(pay_error)
    else:
        new_payment = {
            "Date": pay_date.strftime("%m/%d/%Y"),
            "Payer": pay_payer,
            "Amount": pay_amount,
            "Payee": pay_payee,
            "Notes": pay_notes
        }
        append_to_google_sheet(SHEET_URL, new_payment, worksheet_index=1)
        st.success("Payment added successfully!")
        for key in ["pay_date", "pay_payer", "pay_amount", "pay_payee", "pay_notes"]:
            st.session_state.pop(key, None)
        st.rerun()
