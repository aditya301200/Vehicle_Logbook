import os
import sys
import typing
import warnings
import sqlite3
import psycopg2
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from datetime import date, datetime, timedelta

# Prevent Streamlit hot-reload module eviction issues in Python 3.11/3.12+
if "warnings" not in sys.modules:
    sys.modules["warnings"] = warnings
if "typing" not in sys.modules:
    sys.modules["typing"] = typing

# ---------------- PAGE CONFIG ----------------
st.set_page_config(
    page_title="Garage & Fuel Logbook",
    page_icon="🏍️",
    layout="wide",
    initial_sidebar_state="collapsed"
)

# Mobile-friendly compact CSS
st.markdown("""
    <style>
    .block-container { padding-top: 1.2rem; padding-bottom: 2rem; }
    div[data-testid="stMetricValue"] { font-size: 1.35rem; }
    .stButton>button { width: 100%; border-radius: 8px; font-weight: 600; }
    </style>
""", unsafe_allow_html=True)

LOCAL_DB_FILE = "vehicle_logbook.db"
RECYCLE_BIN_RETENTION_DAYS = 3
FP_RUPEE_VALUE = 0.90  # 1 Fuel Point = ₹0.90

# ---------------- CACHED CLOUD POSTGRES / SQLITE ENGINE ----------------
@st.cache_resource
def get_db_url():
    """Fetches DATABASE_URL once and caches it."""
    try:
        if "DATABASE_URL" in st.secrets:
            return st.secrets["DATABASE_URL"]
    except Exception:
        pass
    return os.environ.get("DATABASE_URL", None)

def is_cloud_db():
    url = get_db_url()
    return bool(url and url.startswith("postgres"))

@st.cache_resource
def get_cached_connection():
    """Maintains a single persistent database connection across clicks."""
    if is_cloud_db():
        return psycopg2.connect(get_db_url())
    return sqlite3.connect(LOCAL_DB_FILE, check_same_thread=False)

def get_connection():
    """Returns the cached connection, reconnecting automatically if dropped by cloud provider."""
    conn = get_cached_connection()
    if is_cloud_db():
        try:
            if conn.closed != 0:
                get_cached_connection.clear()
                conn = get_cached_connection()
            else:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
        except Exception:
            get_cached_connection.clear()
            conn = get_cached_connection()
    return conn

@st.cache_data(ttl=300, show_spinner=False)
def load_data(table_name, include_deleted=False):
    """Fetches table records and caches them in RAM. Cleared automatically on any DB write."""
    conn = get_connection()
    cur = conn.cursor()
    try:
        if include_deleted:
            query = f"SELECT * FROM {table_name} WHERE is_deleted = 1"
        else:
            query = f"SELECT * FROM {table_name} WHERE COALESCE(is_deleted, 0) = 0"
        cur.execute(query)
        rows = cur.fetchall()
        cols = [desc[0] for desc in cur.description] if cur.description else []
        if not rows:
            return pd.DataFrame(columns=cols)
        return pd.DataFrame(list(rows), columns=cols)
    except Exception:
        if is_cloud_db():
            conn.rollback()
        return pd.DataFrame()
    finally:
        cur.close()

def execute_query(query, params=(), clear_cache=True):
    """Executes INSERT/UPDATE/DELETE using the persistent connection and invalidates cache."""
    conn = get_connection()
    cur = conn.cursor()
    try:
        if is_cloud_db():
            query = query.replace("?", "%s")
        cur.execute(query, params)
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        cur.close()
    if clear_cache:
        load_data.clear()

def ensure_column_exists(table_name, column_name, column_def):
    """Safely adds new columns to existing Cloud PostgreSQL or Local SQLite tables."""
    conn = get_connection()
    cur = conn.cursor()
    try:
        if is_cloud_db():
            cur.execute(f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS {column_name} {column_def};")
        else:
            cur.execute(f"PRAGMA table_info({table_name});")
            existing_cols = [row[1] for row in cur.fetchall()]
            if column_name not in existing_cols:
                cur.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_def};")
        conn.commit()
    except Exception:
        conn.rollback()
    finally:
        cur.close()

VEHICLES = ["Aditya's Bike (Apache RTR 160 4V)", "Abhijit's Scooty"]
RIDERS = ["Aditya Das", "Abhijit Das"]
STATIONS = [
    "Dwaraka Service Station (IndianOil)",
    "Mira Service Station (IndianOil)",
    "Murliwala Filling Station",
    "Other IndianOil Outlet",
    "Other Pump (HP / BP / Shell)"
]
PAYMENT_METHODS = [
    "HDFC IndianOil Credit Card (5% Fuel Points)",
    "Flipkart Axis Credit Card",
    "UPI / Cash / Debit Card"
]
MAINTENANCE_CATEGORIES = [
    "Regular Servicing & Engine Oil",
    "Chain Clean & Lube",
    "Brake Pads / Shoes / Cable",
    "Tyre / Puncture / Air",
    "Battery / Electrical",
    "Insurance / PUC Renewal",
    "Washing & Cleaning",
    "Other Spare Parts"
]

def recalculate_vehicle_mileage_chain(vehicle_name, method="full_tank"):
    """
    Recalculates mileage_kmpl and fuel_cost_per_km for a vehicle:
    - 'full_tank' (Standard Full-Tank -> Red Blink -> Full-Tank):
      Today's top-up litres is the exact fuel consumed since the last full tank.
      KMPL = Today's Distance / Today's Litres.
    - 'reserve_prev' (Red Blink -> Partial Fill -> Red Blink):
      KMPL = Today's Distance / Previous Fill-up's Litres.
    """
    conn = get_connection()
    cur = conn.cursor()
    try:
        q = "SELECT id, distance_km, litres, total_cost FROM vehicle_fuel_logs WHERE vehicle = ? AND COALESCE(is_deleted, 0) = 0 ORDER BY log_date ASC, id ASC"
        if is_cloud_db():
            q = q.replace("?", "%s")
        cur.execute(q, (vehicle_name,))
        rows = cur.fetchall()

        prev_litres = None
        prev_cost = None

        for r in rows:
            rec_id = int(r[0])
            dist = float(r[1] or 0.0)
            curr_lit = float(r[2] or 0.0)
            curr_cost = float(r[3] or 0.0)
            ppl = round(curr_cost / curr_lit, 2) if curr_lit > 0 else 0.0

            if method == "reserve_prev" and prev_litres is not None and prev_litres > 0:
                used_litres = prev_litres
                used_cost = prev_cost if (prev_cost is not None and prev_cost > 0) else curr_cost
            else:
                used_litres = curr_lit
                used_cost = curr_cost

            mileage = round(dist / used_litres, 2) if (dist > 0 and used_litres > 0) else 0.0
            cpk = round(used_cost / dist, 2) if (dist > 0 and used_cost > 0) else 0.0

            upd_q = "UPDATE vehicle_fuel_logs SET price_per_litre = ?, mileage_kmpl = ?, fuel_cost_per_km = ? WHERE id = ?"
            if is_cloud_db():
                upd_q = upd_q.replace("?", "%s")
            cur.execute(upd_q, (ppl, mileage, cpk, rec_id))

            prev_litres = curr_lit
            prev_cost = curr_cost

        conn.commit()
    except Exception:
        conn.rollback()
    finally:
        cur.close()
    load_data.clear()

