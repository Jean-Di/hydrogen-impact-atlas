# app.py
# =============================================================================
# Hydrogen Impact Atlas — Africa-only interactive atlas with PDF country reports
# =============================================================================
# Features:
# - Loads Africa-only shapes (simplified for speed)
# - Reads Excel with 3 scenario sheets + iMeta + iAssumptions
# - Map: Blue (Low), Green (Medium), Orange (High), Grey (NoData)
# - Indicator title from iMeta.IndName, description note from iMeta.Note
# - Sidebar: choose scenario, indicator (for map), pick indicators (for PDF), country, download PDF
# - PDF: Page 1 summary table (qualitative), then one page per indicator with value + note
# - Footer on each PDF page
#
# HOW TO EDIT COLORS:
#   -> See COLOR_MAP and HEX constants under "Style / Color settings".
#
# HOW TO ADD / EDIT ASSUMPTIONS:
#   -> Edit the "iAssumptions" sheet in Excel (columns: Scenario, Text).
#
# HOW TO DEPLOY:
#   1) Local: streamlit run app.py
#   2) Streamlit Cloud: push repo to GitHub, set secrets if any, deploy via share.streamlit.io
#   3) Docker: FROM python:3.11-slim; pip install -r requirements.txt; streamlit run app.py --server.port 8501
#
# =============================================================================

import io
import json
import geopandas as gpd
import pandas as pd
import plotly.express as px
import streamlit as st
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib.units import mm

# -----------------------------------------------------------------------------
# Streamlit page config
# -----------------------------------------------------------------------------
st.set_page_config(page_title="Hydrogen Impact Atlas", layout="wide")

# -----------------------------------------------------------------------------
# Paths & sheet names (EDIT HERE IF YOUR FILES MOVE)
# -----------------------------------------------------------------------------
SHAPEFILE_PATH = "Shape_file_10m/ne_10m_admin_0_countries.shp"
EXCEL_PATH = "GHI_CoinR.xlsx"

# IMPORTANT: The scenario sheet names in the Excel file.
SCENARIO_SHEETS = [
    "Short-Term Scenario",
    "Mid-Term Scenario",
    "Long-Term Scenario",
]

META_SHEET = "iMeta"            # must contain columns: iCode, IndName, Note (Note optional)
ASSUMPTIONS_SHEET = "iAssumptions"  # must contain columns: Scenario, Text

# Footer in PDF pages
FOOTER_TEXT = "Designed by JeanDi KOUAKOU — jeandidikouakou@gmail.com"

# -----------------------------------------------------------------------------
# Style / Color settings (EDIT COLORS HERE)
# -----------------------------------------------------------------------------
# Strong, accessible colors:

HEX_BLUE   =  "#c6dbef" # Low 
HEX_GREEN  =  "#6baed6"  # Medium
HEX_ORANGE =  "#08519c" # High (fort orange) 


COLOR_MAP = {
    "Low":    HEX_BLUE,
    "Medium": HEX_GREEN,
    "High":   HEX_ORANGE,
}

# Category order for legends
CATEGORY_ORDER = ["High", "Medium", "Low"]

# -----------------------------------------------------------------------------
# Cache loaders
# -----------------------------------------------------------------------------
@st.cache_data(show_spinner=False)
def load_world_africa(shapefile_path: str, simplify_tol: float = 0.05):
    """
    Load shapefile, keep Africa only, simplify geometry for speed, return:
      - GeoDataFrame with columns: ISO3, Country, geometry
      - GeoJSON dict for Plotly
    """
    gdf = gpd.read_file(shapefile_path)

    # Keep Africa only if CONTINENT column exists
    if "CONTINENT" in gdf.columns:
        gdf = gdf[gdf["CONTINENT"] == "Africa"].copy()

    # Normalize ISO3 column name
    if "SOV_A3" not in gdf.columns and "ADM0_A3" in gdf.columns:
        gdf["SOV_A3"] = gdf["ADM0_A3"]
    if "SOV_A3" not in gdf.columns:
        raise RuntimeError("Shapefile missing SOV_A3/ADM0_A3 column (ISO3).")

    # Keep and rename essential columns
    gdf = gdf[["SOV_A3", "ADMIN", "geometry"]].rename(
        columns={"SOV_A3": "ISO3", "ADMIN": "Country"}
    )

    # Simplify geometry to accelerate front-end rendering
    try:
        gdf["geometry"] = gdf["geometry"].simplify(simplify_tol, preserve_topology=True)
    except Exception:
        pass

    gdf["ISO3"] = gdf["ISO3"].astype(str).str.upper()
    gdf = gdf.reset_index(drop=True)

    geojson = json.loads(gdf.to_json())
    return gdf, geojson


