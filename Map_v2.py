# app.py
import streamlit as st
import geopandas as gpd
import pandas as pd
import plotly.express as px
import json
import io
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib.units import mm

# ----------------------
# Config
# ----------------------
st.set_page_config(page_title="Hydrogen Impact Atlas", layout="wide")

# --- EDIT THESE PATHS AS NEEDED ---
SHAPEFILE_PATH = "Shape_file_10m/ne_10m_admin_0_countries.shp"
EXCEL_PATH = "GHI_CoinR_v2.xlsx"
SCENARIO_SHEETS = ["iData_Short", "iData_Mid", "iData_Long"]
META_SHEET = "iMeta"

# Footer text to show on each PDF page — put your name & email here:
FOOTER_TEXT = "Designed by JeanDi Kouakou — jeandidikouakou@gmail.com"
# ----------------------


# ----------------------
# Helpers & cached loaders
# ----------------------
@st.cache_data(show_spinner=False)
def load_world_africa(shapefile_path, simplify_tol=0.05):
    """Load shapefile, keep Africa only, simplify geometry for speed, return GeoDataFrame and geojson."""
    gdf = gpd.read_file(shapefile_path)
    # keep only Africa if column exists
    if "CONTINENT" in gdf.columns:
        gdf = gdf[gdf["CONTINENT"] == "Africa"].copy()
    # ensure expected columns
    if "SOV_A3" not in gdf.columns and "ADM0_A3" in gdf.columns:
        gdf["SOV_A3"] = gdf["ADM0_A3"]
    if "SOV_A3" not in gdf.columns:
        raise RuntimeError("Shapefile missing SOV_A3/ADM0_A3 column (ISO3) — adapt shapefile or code.")
    # keep only needed columns and rename
    gdf = gdf[["SOV_A3", "ADMIN", "geometry"]].rename(columns={"SOV_A3": "ISO3", "ADMIN": "Country"})
    # simplify geometry slightly to speed up
    try:
        gdf["geometry"] = gdf["geometry"].simplify(simplify_tol, preserve_topology=True)
    except Exception:
        pass
    gdf = gdf.reset_index(drop=True)
    geojson = json.loads(gdf.to_json())
    return gdf, geojson

@st.cache_data(show_spinner=False)
def load_excel_sheets(excel_path, scenario_sheets, meta_sheet):
    """Load only scenario sheets + meta sheet from excel and standardize column names."""
    all_sheets = pd.read_excel(excel_path, sheet_name=scenario_sheets + [meta_sheet])
    # extract meta and scenarios
    meta_df = all_sheets[meta_sheet] if meta_sheet in all_sheets else pd.DataFrame()
    scenarios = {s: all_sheets[s] for s in scenario_sheets if s in all_sheets}
    # standardize each scenario df: rename common id/name columns to ISO3 / Country
    for name, df in scenarios.items():
        df = df.copy()
        # strip headers spaces
        df.columns = [c.strip() if isinstance(c, str) else c for c in df.columns]
        low = {c.lower(): c for c in df.columns if isinstance(c, str)}
        rename_map = {}
        if "ucode" in low:
            rename_map[low["ucode"]] = "ISO3"
        elif "code" in low:
            rename_map[low["code"]] = "ISO3"
        elif "iso3" in low:
            rename_map[low["iso3"]] = "ISO3"

        if "uname" in low:
            rename_map[low["uname"]] = "Country"
        elif "country" in low:
            rename_map[low["country"]] = "Country"

        df = df.rename(columns=rename_map)
        # Ensure ISO3 uppercase if present
        if "ISO3" in df.columns:
            df["ISO3"] = df["ISO3"].astype(str).str.upper()
        scenarios[name] = df
    return scenarios, meta_df


def classify_three(series):
    """Return categorical series: Low / Medium / High / NoData using terciles (qcut)."""
    s = pd.to_numeric(series, errors="coerce")
    if s.dropna().empty:
        return pd.Series(["NoData"] * len(s), index=s.index)
    if s.nunique(dropna=True) == 1:
        # all same value -> mark as Medium for non-null values
        out = pd.Series(["NoData"] * len(s), index=s.index)
        out[s.notna()] = "Medium"
        return out
    try:
        cats = pd.qcut(s, q=3, labels=["Low", "Medium", "High"])
    except Exception:
        cats = pd.cut(s, bins=3, labels=["Low", "Medium", "High"])
    out = pd.Series(["NoData"] * len(s), index=s.index)
    out.loc[cats.index] = cats.astype(str)
    return out


def cat_to_label(cat):
    return {"Low": "Low", "Medium": "Moderate", "High": "High", "NoData": "No data"}.get(cat, "No data")