@st.cache_resource
def initialize_app_once():
    """Runs DB table creation, schema migration, and 3-day recycle bin purge ONLY ONCE per server start."""
    conn = get_connection()
    cur = conn.cursor()
    id_type = "SERIAL PRIMARY KEY" if is_cloud_db() else "INTEGER PRIMARY KEY AUTOINCREMENT"

    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS vehicle_fuel_logs (
            id {id_type},
            log_date TEXT NOT NULL,
            vehicle TEXT NOT NULL,
            rider TEXT NOT NULL,
            entry_mode TEXT NOT NULL,
            odo_before REAL,
            odo_after REAL,
            distance_km REAL NOT NULL,
            litres REAL NOT NULL,
            total_cost REAL NOT NULL,
            price_per_litre REAL NOT NULL,
            mileage_kmpl REAL NOT NULL,
            fuel_cost_per_km REAL NOT NULL,
            station TEXT,
            payment_method TEXT NOT NULL,
            fuel_points REAL DEFAULT 0,
            notes TEXT,
            is_deleted INTEGER DEFAULT 0,
            deleted_at TEXT
        )
    """)

    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS vehicle_maintenance_logs (
            id {id_type},
            service_date TEXT NOT NULL,
            vehicle TEXT NOT NULL,
            rider TEXT NOT NULL,
            category TEXT NOT NULL,
            odometer REAL,
            cost REAL NOT NULL,
            payment_method TEXT,
            notes TEXT,
            is_deleted INTEGER DEFAULT 0,
            deleted_at TEXT
        )
    """)
    conn.commit()
    cur.close()

    for tbl in ["vehicle_fuel_logs", "vehicle_maintenance_logs"]:
        ensure_column_exists(tbl, "is_deleted", "INTEGER DEFAULT 0")
        ensure_column_exists(tbl, "deleted_at", "TEXT")

    cutoff_str = (datetime.now() - timedelta(days=RECYCLE_BIN_RETENTION_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    for tbl in ["vehicle_fuel_logs", "vehicle_maintenance_logs"]:
        execute_query(
            f"DELETE FROM {tbl} WHERE is_deleted = 1 AND deleted_at IS NOT NULL AND deleted_at < ?",
            (cutoff_str,),
            clear_cache=False
        )

    for v in VEHICLES:
        recalculate_vehicle_mileage_chain(v, method="full_tank")

    return True

initialize_app_once()

# ---------------- SESSION STATE ----------------
if "last_deleted" not in st.session_state:
    st.session_state["last_deleted"] = None
if "fuel_form_version" not in st.session_state:
    st.session_state["fuel_form_version"] = 0
if "maint_form_version" not in st.session_state:
    st.session_state["maint_form_version"] = 0
if "kmpl_calc_mode" not in st.session_state:
    st.session_state["kmpl_calc_mode"] = "full_tank"

# ---------------- HELPER FUNCTIONS ----------------
def calculate_hdfc_fuel_points(cost, payment_method, station):
    """Calculates 5% Fuel Points on HDFC IndianOil Credit Card at IndianOil outlets."""
    if "HDFC IndianOil" in str(payment_method) and ("IndianOil" in str(station) or "Murliwala" in str(station)):
        return round(float(cost) * 0.05, 2)
    return 0.0

def soft_delete_record(table_name, record_id, label):
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    execute_query(
        f"UPDATE {table_name} SET is_deleted = 1, deleted_at = ? WHERE id = ?",
        (now_str, int(record_id))
    )
    if table_name == "vehicle_fuel_logs":
        for v in VEHICLES:
            recalculate_vehicle_mileage_chain(v, method=st.session_state["kmpl_calc_mode"])
    st.session_state["last_deleted"] = {
        "table": table_name,
        "id": int(record_id),
        "label": label
    }

def restore_record(table_name, record_id):
    execute_query(
        f"UPDATE {table_name} SET is_deleted = 0, deleted_at = NULL WHERE id = ?",
        (int(record_id),)
    )
    if table_name == "vehicle_fuel_logs":
        for v in VEHICLES:
            recalculate_vehicle_mileage_chain(v, method=st.session_state["kmpl_calc_mode"])
    if (
        st.session_state["last_deleted"]
        and st.session_state["last_deleted"]["table"] == table_name
        and st.session_state["last_deleted"]["id"] == int(record_id)
    ):
        st.session_state["last_deleted"] = None

def permanent_delete_record(table_name, record_id):
    execute_query(f"DELETE FROM {table_name} WHERE id = ?", (int(record_id),))
    if table_name == "vehicle_fuel_logs":
        for v in VEHICLES:
            recalculate_vehicle_mileage_chain(v, method=st.session_state["kmpl_calc_mode"])
    if (
        st.session_state["last_deleted"]
        and st.session_state["last_deleted"]["table"] == table_name
        and st.session_state["last_deleted"]["id"] == int(record_id)
    ):
        st.session_state["last_deleted"] = None

def format_time_remaining(deleted_at_str):
    """Returns human-readable time remaining before 3-day auto-purge."""
    try:
        deleted_dt = datetime.strptime(str(deleted_at_str), "%Y-%m-%d %H:%M:%S")
        expiry_dt = deleted_dt + timedelta(days=RECYCLE_BIN_RETENTION_DAYS)
        remaining = expiry_dt - datetime.now()
        total_hours = int(remaining.total_seconds() // 3600)
        if total_hours <= 0:
            return "Expiring soon"
        days = total_hours // 24
        hours = total_hours % 24
        return f"{days}d {hours}h left"
    except Exception:
        return "3d left"

# ---------------- POP-UP DIALOGS FOR DELETE & MODIFY ----------------
@st.dialog("⚠️ Confirm Action")
def confirm_action_dialog(action_type, title_text, details_md, payload):
    """Pop-up modal before any delete or bulk restore action."""
    st.markdown(f"### {title_text}")
    st.markdown(details_md)
    st.warning("Are you sure you want to proceed?")

    c1, c2 = st.columns(2)
    with c1:
        if st.button("✅ Yes, Confirm", type="primary", use_container_width=True):
            if action_type == "soft_delete":
                soft_delete_record(payload["table"], payload["id"], payload["label"])
                st.toast(f"Moved {payload['label']} to Recycle Bin.", icon="🗑️")
            elif action_type == "perm_delete":
                permanent_delete_record(payload["table"], payload["id"])
                st.toast(f"Permanently deleted ID #{payload['id']}.", icon="🔥")
            elif action_type == "empty_bin":
                execute_query("DELETE FROM vehicle_fuel_logs WHERE is_deleted = 1")
                execute_query("DELETE FROM vehicle_maintenance_logs WHERE is_deleted = 1")
                st.session_state["last_deleted"] = None
                st.toast("Recycle Bin permanently emptied!", icon="🔥")
            elif action_type == "restore_all":
                execute_query("UPDATE vehicle_fuel_logs SET is_deleted = 0, deleted_at = NULL WHERE is_deleted = 1")
                execute_query("UPDATE vehicle_maintenance_logs SET is_deleted = 0, deleted_at = NULL WHERE is_deleted = 1")
                for v in VEHICLES:
                    recalculate_vehicle_mileage_chain(v, method=st.session_state["kmpl_calc_mode"])
                st.session_state["last_deleted"] = None
                st.toast("All items restored!", icon="✅")
            st.rerun()
    with c2:
        if st.button("✖️ Cancel", use_container_width=True):
            st.rerun()

@st.dialog("✏️ Modify Fuel Entry", width="large")
def edit_fuel_dialog(row_dict):
    """Pop-up modal form to edit a Fuel Entry with a 2-step confirmation check."""
    entry_id = int(row_dict["id"])
    confirm_key = f"confirm_fuel_payload_{entry_id}"

    if st.session_state.get(confirm_key) is None:
        with st.form(f"popup_edit_fuel_form_{entry_id}"):
            ef1, ef2, ef3 = st.columns(3)
            with ef1:
                edit_f_veh = st.selectbox(
                    "Vehicle", VEHICLES,
                    index=VEHICLES.index(row_dict["vehicle"]) if row_dict["vehicle"] in VEHICLES else 0
                )
            with ef2:
                edit_f_rider = st.selectbox(
                    "Rider", RIDERS,
                    index=RIDERS.index(row_dict["rider"]) if row_dict["rider"] in RIDERS else 0
                )
            with ef3:
                edit_f_date = st.date_input(
                    "Fill-up Date",
                    value=pd.to_datetime(row_dict["log_date"]).date()
                )

            ef4, ef5, ef6, ef7 = st.columns(4)
            with ef4:
                edit_f_dist = st.number_input("Distance (KM)", value=float(row_dict["distance_km"] or 0.1), min_value=0.1, step=0.1)
            with ef5:
                edit_f_litres = st.number_input("Litres Filled", value=float(row_dict["litres"] or 0.01), min_value=0.01, step=0.01)
            with ef6:
                edit_f_cost = st.number_input("Total Cost Paid (₹)", value=float(row_dict["total_cost"] or 1.0), min_value=1.0, step=1.0)
            with ef7:
                edit_f_odo_after = st.number_input("Odometer After (KM)", value=float(row_dict["odo_after"] or 0.0), min_value=0.0, step=1.0)

            ef8, ef9 = st.columns(2)
            with ef8:
                edit_f_station = st.selectbox(
                    "Station", STATIONS,
                    index=STATIONS.index(row_dict["station"]) if row_dict["station"] in STATIONS else 0
                )
            with ef9:
                edit_f_pay = st.selectbox(
                    "Payment Method", PAYMENT_METHODS,
                    index=PAYMENT_METHODS.index(row_dict["payment_method"]) if row_dict["payment_method"] in PAYMENT_METHODS else 0
                )

            edit_f_notes = st.text_input("Notes", value=str(row_dict["notes"] or ""))

            btn_c1, btn_c2 = st.columns(2)
            with btn_c1:
                review_clicked = st.form_submit_button("💾 Review & Save Changes", type="primary", use_container_width=True)
            with btn_c2:
                cancel_clicked = st.form_submit_button("✖️ Cancel", use_container_width=True)

            if cancel_clicked:
                st.rerun()

            if review_clicked:
                new_ppl = round(edit_f_cost / edit_f_litres, 2)
                new_fp = calculate_hdfc_fuel_points(edit_f_cost, edit_f_pay, edit_f_station)

                st.session_state[confirm_key] = {
                    "id": entry_id,
                    "log_date": str(edit_f_date),
                    "vehicle": edit_f_veh,
                    "rider": edit_f_rider,
                    "odo_before": float(row_dict["odo_before"] or 0.0),
                    "odo_after": edit_f_odo_after,
                    "distance_km": edit_f_dist,
                    "litres": edit_f_litres,
                    "total_cost": edit_f_cost,
                    "price_per_litre": new_ppl,
                    "station": edit_f_station,
                    "payment_method": edit_f_pay,
                    "fuel_points": new_fp,
                    "notes": edit_f_notes
                }
                st.rerun(scope="fragment")

    else:
        p = st.session_state[confirm_key]
        preview_kmpl = round(p["distance_km"] / p["litres"], 2)
        st.warning(f"⚠️ Please confirm your modifications for **Fuel Entry #{entry_id}**:")
        st.markdown(
            f"- **Date:** {row_dict['log_date']} → `{p['log_date']}`\n"
            f"- **Vehicle / Rider:** {row_dict['vehicle']} ({row_dict['rider']}) → `{p['vehicle']} ({p['rider']})`\n"
            f"- **Distance & Litres:** {row_dict['distance_km']} KM / {row_dict['litres']} L → `{p['distance_km']} KM / {p['litres']} L` (**Full-Tank Mileage:** `{preview_kmpl} km/L`)\n"
            f"- **Total Cost:** ₹{row_dict['total_cost']} → `₹{p['total_cost']}` (**Points:** `{p['fuel_points']} FP ≈ ₹{round(p['fuel_points']*FP_RUPEE_VALUE, 2)}`)\n"
            f"- **Station & Payment:** {row_dict['station']} | `{p['station']}` ({p['payment_method']})\n"
            f"- **Notes:** `{p['notes']}`"
        )
        c1, c2 = st.columns(2)
        with c1:
            if st.button("✅ Yes, Save Changes", type="primary", use_container_width=True):
                execute_query("""
                    UPDATE vehicle_fuel_logs
                    SET log_date = ?, vehicle = ?, rider = ?, odo_before = ?, odo_after = ?,
                        distance_km = ?, litres = ?, total_cost = ?, price_per_litre = ?,
                        station = ?, payment_method = ?, fuel_points = ?, notes = ?
                    WHERE id = ?
                """, (
                    p["log_date"], p["vehicle"], p["rider"],
                    p["odo_before"], p["odo_after"], p["distance_km"],
                    p["litres"], p["total_cost"], p["price_per_litre"],
                    p["station"], p["payment_method"], p["fuel_points"], p["notes"],
                    int(p["id"])
                ))
                for v in VEHICLES:
                    recalculate_vehicle_mileage_chain(v, method=st.session_state["kmpl_calc_mode"])
                st.session_state.pop(confirm_key, None)
                st.toast(f"Updated Fuel Entry #{entry_id}!", icon="✅")
                st.rerun()
        with c2:
            if st.button("⬅️ Back to Edit Form", use_container_width=True):
                st.session_state.pop(confirm_key, None)
                st.rerun(scope="fragment")

@st.dialog("✏️ Modify Maintenance Entry", width="large")
def edit_maint_dialog(row_dict):
    """Pop-up modal form to edit a Maintenance Entry with a 2-step confirmation check."""
    entry_id = int(row_dict["id"])
    confirm_key = f"confirm_maint_payload_{entry_id}"

    if st.session_state.get(confirm_key) is None:
        with st.form(f"popup_edit_maint_form_{entry_id}"):
            em1, em2, em3 = st.columns(3)
            with em1:
                edit_m_veh = st.selectbox(
                    "Vehicle", VEHICLES,
                    index=VEHICLES.index(row_dict["vehicle"]) if row_dict["vehicle"] in VEHICLES else 0
                )
            with em2:
                edit_m_rider = st.selectbox(
                    "Rider", RIDERS,
                    index=RIDERS.index(row_dict["rider"]) if row_dict["rider"] in RIDERS else 0
                )
            with em3:
                edit_m_date = st.date_input(
                    "Service Date",
                    value=pd.to_datetime(row_dict["service_date"]).date()
                )

            em4, em5, em6 = st.columns(3)
            with em4:
                edit_m_cat = st.selectbox(
                    "Category", MAINTENANCE_CATEGORIES,
                    index=MAINTENANCE_CATEGORIES.index(row_dict["category"]) if row_dict["category"] in MAINTENANCE_CATEGORIES else 0
                )
            with em5:
                edit_m_cost = st.number_input("Cost (₹)", value=float(row_dict["cost"] or 1.0), min_value=1.0, step=10.0)
            with em6:
                edit_m_odo = st.number_input("Odometer (KM)", value=float(row_dict["odometer"] or 0.0), min_value=0.0, step=10.0)

            em7, em8 = st.columns([1, 2])
            with em7:
                edit_m_pay = st.selectbox(
                    "Payment Mode", PAYMENT_METHODS,
                    index=PAYMENT_METHODS.index(row_dict["payment_method"]) if row_dict["payment_method"] in PAYMENT_METHODS else 2
                )
            with em8:
                edit_m_notes = st.text_input("Notes", value=str(row_dict["notes"] or ""))

            btn_m1, btn_m2 = st.columns(2)
            with btn_m1:
                review_m_clicked = st.form_submit_button("💾 Review & Save Changes", type="primary", use_container_width=True)
            with btn_m2:
                cancel_m_clicked = st.form_submit_button("✖️ Cancel", use_container_width=True)

            if cancel_m_clicked:
                st.rerun()

            if review_m_clicked:
                st.session_state[confirm_key] = {
                    "id": entry_id,
                    "service_date": str(edit_m_date),
                    "vehicle": edit_m_veh,
                    "rider": edit_m_rider,
                    "category": edit_m_cat,
                    "odometer": edit_m_odo,
                    "cost": edit_m_cost,
                    "payment_method": edit_m_pay,
                    "notes": edit_m_notes
                }
                st.rerun(scope="fragment")

    else:
        p = st.session_state[confirm_key]
        st.warning(f"⚠️ Please confirm your modifications for **Maintenance Entry #{entry_id}**:")
        st.markdown(
            f"- **Service Date:** {row_dict['service_date']} → `{p['service_date']}`\n"
            f"- **Vehicle / Rider:** {row_dict['vehicle']} ({row_dict['rider']}) → `{p['vehicle']} ({p['rider']})`\n"
            f"- **Category:** {row_dict['category']} → `{p['category']}`\n"
            f"- **Cost & Odometer:** ₹{row_dict['cost']} ({row_dict['odometer']} KM) → `₹{p['cost']} ({p['odometer']} KM)`\n"
            f"- **Notes:** `{p['notes']}`"
        )
        c1, c2 = st.columns(2)
        with c1:
            if st.button("✅ Yes, Save Changes", type="primary", use_container_width=True):
                execute_query("""
                    UPDATE vehicle_maintenance_logs
                    SET service_date = ?, vehicle = ?, rider = ?, category = ?,
                        odometer = ?, cost = ?, payment_method = ?, notes = ?
                    WHERE id = ?
                """, (
                    p["service_date"], p["vehicle"], p["rider"],
                    p["category"], p["odometer"], p["cost"],
                    p["payment_method"], p["notes"], int(p["id"])
                ))
                st.session_state.pop(confirm_key, None)
                st.toast(f"Updated Maintenance Entry #{entry_id}!", icon="✅")
                st.rerun()
        with c2:
            if st.button("⬅️ Back to Edit Form", use_container_width=True):
                st.session_state.pop(confirm_key, None)
                st.rerun(scope="fragment")

# ---------------- HEADER ----------------
col_title, col_status = st.columns([4, 1])
with col_title:
    st.title("🏍️ Vehicle Fuel, Mileage & Service Logbook")
    st.caption("Full-Tank → Red Blink → Full-Tank Mileage Tracker, Monthly Running Costs, and HDFC IndianOil 5% Fuel Points (1 FP = ₹0.90).")
with col_status:
    if is_cloud_db():
        st.success("☁️ Cloud PostgreSQL Active")
    else:
        st.warning("💻 Local SQLite Mode")

# Quick Undo Banner if an item was just deleted in this session
if st.session_state["last_deleted"]:
    last_del = st.session_state["last_deleted"]
    u_col1, u_col2, u_col3 = st.columns([6, 2, 1])
    with u_col1:
        st.warning(f"🗑️ Moved **{last_del['label']}** to Recycle Bin (auto-clears in 3 days).")
    with u_col2:
        if st.button("↩️ Undo Last Delete", type="primary", key="quick_undo_btn"):
            restore_record(last_del["table"], last_del["id"])
            st.toast(f"Restored {last_del['label']}!", icon="✅")
            st.rerun()
    with u_col3:
        if st.button("✖️ Dismiss", key="dismiss_undo_btn"):
            st.session_state["last_deleted"] = None
            st.rerun()

# Fetch cached datasets once at top level
active_fuel_df = load_data("vehicle_fuel_logs", include_deleted=False)
active_maint_df = load_data("vehicle_maintenance_logs", include_deleted=False)
del_fuel_df = load_data("vehicle_fuel_logs", include_deleted=True)
del_maint_df = load_data("vehicle_maintenance_logs", include_deleted=True)
bin_count = len(del_fuel_df) + len(del_maint_df)

tabs = st.tabs([
    "⛽ Log Fuel",
    "🔧 Log Maintenance",
    "📊 Monthly Dashboard",
    "💳 HDFC Fuel Points",
    "🗂️ History, Search & Manage",
    f"🗑️ Recycle Bin ({bin_count})"
])

# =====================================================================
# TAB 1: LOG FUEL (Full-Tank-to-Full-Tank Default + Auto-Reset)
# =====================================================================
@st.fragment
def render_log_fuel_tab(fuel_df_cached):
    st.subheader("⛽ New Petrol Fill-Up Entry")

    fv = st.session_state["fuel_form_version"]
    veh_key = f"fuel_veh_{fv}"
    rider_key = f"fuel_rider_{fv}"
    mode_key = f"fuel_mode_{fv}"

    def on_fuel_vehicle_change():
        selected_v = st.session_state[veh_key]
        if "Aditya" in selected_v:
            st.session_state[rider_key] = "Aditya Das"
            st.session_state[mode_key] = "Trip B Reset (Direct KMs Driven)"
        else:
            st.session_state[rider_key] = "Abhijit Das"
            st.session_state[mode_key] = "Odometer Before & After Fill-up (Scooty Mode)"

    col_v1, col_v2, col_v3 = st.columns(3)
    with col_v1:
        vehicle = st.selectbox(
            "Select Vehicle",
            VEHICLES,
            index=0,
            key=veh_key,
            on_change=on_fuel_vehicle_change
        )
    with col_v2:
        if rider_key not in st.session_state:
            st.session_state[rider_key] = "Aditya Das" if "Aditya" in vehicle else "Abhijit Das"
        rider = st.selectbox("Filled By", RIDERS, key=rider_key)
    with col_v3:
        log_date = st.date_input("Date of Fill-up", value=date.today(), key=f"fuel_date_{fv}")

    st.divider()

    # Fetch Previous Fill-up for reference
    prev_litres = None
    prev_cost = None
    prev_date = None
    last_odo = 0.0

    if not fuel_df_cached.empty:
        veh_logs = fuel_df_cached[fuel_df_cached["vehicle"] == vehicle].sort_values(["log_date", "id"])
        if not veh_logs.empty:
            last_row = veh_logs.iloc[-1]
            prev_litres = float(last_row["litres"]) if pd.notna(last_row["litres"]) and float(last_row["litres"]) > 0 else None
            prev_cost = float(last_row["total_cost"]) if pd.notna(last_row["total_cost"]) and float(last_row["total_cost"]) > 0 else None
            prev_date = str(last_row["log_date"])
            if pd.notna(last_row["odo_after"]) and float(last_row["odo_after"]) > 0:
                last_odo = float(last_row["odo_after"])

    # Mileage Formula Selector (Defaults to Full-Tank -> Red Blink -> Full-Tank)
    m_col1, m_col2 = st.columns([1, 1])
    with m_col1:
        if mode_key not in st.session_state:
            st.session_state[mode_key] = (
                "Trip B Reset (Direct KMs Driven)" if "Bike" in vehicle else "Odometer Before & After Fill-up (Scooty Mode)"
            )
        entry_mode = st.radio(
            "Distance Input Method",
            ["Trip B Reset (Direct KMs Driven)", "Odometer Before & After Fill-up (Scooty Mode)"],
            horizontal=True,
            key=mode_key
        )
    with m_col2:
        calc_choice = st.radio(
            "Mileage (KM/L) Formula",
            [
                "Full-Tank → Red Blink → Full-Tank (Uses Today's Top-Up Litres)",
                "Partial Fill Reserve-to-Reserve (Uses Previous Fill-Up Litres)"
            ],
            index=0 if st.session_state["kmpl_calc_mode"] == "full_tank" else 1,
            horizontal=False,
            help="When you fill a Full Tank, reset Trip 0, run until Red Blink, and fill to the top again, the litres entering the tank TODAY is the exact amount of fuel burned during Trip B."
        )
        new_calc_mode = "full_tank" if "Full-Tank" in calc_choice else "reserve_prev"
        if new_calc_mode != st.session_state["kmpl_calc_mode"]:
            st.session_state["kmpl_calc_mode"] = new_calc_mode
            for v in VEHICLES:
                recalculate_vehicle_mileage_chain(v, method=new_calc_mode)
            st.rerun()

    col_d1, col_d2, col_d3 = st.columns(3)
    odo_before, odo_after, distance_km = 0.0, 0.0, 0.0

    if entry_mode == "Trip B Reset (Direct KMs Driven)":
        with col_d1:
            distance_km = st.number_input("Trip B Distance Driven (KM)", min_value=0.0, value=0.0, step=0.1, format="%.1f", key=f"dist_{fv}")
        with col_d2:
            odo_after = st.number_input("Current Odometer KM (Optional)", min_value=0.0, value=0.0, step=1.0, format="%.1f", key=f"odo_opt_{fv}")
    else:
        with col_d1:
            odo_before = st.number_input("KMs Before Fill-up (Previous Odo)", value=last_odo, min_value=0.0, step=0.1, format="%.1f", key=f"odo_bef_{fv}")
        with col_d2:
            odo_after = st.number_input("KMs At Current Fill-up (Current Odo)", value=0.0, min_value=0.0, step=0.1, format="%.1f", key=f"odo_aft_{fv}")
        with col_d3:
            distance_km = max(0.0, round(odo_after - odo_before, 1))
            st.metric("Calculated Distance Driven", f"{distance_km:.1f} KM")

    col_f1, col_f2, col_f3, col_f4 = st.columns(4)
    with col_f1:
        litres = st.number_input("Petrol Filled Today to Full Tank (Litres)", min_value=0.0, value=0.0, step=0.01, format="%.2f", key=f"lit_{fv}")
    with col_f2:
        total_cost = st.number_input("Total Cost Paid Today (₹)", min_value=0.0, value=0.0, step=1.0, format="%.2f", key=f"cost_{fv}")
    with col_f3:
        station = st.selectbox("Filling Station", STATIONS, index=0, key=f"station_{fv}")
    with col_f4:
        payment_method = st.selectbox("Payment Method", PAYMENT_METHODS, index=0, key=f"pay_{fv}")

    notes = st.text_input("Notes (Optional - e.g., Full tank after red indicator blink, Trip B reset to 0)", value="", key=f"notes_{fv}")

    # Live Preview Calculation
    if distance_km > 0 and litres > 0 and total_cost > 0:
        if st.session_state["kmpl_calc_mode"] == "reserve_prev" and prev_litres is not None:
            used_litres_for_kmpl = prev_litres
            used_cost_for_cpk = prev_cost if prev_cost is not None else total_cost
            basis_tag = f"Reserve-to-Reserve: based on prev fill of {prev_litres:.2f} L on {prev_date}"
        else:
            used_litres_for_kmpl = litres
            used_cost_for_cpk = total_cost
            basis_tag = f"Full-Tank Top-Up: {distance_km:.1f} KM ÷ {litres:.2f} L refilled today"

        live_mileage = round(distance_km / used_litres_for_kmpl, 2)
        live_ppl = round(total_cost / litres, 2)
        live_cpk = round(used_cost_for_cpk / distance_km, 2)
        live_points = calculate_hdfc_fuel_points(total_cost, payment_method, station)
        live_fp_rupees = round(live_points * FP_RUPEE_VALUE, 2)

        st.info(
            f"**Live Calculation:** Mileage: **{live_mileage} km/L** *({basis_tag})* | "
            f"Fuel Rate: **₹{live_ppl}/L** | "
            f"Fuel Cost/KM: **₹{live_cpk}/km** | "
            f"HDFC 5% Fuel Points: **{live_points} FP (≈ ₹{live_fp_rupees})**"
        )

    if st.button("💾 Save Fuel Entry", type="primary", key=f"save_fuel_btn_{fv}"):
        if distance_km <= 0 or litres <= 0 or total_cost <= 0:
            st.error("Please enter valid positive numbers for Distance (KM), Litres, and Total Cost.")
        else:
            if st.session_state["kmpl_calc_mode"] == "reserve_prev" and prev_litres is not None:
                used_litres_for_kmpl = prev_litres
                used_cost_for_cpk = prev_cost if prev_cost is not None else total_cost
            else:
                used_litres_for_kmpl = litres
                used_cost_for_cpk = total_cost

            mileage_kmpl = round(distance_km / used_litres_for_kmpl, 2)
            price_per_litre = round(total_cost / litres, 2)
            fuel_cost_per_km = round(used_cost_for_cpk / distance_km, 2)
            fuel_points = calculate_hdfc_fuel_points(total_cost, payment_method, station)

            execute_query("""
                INSERT INTO vehicle_fuel_logs (
                    log_date, vehicle, rider, entry_mode, odo_before, odo_after,
                    distance_km, litres, total_cost, price_per_litre, mileage_kmpl,
                    fuel_cost_per_km, station, payment_method, fuel_points, notes, is_deleted
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            """, (
                str(log_date), vehicle, rider, entry_mode, odo_before, odo_after,
                distance_km, litres, total_cost, price_per_litre, mileage_kmpl,
                fuel_cost_per_km, station, payment_method, fuel_points, notes
            ))
            recalculate_vehicle_mileage_chain(vehicle, method=st.session_state["kmpl_calc_mode"])

            # Reset all inputs to clean defaults for the next entry
            st.session_state["fuel_form_version"] += 1
            st.toast(
                f"Saved! {vehicle} achieved {mileage_kmpl} km/L ({distance_km} KM on {used_litres_for_kmpl} L). Form reset!",
                icon="✅"
            )
            st.rerun()

with tabs[0]:
    render_log_fuel_tab(active_fuel_df)

# =====================================================================
# TAB 2: LOG MAINTENANCE (Auto-sync Rider with Vehicle + Form Reset)
# =====================================================================
@st.fragment
def render_log_maint_tab():
    st.subheader("🔧 Add Maintenance / Service Cost")

    mv = st.session_state["maint_form_version"]
    m_veh_key = f"maint_veh_{mv}"
    m_rider_key = f"maint_rider_{mv}"

    def on_maint_vehicle_change():
        selected_mv = st.session_state[m_veh_key]
        st.session_state[m_rider_key] = "Aditya Das" if "Aditya" in selected_mv else "Abhijit Das"

    col_m1, col_m2, col_m3 = st.columns(3)
    with col_m1:
        m_vehicle = st.selectbox(
            "Select Vehicle",
            VEHICLES,
            index=0,
            key=m_veh_key,
            on_change=on_maint_vehicle_change
        )
    with col_m2:
        if m_rider_key not in st.session_state:
            st.session_state[m_rider_key] = "Aditya Das" if "Aditya" in m_vehicle else "Abhijit Das"
        m_rider = st.selectbox("Logged By", RIDERS, key=m_rider_key)
    with col_m3:
        m_date = st.date_input("Service Date", value=date.today(), key=f"maint_date_{mv}")

    col_m4, col_m5, col_m6 = st.columns(3)
    with col_m4:
        m_category = st.selectbox("Service Category", MAINTENANCE_CATEGORIES, key=f"maint_cat_{mv}")
    with col_m5:
        m_cost = st.number_input("Total Maintenance Cost (₹)", min_value=0.0, value=0.0, step=10.0, format="%.2f", key=f"maint_cost_{mv}")
    with col_m6:
        m_odo = st.number_input("Odometer Reading at Service (KM)", min_value=0.0, value=0.0, step=10.0, format="%.1f", key=f"maint_odo_{mv}")

    col_m7, col_m8 = st.columns([1, 2])
    with col_m7:
        m_payment = st.selectbox("Payment Mode", PAYMENT_METHODS, index=2, key=f"maint_pay_{mv}")
    with col_m8:
        m_notes = st.text_input("Service Details (e.g., Engine oil change, brake pad replacement, chain lube)", value="", key=f"maint_notes_{mv}")

    if st.button("🛠️ Save Maintenance Entry", type="primary", key=f"save_maint_btn_{mv}"):
        if m_cost <= 0:
            st.error("Please enter a valid maintenance cost greater than ₹0.")
        else:
            execute_query("""
                INSERT INTO vehicle_maintenance_logs (
                    service_date, vehicle, rider, category, odometer, cost, payment_method, notes, is_deleted
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
            """, (str(m_date), m_vehicle, m_rider, m_category, m_odo, m_cost, m_payment, m_notes))
            st.session_state["maint_form_version"] += 1
            st.toast(f"Logged ₹{m_cost:.2f} under '{m_category}' for {m_vehicle}!", icon="🛠️")
            st.rerun()

with tabs[1]:
    render_log_maint_tab()

# =====================================================================
# TAB 3: MONTHLY DASHBOARD & FILTERS (Isolated Fragment)
# =====================================================================
@st.fragment
def render_dashboard_tab(fuel_df, maint_df):
    st.subheader("📊 Monthly Distance, Fuel, Maintenance & True Running Cost")

    if fuel_df.empty and maint_df.empty:
        st.info("No active records logged yet. Add your first fuel fill-up or service record to unlock analytics!")
        return

    f_df = fuel_df.copy()
    m_df = maint_df.copy()

    all_months = set()
    if not f_df.empty:
        f_df["month"] = pd.to_datetime(f_df["log_date"]).dt.strftime("%Y-%m")
        all_months.update(f_df["month"].dropna().unique())
    if not m_df.empty:
        m_df["month"] = pd.to_datetime(m_df["service_date"]).dt.strftime("%Y-%m")
        all_months.update(m_df["month"].dropna().unique())
    sorted_months = sorted(list(all_months), reverse=True)

    with st.expander("🔍 Filter Monthly Dashboard (Vehicle, Rider, Month, Payment, Keyword)", expanded=True):
        df_c1, df_c2, df_c3, df_c4, df_c5 = st.columns(5)
        with df_c1:
            selected_veh = st.selectbox("Vehicle", ["All Vehicles"] + VEHICLES, key="dash_veh")
        with df_c2:
            selected_rider = st.selectbox("Rider", ["All Riders"] + RIDERS, key="dash_rider")
        with df_c3:
            selected_months = st.multiselect("Months (Empty = All)", options=sorted_months, default=[], key="dash_months")
        with df_c4:
            selected_pay = st.selectbox("Payment Mode", ["All Payment Modes"] + PAYMENT_METHODS, key="dash_pay")
        with df_c5:
            dash_search = st.text_input("Search Station / Category / Notes", key="dash_kw").strip()

    if not f_df.empty:
        if selected_veh != "All Vehicles":
            f_df = f_df[f_df["vehicle"] == selected_veh]
        if selected_rider != "All Riders":
            f_df = f_df[f_df["rider"] == selected_rider]
        if selected_months:
            f_df = f_df[f_df["month"].isin(selected_months)]
        if selected_pay != "All Payment Modes":
            f_df = f_df[f_df["payment_method"] == selected_pay]
        if dash_search:
            mask_f = (
                f_df["station"].astype(str).str.contains(dash_search, case=False, na=False)
                | f_df["notes"].astype(str).str.contains(dash_search, case=False, na=False)
                | f_df["vehicle"].astype(str).str.contains(dash_search, case=False, na=False)
            )
            f_df = f_df[mask_f]

    if not m_df.empty:
        if selected_veh != "All Vehicles":
            m_df = m_df[m_df["vehicle"] == selected_veh]
        if selected_rider != "All Riders":
            m_df = m_df[m_df["rider"] == selected_rider]
        if selected_months:
            m_df = m_df[m_df["month"].isin(selected_months)]
        if selected_pay != "All Payment Modes":
            m_df = m_df[m_df["payment_method"] == selected_pay]
        if dash_search:
            mask_m = (
                m_df["category"].astype(str).str.contains(dash_search, case=False, na=False)
                | m_df["notes"].astype(str).str.contains(dash_search, case=False, na=False)
                | m_df["vehicle"].astype(str).str.contains(dash_search, case=False, na=False)
            )
            m_df = m_df[mask_m]

    if not f_df.empty:
        for num_c in ["distance_km", "litres", "total_cost", "mileage_kmpl", "fuel_points"]:
            f_df[num_c] = pd.to_numeric(f_df[num_c], errors="coerce").fillna(0.0)
        monthly_fuel = f_df.groupby("month").agg(
            total_km=("distance_km", "sum"),
            total_litres=("litres", "sum"),
            fuel_cost=("total_cost", "sum"),
            avg_mileage=("mileage_kmpl", "mean"),
            fuel_points=("fuel_points", "sum")
        ).reset_index()
    else:
        monthly_fuel = pd.DataFrame(columns=["month", "total_km", "total_litres", "fuel_cost", "avg_mileage", "fuel_points"])

    if not m_df.empty:
        m_df["cost"] = pd.to_numeric(m_df["cost"], errors="coerce").fillna(0.0)
        monthly_maint = m_df.groupby("month").agg(
            maint_cost=("cost", "sum")
        ).reset_index()
    else:
        monthly_maint = pd.DataFrame(columns=["month", "maint_cost"])

    summary_df = pd.merge(monthly_fuel, monthly_maint, on="month", how="outer").fillna(0.0)

    if summary_df.empty:
        st.warning("No records match your current filter selection.")
    else:
        numeric_cols = ["total_km", "total_litres", "fuel_cost", "avg_mileage", "fuel_points", "maint_cost"]
        for col in numeric_cols:
            summary_df[col] = pd.to_numeric(summary_df[col], errors="coerce").fillna(0.0).astype(float)

        summary_df["month"] = summary_df["month"].astype(str)
        summary_df = summary_df.sort_values("month", ascending=False)

        summary_df["total_running_cost"] = summary_df["fuel_cost"] + summary_df["maint_cost"]
        summary_df["true_cost_per_km"] = summary_df.apply(
            lambda r: round(r["total_running_cost"] / r["total_km"], 2) if r["total_km"] > 0 else 0.0, axis=1
        )
        summary_df["fuel_only_cost_per_km"] = summary_df.apply(
            lambda r: round(r["fuel_cost"] / r["total_km"], 2) if r["total_km"] > 0 else 0.0, axis=1
        )

        tot_km = float(summary_df["total_km"].sum())
        tot_litres = float(summary_df["total_litres"].sum())
        tot_fuel_cost = float(summary_df["fuel_cost"].sum())
        tot_maint_cost = float(summary_df["maint_cost"].sum())
        tot_running = tot_fuel_cost + tot_maint_cost
        overall_mileage = round(tot_km / tot_litres, 2) if tot_litres > 0 else 0.0
        overall_cpk = round(tot_running / tot_km, 2) if tot_km > 0 else 0.0

        k1, k2, k3, k4, k5, k6 = st.columns(6)
        k1.metric("Total Distance", f"{tot_km:,.1f} KM")
        k2.metric("Total Petrol", f"{tot_litres:,.2f} L")
        k3.metric("True Overall Mileage", f"{overall_mileage} km/L")
        k4.metric("Total Fuel Cost", f"₹{tot_fuel_cost:,.0f}")
        k5.metric("Total Maintenance", f"₹{tot_maint_cost:,.0f}")
        k6.metric("True Running Cost/KM", f"₹{overall_cpk}/km")

        st.divider()

        st.markdown("#### 📅 Month-by-Month Breakdown")
        display_summary = summary_df.rename(columns={
            "month": "Month",
            "total_km": "Distance (KM)",
            "total_litres": "Petrol (L)",
            "avg_mileage": "Avg Mileage (km/L)",
            "fuel_cost": "Fuel Cost (₹)",
            "maint_cost": "Maintenance (₹)",
            "total_running_cost": "Total Running Cost (₹)",
            "fuel_only_cost_per_km": "Fuel Cost/KM (₹)",
            "true_cost_per_km": "True Running Cost/KM (₹)",
            "fuel_points": "5% Fuel Points Earned"
        })
        st.dataframe(
            display_summary[[
                "Month", "Distance (KM)", "Petrol (L)", "Avg Mileage (km/L)",
                "Fuel Cost (₹)", "Maintenance (₹)", "Total Running Cost (₹)",
                "Fuel Cost/KM (₹)", "True Running Cost/KM (₹)", "5% Fuel Points Earned"
            ]].style.format({
                "Distance (KM)": "{:.1f}",
                "Petrol (L)": "{:.2f}",
                "Avg Mileage (km/L)": "{:.2f}",
                "Fuel Cost (₹)": "₹{:,.2f}",
                "Maintenance (₹)": "₹{:,.2f}",
                "Total Running Cost (₹)": "₹{:,.2f}",
                "Fuel Cost/KM (₹)": "₹{:.2f}",
                "True Running Cost/KM (₹)": "₹{:.2f}",
                "5% Fuel Points Earned": "{:.2f} FP"
            }),
            use_container_width=True,
            hide_index=True
        )

        c_chart1, c_chart2 = st.columns(2)
        with c_chart1:
            plot_sum = summary_df.sort_values("month")
            fig_cost = go.Figure()
            fig_cost.add_trace(go.Bar(
                x=plot_sum["month"].tolist(),
                y=plot_sum["fuel_cost"].tolist(),
                name="Fuel Cost (₹)"
            ))
            fig_cost.add_trace(go.Bar(
                x=plot_sum["month"].tolist(),
                y=plot_sum["maint_cost"].tolist(),
                name="Maintenance Cost (₹)"
            ))
            fig_cost.update_layout(
                barmode="stack",
                title="Monthly Fuel vs. Maintenance Cost (₹)",
                xaxis_title="Month",
                yaxis_title="Amount (₹)",
                legend_title="Expense Type",
                margin=dict(l=20, r=20, t=50, b=20)
            )
            st.plotly_chart(fig_cost, use_container_width=True)

        with c_chart2:
            if not f_df.empty:
                f_df_plot = f_df.copy()
                f_df_plot["mileage_kmpl"] = pd.to_numeric(f_df_plot["mileage_kmpl"], errors="coerce").fillna(0.0).astype(float)
                f_df_plot = f_df_plot.sort_values(["log_date", "id"])

                fig_mil = go.Figure()
                for veh_name, grp in f_df_plot.groupby("vehicle"):
                    fig_mil.add_trace(go.Scatter(
                        x=grp["log_date"].tolist(),
                        y=grp["mileage_kmpl"].tolist(),
                        mode="lines+markers",
                        name=str(veh_name)
                    ))
                fig_mil.update_layout(
                    title="Mileage Trend per Fill-up (km/L)",
                    xaxis_title="Fill-up Date",
                    yaxis_title="Mileage (km/L)",
                    legend_title="Vehicle",
                    margin=dict(l=20, r=20, t=50, b=20)
                )
                st.plotly_chart(fig_mil, use_container_width=True)

with tabs[2]:
    render_dashboard_tab(active_fuel_df, active_maint_df)

# =====================================================================
# TAB 4: HDFC INDIANOIL 5% FUEL POINTS TRACKER (Month Range Filter + 1 FP = ₹0.90)
# =====================================================================
@st.fragment
def render_hdfc_tab(fuel_df):
    st.subheader("💳 HDFC IndianOil Credit Card — 5% Fuel Points Tracker (1 FP = ₹0.90)")

    if fuel_df.empty:
        st.info("No fuel transactions logged yet.")
        return

    hdfc_df = fuel_df[fuel_df["payment_method"].astype(str).str.contains("HDFC IndianOil", na=False)].copy()
    if hdfc_df.empty:
        st.warning("No transactions logged with the HDFC IndianOil Credit Card yet.")
        return

    hdfc_df["month"] = pd.to_datetime(hdfc_df["log_date"]).dt.strftime("%Y-%m")
    hdfc_df["total_cost"] = pd.to_numeric(hdfc_df["total_cost"], errors="coerce").fillna(0.0)
    hdfc_df["fuel_points"] = pd.to_numeric(hdfc_df["fuel_points"], errors="coerce").fillna(0.0)
    hdfc_df["litres"] = pd.to_numeric(hdfc_df["litres"], errors="coerce").fillna(0.0)

    # Sorted chronological list of available months for Start/End Range selection
    available_months_asc = sorted(hdfc_df["month"].dropna().unique().tolist())
    available_stations = sorted(hdfc_df["station"].dropna().unique().tolist())

    with st.expander("🔍 Filter Fuel Points by Month Range, Station & Vehicle", expanded=True):
        f_col1, f_col2, f_col3, f_col4 = st.columns(4)
        with f_col1:
            start_month = st.selectbox(
                "From Month (Start)",
                options=available_months_asc,
                index=0,
                key="hdfc_start_month"
            )
        with f_col2:
            # Ensure End Month options are >= Start Month
            valid_end_months = [m for m in available_months_asc if m >= start_month]
            end_month = st.selectbox(
                "To Month (End)",
                options=valid_end_months,
                index=len(valid_end_months) - 1,
                key="hdfc_end_month"
            )
        with f_col3:
            sel_h_station = st.selectbox(
                "Filling Station",
                ["All Stations"] + available_stations,
                key="hdfc_st_filter"
            )
        with f_col4:
            sel_h_veh = st.selectbox(
                "Vehicle",
                ["All Vehicles"] + VEHICLES,
                key="hdfc_veh_filter"
            )

    # Apply Month Range & Other Filters
    hdfc_df = hdfc_df[(hdfc_df["month"] >= start_month) & (hdfc_df["month"] <= end_month)]
    if sel_h_station != "All Stations":
        hdfc_df = hdfc_df[hdfc_df["station"] == sel_h_station]
    if sel_h_veh != "All Vehicles":
        hdfc_df = hdfc_df[hdfc_df["vehicle"] == sel_h_veh]

    if hdfc_df.empty:
        st.warning(f"No HDFC IndianOil transactions found between **{start_month}** and **{end_month}** for the selected filters.")
        return

    total_hdfc_spend = float(hdfc_df["total_cost"].sum())
    total_fp = float(hdfc_df["fuel_points"].sum())
    total_savings_inr = round(total_fp * FP_RUPEE_VALUE, 2)

    p1, p2, p3, p4 = st.columns(4)
    p1.metric("Selected Period", f"{start_month} → {end_month}")
    p2.metric("Total HDFC Card Spend", f"₹{total_hdfc_spend:,.2f}")
    p3.metric("Total 5% Fuel Points", f"{total_fp:,.2f} FP")
    p4.metric("Effective Fuel Savings (1 FP = ₹0.90)", f"₹{total_savings_inr:,.2f}")

    st.markdown("#### 📅 Monthly Fuel Points Statement Breakdown")
    monthly_fp = hdfc_df.groupby(["month", "station"]).agg(
        tx_count=("id", "count"),
        total_litres=("litres", "sum"),
        total_spend=("total_cost", "sum"),
        points_earned=("fuel_points", "sum")
    ).reset_index().sort_values("month", ascending=False)
    monthly_fp["rupee_value"] = (monthly_fp["points_earned"] * FP_RUPEE_VALUE).round(2)

    st.dataframe(
        monthly_fp.rename(columns={
            "month": "Month",
            "station": "Filling Station",
            "tx_count": "Refills",
            "total_litres": "Petrol (L)",
            "total_spend": "Total Spend (₹)",
            "points_earned": "5% Fuel Points (FP)",
            "rupee_value": "Value in ₹ (1 FP = ₹0.90)"
        }).style.format({
            "Petrol (L)": "{:.2f}",
            "Total Spend (₹)": "₹{:,.2f}",
            "5% Fuel Points (FP)": "{:.2f} FP",
            "Value in ₹ (1 FP = ₹0.90)": "₹{:,.2f}"
        }),
        use_container_width=True,
        hide_index=True
    )

    with st.expander(f"🧾 View Individual HDFC Card Transactions ({len(hdfc_df)} refills in range)", expanded=False):
        tx_display = hdfc_df[["log_date", "vehicle", "rider", "station", "litres", "total_cost", "fuel_points", "notes"]].copy()
        tx_display["rupee_val"] = (tx_display["fuel_points"] * FP_RUPEE_VALUE).round(2)
        tx_display = tx_display.sort_values("log_date", ascending=False)
        st.dataframe(
            tx_display.rename(columns={
                "log_date": "Date",
                "vehicle": "Vehicle",
                "rider": "Rider",
                "station": "Station",
                "litres": "Litres",
                "total_cost": "Cost Paid (₹)",
                "fuel_points": "Fuel Points (FP)",
                "rupee_val": "Savings (₹)",
                "notes": "Notes"
            }),
            use_container_width=True,
            hide_index=True
        )

with tabs[3]:
    render_hdfc_tab(active_fuel_df)

# =====================================================================
# TAB 5: FULL HISTORY SEARCH, POP-UP MODIFY & SOFT DELETE
# =====================================================================
@st.fragment
def render_history_tab(fuel_df, maint_df):
    st.subheader("🗂️ Full History Search, Modify & Soft Delete")
    st.caption("Search anything across your entire history, or click Modify / Delete to open a pop-up modal.")

    with st.expander("🔎 Search & Filter Full History (Find Anything)", expanded=True):
        h_c1, h_c2, h_c3, h_c4 = st.columns(4)
        with h_c1:
            hist_search = st.text_input("Keyword Search (Notes, Station, Category, Amount, ID)", key="hist_search").strip()
        with h_c2:
            hist_veh = st.selectbox("Filter Vehicle", ["All Vehicles"] + VEHICLES, key="hist_veh")
        with h_c3:
            hist_rider = st.selectbox("Filter Rider", ["All Riders"] + RIDERS, key="hist_rider")
        with h_c4:
            hist_pay = st.selectbox("Filter Payment Method", ["All Payment Modes"] + PAYMENT_METHODS, key="hist_pay")

        h_d1, h_d2, h_d3 = st.columns(3)
        with h_d1:
            use_date_filter = st.checkbox("Filter by Date Range", value=False, key="hist_use_date")
        with h_d2:
            start_dt = st.date_input("Start Date", value=date.today() - timedelta(days=90), disabled=not use_date_filter, key="hist_start")
        with h_d3:
            end_dt = st.date_input("End Date", value=date.today(), disabled=not use_date_filter, key="hist_end")

    filt_fuel = fuel_df.copy()
    if not filt_fuel.empty:
        if hist_veh != "All Vehicles":
            filt_fuel = filt_fuel[filt_fuel["vehicle"] == hist_veh]
        if hist_rider != "All Riders":
            filt_fuel = filt_fuel[filt_fuel["rider"] == hist_rider]
        if hist_pay != "All Payment Modes":
            filt_fuel = filt_fuel[filt_fuel["payment_method"] == hist_pay]
        if use_date_filter:
            filt_fuel = filt_fuel[
                (pd.to_datetime(filt_fuel["log_date"]).dt.date >= start_dt)
                & (pd.to_datetime(filt_fuel["log_date"]).dt.date <= end_dt)
            ]
        if hist_search:
            combined_f = filt_fuel.astype(str).agg(" ".join, axis=1)
            filt_fuel = filt_fuel[combined_f.str.contains(hist_search, case=False, na=False)]

    filt_maint = maint_df.copy()
    if not filt_maint.empty:
        if hist_veh != "All Vehicles":
            filt_maint = filt_maint[filt_maint["vehicle"] == hist_veh]
        if hist_rider != "All Riders":
            filt_maint = filt_maint[filt_maint["rider"] == hist_rider]
        if hist_pay != "All Payment Modes":
            filt_maint = filt_maint[filt_maint["payment_method"] == hist_pay]
        if use_date_filter:
            filt_maint = filt_maint[
                (pd.to_datetime(filt_maint["service_date"]).dt.date >= start_dt)
                & (pd.to_datetime(filt_maint["service_date"]).dt.date <= end_dt)
            ]
        if hist_search:
            combined_m = filt_maint.astype(str).agg(" ".join, axis=1)
            filt_maint = filt_maint[combined_m.str.contains(hist_search, case=False, na=False)]

    # ---- FUEL HISTORY SECTION ----
    st.markdown(f"#### ⛽ Fuel Refill History ({len(filt_fuel)} matching records)")
    if not filt_fuel.empty:
        display_fuel_cols = [c for c in filt_fuel.columns if c not in ("is_deleted", "deleted_at")]
        st.dataframe(
            filt_fuel[display_fuel_cols].sort_values(["log_date", "id"], ascending=[False, False]),
            use_container_width=True,
            hide_index=True
        )

        fuel_options = {
            int(row["id"]): f"ID #{int(row['id'])} | {row['log_date']} | {row['vehicle']} | ₹{row['total_cost']} ({row['distance_km']} KM → {row['mileage_kmpl']} km/L)"
            for _, row in filt_fuel.sort_values("id", ascending=False).iterrows()
        }

        f_bar1, f_bar2, f_bar3 = st.columns([3, 1, 1])
        with f_bar1:
            selected_fuel_id = st.selectbox(
                "Select Fuel Entry to Modify or Delete",
                options=list(fuel_options.keys()),
                format_func=lambda x: fuel_options[x],
                key="manage_fuel_select"
            )
        sel_f_row = filt_fuel[filt_fuel["id"] == selected_fuel_id].iloc[0].to_dict()

        with f_bar2:
            st.write("")
            st.write("")
            if st.button("✏️ Modify Fuel Entry", type="primary", key="btn_open_mod_fuel"):
                st.session_state.pop(f"confirm_fuel_payload_{selected_fuel_id}", None)
                edit_fuel_dialog(sel_f_row)
        with f_bar3:
            st.write("")
            st.write("")
            if st.button("🗑️ Delete Fuel Entry", key="btn_soft_del_fuel"):
                confirm_action_dialog(
                    action_type="soft_delete",
                    title_text="Move Fuel Entry to Recycle Bin?",
                    details_md=f"**Entry:** {fuel_options[selected_fuel_id]}\n\n*Note: You can restore this from the Recycle Bin within 3 days.*",
                    payload={"table": "vehicle_fuel_logs", "id": selected_fuel_id, "label": fuel_options[selected_fuel_id]}
                )
    else:
        st.caption("No matching fuel logs found.")

    st.divider()

    # ---- MAINTENANCE HISTORY SECTION ----
    st.markdown(f"#### 🔧 Maintenance History ({len(filt_maint)} matching records)")
    if not filt_maint.empty:
        display_maint_cols = [c for c in filt_maint.columns if c not in ("is_deleted", "deleted_at")]
        st.dataframe(
            filt_maint[display_maint_cols].sort_values(["service_date", "id"], ascending=[False, False]),
            use_container_width=True,
            hide_index=True
        )

        maint_options = {
            int(row["id"]): f"ID #{int(row['id'])} | {row['service_date']} | {row['vehicle']} | {row['category']} (₹{row['cost']})"
            for _, row in filt_maint.sort_values("id", ascending=False).iterrows()
        }

        m_bar1, m_bar2, m_bar3 = st.columns([3, 1, 1])
        with m_bar1:
            selected_maint_id = st.selectbox(
                "Select Maintenance Entry to Modify or Delete",
                options=list(maint_options.keys()),
                format_func=lambda x: maint_options[x],
                key="manage_maint_select"
            )
        sel_m_row = filt_maint[filt_maint["id"] == selected_maint_id].iloc[0].to_dict()

        with m_bar2:
            st.write("")
            st.write("")
            if st.button("✏️ Modify Maintenance", type="primary", key="btn_open_mod_maint"):
                st.session_state.pop(f"confirm_maint_payload_{selected_maint_id}", None)
                edit_maint_dialog(sel_m_row)
        with m_bar3:
            st.write("")
            st.write("")
            if st.button("🗑️ Delete Maintenance", key="btn_soft_del_maint"):
                confirm_action_dialog(
                    action_type="soft_delete",
                    title_text="Move Maintenance Entry to Recycle Bin?",
                    details_md=f"**Entry:** {maint_options[selected_maint_id]}\n\n*Note: You can restore this from the Recycle Bin within 3 days.*",
                    payload={"table": "vehicle_maintenance_logs", "id": selected_maint_id, "label": maint_options[selected_maint_id]}
                )
    else:
        st.caption("No matching maintenance logs found.")

with tabs[4]:
    render_history_tab(active_fuel_df, active_maint_df)

# =====================================================================
# TAB 6: 3-DAY AUTO-CLEARING RECYCLE BIN (Isolated Fragment)
# =====================================================================
@st.fragment
def render_recycle_bin_tab(del_fuel_df, del_maint_df):
    st.subheader("🗑️ Recycle Bin (3-Day Auto-Clear)")
    st.caption("Items in the Recycle Bin are excluded from all charts and totals, and are permanently purged 72 hours after deletion.")

    if del_fuel_df.empty and del_maint_df.empty:
        st.success("🎉 Recycle Bin is completely empty!")
        return

    b_col1, b_col2 = st.columns(2)
    with b_col1:
        if st.button("↩️ Restore All Deleted Items", type="primary"):
            confirm_action_dialog(
                action_type="restore_all",
                title_text="Restore All Deleted Items?",
                details_md=f"This will restore **{len(del_fuel_df)} fuel logs** and **{len(del_maint_df)} maintenance logs** back to your active logbook.",
                payload={}
            )
    with b_col2:
        if st.button("🔥 Empty Recycle Bin Permanently"):
            confirm_action_dialog(
                action_type="empty_bin",
                title_text="Permanently Empty Recycle Bin?",
                details_md="⚠️ **Warning:** This will permanently erase all items currently in the Recycle Bin. This action cannot be undone!",
                payload={}
            )

    st.divider()

    st.markdown("#### ⛽ Deleted Fuel Entries")
    if not del_fuel_df.empty:
        df_bin_f = del_fuel_df.copy()
        df_bin_f["Auto-Purge In"] = df_bin_f["deleted_at"].apply(format_time_remaining)
        st.dataframe(
            df_bin_f[["id", "Auto-Purge In", "deleted_at", "log_date", "vehicle", "rider", "distance_km", "litres", "total_cost", "station"]],
            use_container_width=True,
            hide_index=True
        )

        rf_col1, rf_col2, rf_col3 = st.columns([2, 1, 1])
        with rf_col1:
            del_fuel_map = {
                int(r["id"]): f"ID #{int(r['id'])} | {r['log_date']} | {r['vehicle']} | ₹{r['total_cost']} ({r['Auto-Purge In']})"
                for _, r in df_bin_f.iterrows()
            }
            target_fuel_id = st.selectbox(
                "Select Deleted Fuel Entry",
                options=list(del_fuel_map.keys()),
                format_func=lambda x: del_fuel_map[x],
                key="bin_fuel_select"
            )
        with rf_col2:
            st.write("")
            st.write("")
            if st.button("↩️ Undo / Restore Fuel", key="restore_fuel_btn"):
                restore_record("vehicle_fuel_logs", target_fuel_id)
                st.toast(f"Restored Fuel Entry #{target_fuel_id}!", icon="✅")
                st.rerun()
        with rf_col3:
            st.write("")
            st.write("")
            if st.button("❌ Delete Permanently", key="perm_del_fuel_btn"):
                confirm_action_dialog(
                    action_type="perm_delete",
                    title_text=f"Permanently Delete Fuel Entry #{target_fuel_id}?",
                    details_md=f"**Entry:** {del_fuel_map[target_fuel_id]}\n\n⚠️ This record will be permanently removed and cannot be recovered.",
                    payload={"table": "vehicle_fuel_logs", "id": target_fuel_id}
                )
    else:
        st.caption("No deleted fuel logs in the Recycle Bin.")

    st.divider()

    st.markdown("#### 🔧 Deleted Maintenance Entries")
    if not del_maint_df.empty:
        df_bin_m = del_maint_df.copy()
        df_bin_m["Auto-Purge In"] = df_bin_m["deleted_at"].apply(format_time_remaining)
        st.dataframe(
            df_bin_m[["id", "Auto-Purge In", "deleted_at", "service_date", "vehicle", "category", "cost", "notes"]],
            use_container_width=True,
            hide_index=True
        )

        rm_col1, rm_col2, rm_col3 = st.columns([2, 1, 1])
        with rm_col1:
            del_maint_map = {
                int(r["id"]): f"ID #{int(r['id'])} | {r['service_date']} | {r['vehicle']} | {r['category']} | ₹{r['cost']} ({r['Auto-Purge In']})"
                for _, r in df_bin_m.iterrows()
            }
            target_maint_id = st.selectbox(
                "Select Deleted Maintenance Entry",
                options=list(del_maint_map.keys()),
                format_func=lambda x: del_maint_map[x],
                key="bin_maint_select"
            )
        with rm_col2:
            st.write("")
            st.write("")
            if st.button("↩️ Undo / Restore Maintenance", key="restore_maint_btn"):
                restore_record("vehicle_maintenance_logs", target_maint_id)
                st.toast(f"Restored Maintenance Entry #{target_maint_id}!", icon="✅")
                st.rerun()
        with rm_col3:
            st.write("")
            st.write("")
            if st.button("❌ Delete Permanently", key="perm_del_maint_btn"):
                confirm_action_dialog(
                    action_type="perm_delete",
                    title_text=f"Permanently Delete Maintenance Entry #{target_maint_id}?",
                    details_md=f"**Entry:** {del_maint_map[target_maint_id]}\n\n⚠️ This record will be permanently removed and cannot be recovered.",
                    payload={"table": "vehicle_maintenance_logs", "id": target_maint_id}
                )
    else:
        st.caption("No deleted maintenance logs in the Recycle Bin.")

with tabs[5]:
    render_recycle_bin_tab(del_fuel_df, del_maint_df)