@st.cache_data(show_spinner=False)
def load_excel(excel_path: str, scenario_sheets: list, meta_sheet: str, assumptions_sheet: str):
    """
    Read Excel once. Returns:
      - scenarios: dict[sheet_name] -> DataFrame
      - meta_df: DataFrame (iMeta)
      - assumptions_df: DataFrame (iAssumptions)
    """
    # Read just the sheets we need
    sheets_dict = pd.read_excel(
        excel_path, sheet_name=scenario_sheets + [meta_sheet, assumptions_sheet]
    )

    # Pull meta + assumptions with fallback to empty DF
    meta_df = sheets_dict.get(meta_sheet, pd.DataFrame())
    assumptions_df = sheets_dict.get(assumptions_sheet, pd.DataFrame())

    # Normalize scenario dataframes
    scenarios = {}
    for s in scenario_sheets:
        if s not in sheets_dict:
            continue
        df = sheets_dict[s].copy()
        df.columns = [c.strip() if isinstance(c, str) else c for c in df.columns]

        # Normalize ID / name columns
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

        if "ISO3" in df.columns:
            df["ISO3"] = df["ISO3"].astype(str).str.upper()

        scenarios[s] = df

    return scenarios, meta_df, assumptions_df


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def to_numeric(series: pd.Series) -> pd.Series:
    """Coerce to numeric (NaN on errors)."""
    return pd.to_numeric(series, errors="coerce")


def classify_three(series: pd.Series) -> pd.Series:
    """
    Classify values into 'Low'/'Medium'/'High' using terciles.
    NaNs => 'NoData'. If all values equal, non-NaN => 'Medium'.
    """
    s = to_numeric(series)
    
    # Constant case -> all observed get 'Medium'
    if s.nunique(dropna=True) == 1:
        #out = pd.Series(["NoData"] * len(s), index=s.index)
        out[s.notna()] = "Medium"
        return out

    try:
        cats = pd.qcut(s, q=3, labels=["Low", "Medium", "High"])
    except Exception:
        # Fallback to equal-width bins
        cats = pd.cut(s, bins=3, labels=["Low", "Medium", "High"])

    #out = pd.Series(["NoData"] * len(s), index=s.index)
    #out.loc[cats.index] = cats.astype(str)
    return cats


def category_to_label(cat: str) -> str:
    """Readable label for qualitative category."""
    return {
        "Low": "Low",
        "Medium": "Moderate",
        "High": "High",
        #"NoData": "No data",
    }.get(cat)


def split_text_to_lines(text: str, max_chars: int = 95) -> list:
    """Wrap text to ~max_chars per line (PDF only)."""
    if not text:
        return []
    words = str(text).split()
    lines, cur = [], ""
    for w in words:
        if len(cur) + (1 if cur else 0) + len(w) <= max_chars:
            cur = f"{cur} {w}".strip()
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