def make_pdf_for_country(country, iso, scenarios_dict, meta_dict, footer_text=FOOTER_TEXT):
    """
    Build PDF bytes: for each scenario, for each indicator, one page:
    - Title: explicit indicator name (meta['IndName'] if present)
    - Qualitative level (Low/Moderate/High) — no raw number
    - Note (from meta['Note']) under the result
    - Footer with designer info
    """
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    width, height = A4

    for scen_name, df in scenarios_dict.items():
        if "ISO3" not in df.columns:
            continue
        # compute classification for all indicators in this scenario in advance
        indicators = [col for col in df.columns if col not in ["ISO3", "Country"]]
        if not indicators:
            continue
        # ensure numeric conversion handled inside classify_three
        classes = {}
        for ind in indicators:
            classes[ind] = pd.Series(classify_three(df[ind]).values, index=df["ISO3"].astype(str).str.upper())

        # find row index for country
        if iso not in df["ISO3"].astype(str).str.upper().values:
            # country not present in this scenario
            continue

        for ind in indicators:
            # get category for this iso
            cat = classes[ind].get(iso, "NoData")
            label = cat_to_label(cat)
            # meta info
            meta_info = meta_dict.get(ind, {})
            ind_name = meta_info.get("IndName", ind)
            note = meta_info.get("Note", "")

            # new page
            c.showPage()
            # Header
            c.setFont("Helvetica-Bold", 16)
            c.drawString(20 * mm, height - 20 * mm, f"{country} ({iso})")
            c.setFont("Helvetica-Bold", 13)
            c.drawString(20 * mm, height - 32 * mm, f"Scenario: {scen_name}")
            # Indicator title
            c.setFont("Helvetica-Bold", 12)
            c.drawString(20 * mm, height - 44 * mm, f"Indicator: {ind_name}")
            # Qualitative level (no numbers)
            c.setFont("Helvetica", 11)
            c.drawString(20 * mm, height - 58 * mm, f"Qualitative level: {label}")
            # Note (if exists)
            if note and isinstance(note, str) and note.strip():
                c.setFont("Helvetica-Oblique", 10)
                # wrap text if too long (naïve)
                text_y = height - 72 * mm
                max_width = width - 40 * mm
                # simple wrap
                for chunk in split_text_to_lines(note, max_chars=95):
                    c.drawString(20 * mm, text_y, chunk)
                    text_y -= 6 * mm

            # Footer
            c.setFont("Helvetica", 9)
            c.drawString(20 * mm, 10 * mm, footer_text)

    c.save()
    buf.seek(0)
    return buf.getvalue()


def split_text_to_lines(text, max_chars=95):
    """Simple wrapper to split long note text into lines of ~max_chars characters."""
    words = text.split()
    lines = []
    current = ""
    for w in words:
        if len(current) + 1 + len(w) <= max_chars:
            current = (current + " " + w).strip()
        else:
            lines.append(current)
            current = w
    if current:
        lines.append(current)
    return lines


# ----------------------
# Load data (cached)
# ----------------------
with st.spinner("Loading shapefile & Excel (only Africa + scenario sheets)..."):
    world_gdf, world_geojson = load_world_africa(SHAPEFILE_PATH, simplify_tol=0.05)
    scenarios, meta_df = load_excel_sheets(EXCEL_PATH, SCENARIO_SHEETS, META_SHEET)

# Build meta dict: key = indicator code (iCode), values = {IndName, Note}
meta_dict = {}
if not meta_df.empty:
    meta_df = meta_df.rename(columns={c: c.strip() for c in meta_df.columns})
    lower_map = {c.lower(): c for c in meta_df.columns}
    code_col = lower_map.get("icode", None)
    name_col = lower_map.get("indname", None)
    note_col = lower_map.get("note", None)
    if code_col and name_col:
        for _, r in meta_df.iterrows():
            code = str(r[code_col])
            meta_dict[code] = {
                "IndName": str(r[name_col]) if pd.notna(r[name_col]) else code,
                "Note": str(r[note_col]) if note_col and pd.notna(r.get(note_col, None)) else ""
            }

# ----------------------
# Sidebar controls (ENGLISH)
# ----------------------
st.sidebar.title("Controls")
scenario_choice = st.sidebar.selectbox("Select scenario", SCENARIO_SHEETS, index=0)
df = scenarios.get(scenario_choice).copy()

# try to ensure ISO3 & Country columns exist
if "ISO3" not in df.columns:
    low = {c.lower(): c for c in df.columns}
    if "ucode" in low:
        df = df.rename(columns={low["ucode"]: "ISO3"})
    elif "code" in low:
        df = df.rename(columns={low["code"]: "ISO3"})
if "Country" not in df.columns:
    low = {c.lower(): c for c in df.columns}
    if "uname" in low:
        df = df.rename(columns={low["uname"]: "Country"})

# Ensure ISO3 uppercase
if "ISO3" in df.columns:
    df["ISO3"] = df["ISO3"].astype(str).str.upper()