# -----------------------------------------------------------------------------
# PDF generation
# -----------------------------------------------------------------------------
def make_pdf_for_country(
    *,
    country: str,
    country_code: str,
    scenario_choice: str,
    scenario_df: pd.DataFrame,
    selected_indicators: list,
    meta_dict: dict,
    assumptions_text: str,
    footer_text: str,
) -> bytes:
    """
    Build a multi-page PDF:
    - Page 1: Summary table of selected indicators (qualitative only) + assumptions
    - Pages 2..N: One page per indicator with numeric value, qualitative level, and note.
    Only the SELECTED SCENARIO is used in this report.
    """
    # Precompute qualitative classes only from the selected scenario
    class_map = {}
    for ind in selected_indicators:
        if ind in scenario_df.columns:
            class_map[ind] = classify_three(scenario_df[ind])
        else:
            class_map[ind] = pd.Series(["NoData"] * len(scenario_df), index=scenario_df.index)

    # Extract the row for the selected country
    row = scenario_df[scenario_df["ISO3"].astype(str).str.upper() == country_code]

    # Begin PDF
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    W, H = A4

    # ------------------ PAGE 1: SUMMARY ------------------
    c.setFont("Helvetica-Bold", 16)
    c.drawString(20 * mm, H - 20 * mm, f"Country profile — {country} ({country_code})")
    c.setFont("Helvetica-Bold", 12)
    c.drawString(20 * mm, H - 30 * mm, f"Scenario: {scenario_choice}")

    y = H - 40 * mm

    # Assumptions block
    if assumptions_text:
        c.setFont("Helvetica", 10)
        c.drawString(20 * mm, y, "Assumptions:")
        y -= 6 * mm
        for ln in split_text_to_lines(assumptions_text, max_chars=100):
            c.drawString(22 * mm, y, ln)
            y -= 5 * mm
        y -= 4 * mm

    # Summary header
    c.setFont("Helvetica-Bold", 12)
    c.drawString(20 * mm, y, "Indicator")
    c.drawString(120 * mm, y, "Qualitative Level")
    y -= 7 * mm
    c.setFont("Helvetica", 10)

    for ind in selected_indicators:
        ind_name = meta_dict.get(ind, {}).get("IndName", ind)
        # Category for this country
        cat = "NoData"
        if country_code in class_map[ind].index:
            cat = class_map[ind].loc[class_map[ind].index[class_map[ind].index == class_map[ind].index[class_map[ind].index == class_map[ind].index][0]]].get(country_code, "NoData")
        # The above line is too defensive; simpler:
        if not row.empty:
            try:
                cat = class_map[ind].loc[row.index[0]]
            except Exception:
                pass

        c.drawString(20 * mm, y, ind_name)
        c.drawString(120 * mm, y, category_to_label(cat))
        y -= 6 * mm

        # Page break if needed
        if y < 40 * mm:
            c.setFont("Helvetica-Oblique", 8)
            c.drawString(20 * mm, 10 * mm, footer_text)

            # Disclaimer footer on 1st page
            c.setFont("Helvetica-Oblique", 8)
            c.drawString(20*mm, 15*mm, "Disclaimer: The results in this profile are for research purposes only.")
            c.showPage()
            # repeat header if summary continues onto next page
            y = H - 20 * mm
            c.setFont("Helvetica-Bold", 12)
            c.drawString(20 * mm, y, "Indicator")
            c.drawString(120 * mm, y, "Qualitative Level")
            y -= 7 * mm
            c.setFont("Helvetica", 10)

    # Footer & next page
    c.setFont("Helvetica-Oblique", 8)
    c.drawString(20 * mm, 10 * mm, footer_text)
    c.showPage()

    # ------------------ DETAIL PAGES (one per indicator) ------------------
    for ind in selected_indicators:
        ind_name = meta_dict.get(ind, {}).get("IndName", ind)
        note_text = meta_dict.get(ind, {}).get("Note", "")

        # Numeric value for selected country
        if not row.empty and ind in row.columns and pd.notna(row[ind].iloc[0]):
            try:
                value_text = f"{float(row[ind].iloc[0]):,.2f}"
            except Exception:
                value_text = str(row[ind].iloc[0])
        else:
            value_text = "N/A"

        # Qualitative category
        if not row.empty:
            try:
                cat = class_map[ind].loc[row.index[0]]
            except Exception:
                cat = "NoData"
        else:
            cat = "NoData"

        # Page layout
        c.setFont("Helvetica-Bold", 16)
        c.drawString(20 * mm, H - 25 * mm, f"{country} ({country_code}) — {scenario_choice}")
        c.setFont("Helvetica-Bold", 14)
        c.drawString(20 * mm, H - 40 * mm, ind_name)

        # Value & category
        c.setFont("Helvetica", 12)
        c.drawString(20 * mm, H - 55 * mm, f"Value: {value_text}")
        c.drawString(20 * mm, H - 65 * mm, f"Qualitative: {category_to_label(cat)}")

        # Indicator note
        y = H - 80 * mm
        if note_text:
            c.setFont("Helvetica-Oblique", 10)
            for ln in split_text_to_lines(note_text, max_chars=100):
                c.drawString(20 * mm, y, ln)
                y -= 12

        # Footer & next page
        c.setFont("Helvetica-Oblique", 8)
        c.drawString(20 * mm, 10 * mm, footer_text)
        c.showPage()

    # End
    c.save()
    buf.seek(0)
    return buf.getvalue()


# -----------------------------------------------------------------------------
# Load data
# -----------------------------------------------------------------------------
with st.spinner("Loading shapefile & Excel..."):
    world_gdf, world_geojson = load_world_africa(SHAPEFILE_PATH, simplify_tol=0.05)
    scenarios, meta_df, assumptions_df = load_excel(
        EXCEL_PATH, SCENARIO_SHEETS, META_SHEET, ASSUMPTIONS_SHEET
    )

# -----------------------------------------------------------------------------
# Build meta dict: {iCode: {"IndName": ..., "Note": ...}}
# -----------------------------------------------------------------------------
meta_dict = {}
if not meta_df.empty:
    meta_df = meta_df.rename(columns={c: c.strip() for c in meta_df.columns})
    lower = {c.lower(): c for c in meta_df.columns}
    code_col = lower.get("icode")
    name_col = lower.get("indname")
    note_col = lower.get("note")
    if code_col and name_col:
        for _, r in meta_df.iterrows():
            code = str(r[code_col])
            meta_dict[code] = {
                "IndName": str(r[name_col]) if pd.notna(r[name_col]) else code,
                "Note": str(r[note_col]) if (note_col and pd.notna(r.get(note_col, ""))) else "",
            }

# -----------------------------------------------------------------------------
# Sidebar: scenario, indicator (map), indicators (pdf), country
# -----------------------------------------------------------------------------
st.sidebar.title("Controls Panel")

scenario_choice = st.sidebar.selectbox("Select scenario", SCENARIO_SHEETS, index=0)
df = scenarios.get(scenario_choice, pd.DataFrame()).copy()

# Ensure ISO3 present
if "ISO3" not in df.columns:
    st.error(f"'ISO3' column not found in selected scenario: {scenario_choice}")
    st.stop()

# Figure out name column
if "Country" in df.columns:
    name_col = "Country"
else:
    # fallback to second column if present
    name_col = df.columns[1] if len(df.columns) > 1 else "ISO3"

# Build indicator list (exclude ID/name)
indicator_cols = [c for c in df.columns if c not in ["ISO3", name_col]]
if not indicator_cols:
    st.error("No indicators found in the selected sheet.")
    st.stop()

# Map-driving indicator
indicator_choice = st.sidebar.selectbox("Select indicator for map", indicator_cols)

# Explicit title + note from meta
indicator_title = meta_dict.get(indicator_choice, {}).get("IndName", indicator_choice)
indicator_note = meta_dict.get(indicator_choice, {}).get("Note", "")

# Indicators to include in the PDF
selected_indicators = st.sidebar.multiselect(
    "Select indicators to include in PDF",
    options=indicator_cols,
    default=indicator_cols[: min(8, len(indicator_cols))]
)

# Country selection (from current scenario)
country_list = sorted(df[name_col].dropna().astype(str).unique())
selected_country = st.sidebar.selectbox("Select country", country_list if country_list else ["—"])

# Scenario assumption text
assumption_text = ""
if not assumptions_df.empty and {"Scenario", "Text"}.issubset(assumptions_df.columns):
    row_ass = assumptions_df[
        assumptions_df["Scenario"].astype(str).str.strip().str.lower() ==
        scenario_choice.strip().lower()
    ]
    if not row_ass.empty:
        assumption_text = str(row_ass.iloc[0]["Text"])

# Button to generate PDF
gen_pdf = st.sidebar.button("Generate PDF profile")

# -----------------------------------------------------------------------------
# Map (left) + Info (right)
# -----------------------------------------------------------------------------
# Avoid duplicate Country columns during merge (keep the shapefile "Country")
df_for_merge = df.drop(columns=["Country"], errors="ignore").copy()
map_df = world_gdf.merge(df_for_merge, on="ISO3", how="left")

# Classification for map
if indicator_choice in map_df.columns:
    map_df["Category"] = classify_three(map_df[indicator_choice])