# Determine name/id columns and indicator columns
id_col = "ISO3" if "ISO3" in df.columns else df.columns[0]
name_col = "Country" if "Country" in df.columns else (df.columns[1] if len(df.columns) > 1 else id_col)
indicator_cols = [c for c in df.columns if c not in [id_col, name_col]]

if not indicator_cols:
    st.error("No indicators found in the selected sheet. Check your Excel sheets and column names.")
    st.stop()

indicator_choice = st.sidebar.selectbox("Select indicator", indicator_cols)

# Get explicit name/title & note from meta (if present)
indicator_title = meta_dict.get(indicator_choice, {}).get("IndName", indicator_choice)
indicator_note = meta_dict.get(indicator_choice, {}).get("Note", "")

# Country selector (based on scenario df)
country_list = sorted(df[name_col].dropna().unique().astype(str))
selected_country = st.sidebar.selectbox("Select country", country_list)

# Button to generate PDF (computes and offers download)
gen_pdf = st.sidebar.button("Generate PDF profile")

# ----------------------
# Main layout: map (wide) + right info panel (narrow)
# ----------------------
left_col, right_col = st.columns([3, 1])

# Prepare merged gdf for the map (world_gdf already Africa only)
# Merge on ISO3; ensure both sides uppercase strings
map_df = world_gdf.merge(df, left_on="ISO3", right_on="ISO3", how="left")
# classification (Category) for map display
map_df["Category"] = classify_three(map_df[indicator_choice] if indicator_choice in map_df.columns else pd.Series([None]*len(map_df)))
# color mapping (strong orange, green, blue)
#color_map = {"Low": "#4d98ce", "Medium": "#51b551", "High": "#f48337"}

# Build Plotly figure
fig = px.choropleth(
    map_df,
    geojson=world_geojson,
    locations="ISO3",
    featureidkey="properties.ISO3",
    color="Category",
    category_orders={"Category": ["High", "Medium", "Low"]},
    #color_discrete_map=color_map,
    hover_name="Country_y" if "Country_y" in map_df.columns else "Country_x",
    hover_data={indicator_choice: True} if indicator_choice in map_df.columns else None,
    projection="mercator",
    title=f"{indicator_title} — {scenario_choice.replace('iData_', '')}"
)
fig.update_geos(fitbounds="locations", visible=False)
fig.update_layout(margin={"r":0,"t":40,"l":0,"b":0}, paper_bgcolor="white", legend_title_text="Category")

# Show map large
with left_col:
    st.title("Green Hydrogen Impact Atlas — Africa")
    st.plotly_chart(fig, use_container_width=True)

# Right panel: indicator title, note, four summary numbers
with right_col:
    st.subheader(indicator_title)
    if indicator_note:
        st.caption(indicator_note)
    # compute 4 summary numbers: min, 25th, median, max (ignore NaN)
    ser = pd.to_numeric(df[indicator_choice], errors="coerce")
    if ser.dropna().empty:
        st.write("No numeric data available for this indicator in the selected scenario.")
    else:
        min_v = ser.min()
        q1 = ser.quantile(0.25)
        med = ser.median()
        max_v = ser.max()
        st.markdown("**Summary (selected scenario)**")
        st.metric("Min", f"{min_v:.3g}")
        st.metric("25th percentile", f"{q1:.3g}")
        st.metric("Median", f"{med:.3g}")
        st.metric("Max", f"{max_v:.3g}")

# Data preview (first 4 rows)
st.subheader("Data preview (first 4 rows)")
display_cols = [name_col, id_col, indicator_choice] if id_col != name_col else [name_col, indicator_choice]
st.dataframe(df[display_cols].head(4), use_container_width=True)

# ----------------------
# PDF generation & download
# ----------------------
if gen_pdf:
    if not selected_country:
        st.sidebar.warning("Select a country first.")
    else:
        # find ISO code for selected country in the chosen scenario (best-effort)
        iso_val = None
        row = df[df[name_col].astype(str) == str(selected_country)]
        if not row.empty and "ISO3" in row.columns:
            iso_val = str(row["ISO3"].iloc[0]).upper()
        elif selected_country in map_df["Country"].values:
            iso_val = map_df.loc[map_df["Country"] == selected_country, "ISO3"].iloc[0]
        else:
            st.sidebar.error("ISO3 code not found for selected country; PDF cannot be generated.")
            iso_val = None

        if iso_val:
            # Precompute meta_dict mapping (already built)
            pdf_bytes = make_pdf_for_country(selected_country, iso_val, scenarios, meta_dict)
            st.sidebar.download_button(
                label=f"Download PDF profile for {selected_country}",
                data=pdf_bytes,
                file_name=f"profile_{selected_country}.pdf",
                mime="application/pdf"
            )

# Footer note in app
st.markdown("---")
st.markdown(f"<small>{FOOTER_TEXT}</small>", unsafe_allow_html=True)