# Plotly choropleth
fig = px.choropleth(
    map_df,
    geojson=world_geojson,
    locations="ISO3",
    featureidkey="properties.ISO3",
    color="Category",
    category_orders={"Category": CATEGORY_ORDER},
    color_discrete_map=COLOR_MAP,
    hover_name="Country",  # from shapefile (world_gdf), guaranteed
    hover_data={indicator_choice: True} if indicator_choice in map_df.columns else None,
    projection="mercator",
    title=f"{indicator_title} — {scenario_choice}",
)

# Africa-only view
fig.update_geos(fitbounds="locations", visible=False)
fig.update_layout(
    margin={"r": 0, "t": 48, "l": 0, "b": 0},
    paper_bgcolor="white",
)

# Layout columns
left_col, right_col = st.columns([3.2, 1.0])

with left_col:
    st.title("Green Hydrogen Impact Atlas — Africa")
    st.plotly_chart(fig, use_container_width=True)

with right_col:
    st.subheader(indicator_title)
    if indicator_note:
        st.caption(indicator_note)

    # Summary numbers for this indicator (scenario-wide)
    ser = to_numeric(df[indicator_choice]) if indicator_choice in df.columns else pd.Series(dtype=float)
    if ser.dropna().empty:
        st.write("No numeric data available for this indicator in the selected scenario.")
    else:
        st.markdown(f"**Scenario assumption:** {assumption_text}")
        st.markdown("**Summary (selected scenario)**")
        # keep exactly 4 numbers as requested earlier
        st.metric("Min", f"{ser.min():.3g}")
        st.metric("25th percentile", f"{ser.quantile(0.25):.3g}")
        st.metric("Median", f"{ser.median():.3g}")
        st.metric("Max", f"{ser.max():.3g}")

# Data preview (quick)
st.subheader("Data preview")
preview_cols = ["ISO3", name_col, indicator_choice] if name_col in df.columns else ["ISO3", indicator_choice]
st.dataframe(df[preview_cols].head(12), use_container_width=True)

# -----------------------------------------------------------------------------
# PDF generation
# -----------------------------------------------------------------------------
if gen_pdf and selected_country and not df.empty:
    # Resolve ISO code for selected country
    if name_col in df.columns:
        row_country = df[df[name_col].astype(str) == str(selected_country)]
    else:
        row_country = pd.DataFrame()

    if not row_country.empty and "ISO3" in row_country.columns:
        iso_val = str(row_country.iloc[0]["ISO3"]).upper()
    else:
        st.sidebar.error("ISO3 code not found for selected country; PDF cannot be generated.")
        iso_val = None

    if iso_val:
        # Ensure "selected_indicators" are all valid columns
        valid_inds = [ind for ind in selected_indicators if ind in df.columns]
        if not valid_inds:
            st.sidebar.warning("No valid indicators selected for PDF.")
        else:
            pdf_bytes = make_pdf_for_country(
                country=selected_country,
                country_code=iso_val,
                scenario_choice=scenario_choice,
                scenario_df=df,
                selected_indicators=valid_inds,
                meta_dict=meta_dict,
                assumptions_text=assumption_text,
                footer_text=FOOTER_TEXT,
            )
            st.sidebar.download_button(
                label=f"Download PDF profile for {selected_country}",
                data=pdf_bytes,
                file_name=f"profile_{selected_country.replace(' ', '_')}.pdf",
                mime="application/pdf",
            )

# -----------------------------------------------------------------------------
# Footer
# -----------------------------------------------------------------------------
#DISCLAIMER_TEXT = "Disclaimer: The results provided in this atlas are for information purposes only. They are not official statistics and should not be used as the sole basis for decision-making."
DISCLAIMER_TEXT = (
    "Disclaimer: This atlas is intended for <strong>informational and research purposes only</strong>. "
    "The results presented are <strong>not official statistics</strong> and should not be considered "
    "as definitive or used as the sole basis for decision-making."
)


# Footer in app
st.markdown("---")
#st.markdown(f"<small>{DISCLAIMER_TEXT}</small>", unsafe_allow_html=True)
st.markdown(
    f"""
    <div style="background-color:#f8f9fa; padding:10px; border-radius:8px; font-size:0.9em; color:#444;">
        {DISCLAIMER_TEXT}
    </div>
    """,
    unsafe_allow_html=True
)

st.markdown(f"<small>{FOOTER_TEXT}</small>", unsafe_allow_html=True)

# =============================================================================
# END
# =============================================================================
