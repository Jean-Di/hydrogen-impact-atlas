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
st.set_page_config(page_title="Interactive Atlas", layout="wide")

SHAPEFILE_PATH = "Shape_file_10m/ne_10m_admin_0_countries.shp"  # adapte si nécessaire
EXCEL_PATH = "GHI_CoinR.xlsx" 
SCENARIO_SHEETS = ["iData_Short", "iData_Mid", "iData_Long"]

# ----------------------
# Helpers
# ----------------------
@st.cache_data(show_spinner=False)
def load_world(shapefile_path):
    # on garde SOV_A3 (ISO3) et ADMIN (country name)
    gdf = gpd.read_file(shapefile_path)
    # vérifier colonnes
    if "SOV_A3" not in gdf.columns:
        raise ValueError("Le shapefile doit contenir la colonne 'SOV_A3' (ISO3).")
    if "ADMIN" not in gdf.columns:
        # fallback to name-like column
        pass
    gdf = gdf[["SOV_A3", "ADMIN", "geometry"]].rename(columns={"SOV_A3": "ISO3", "ADMIN": "Country"})
    return gdf

@st.cache_data(show_spinner=False)
def load_scenarios(excel_path, sheets):
    # charge uniquement les scénarios listés (ignore les autres sheets)
    data = pd.read_excel(excel_path, sheet_name=sheets)
    # standardise noms de colonnes : on s'attend à uCode, uName ou uCode -> ISO3, uName -> Country
    for k, df in data.items():
        df.columns = [c.strip() for c in df.columns]
        # renommer colonnes si nécessaires
        rename_map = {}
        low = {c.lower(): c for c in df.columns}
        if "ucode" in low:
            rename_map[low["ucode"]] = "ISO3"
        elif "uCode".lower() in low:  # fallback
            rename_map[low["ucode"]] = "ISO3"
        elif "code" in low:
            rename_map[low["code"]] = "ISO3"
        
        df = df.rename(columns=rename_map)
        data[k] = df
    return data

def classify_three(series):
    """
    Return categorical series with values: 'Low','Medium','High' (strings),
    computed by quantiles (terciles). Handles constant series.
    """
    s = series.dropna().astype(float)
    if s.empty:
        return pd.Series(["NoData"] * len(series), index=series.index)
    # if all same value -> mark as Medium
    if s.nunique() == 1:
        return pd.Series(["Medium" if not pd.isna(x) else "NoData" for x in series], index=series.index)
    try:
        cats = pd.qcut(s, q=3, labels=["Low", "Medium", "High"])
    except Exception:
        # fallback to cut (equal width)
        cats = pd.cut(s, bins=3, labels=["Low", "Medium", "High"])
    # reindex to original index, fill NaN with NoData
    out = pd.Series(index=series.index, dtype="object")
    out.loc[cats.index] = cats.astype(str)
    out = out.fillna("NoData")
    return out

def category_to_color(cat):
    # mapping demanded: Blue small, White mid, Orange large
    mapping = {
        "Low": "blue",
        "Medium": "white",
        "High": "orange",
        "NoData": "lightgrey"
    }
    return mapping.get(cat, "lightgrey")

def category_to_label(cat):
    # adjectives to write in PDF (French: tu as utilisé anglais précédemment; j'utilise anglais here)
    # change these strings if you want French adjectives.
    mapping = {
        "Low": "Low",
        "Medium": "Moderate",
        "High": "High",
        "NoData": "No data"
    }
    return mapping.get(cat, "No data")

def make_pdf_for_country(country, country_code, scenarios_dict, indicator_classes):
    """
    Create a PDF bytes object summarizing qualitative profile for the country.
    - scenarios_dict: dict of name -> dataframe
    - indicator_classes: dict scenario -> dataframe with 'ISO3' and 'category' per indicator (we will compute inside)
    """
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    width, height = A4

    # header
    c.setFont("Helvetica-Bold", 14)
    c.drawString(20 * mm, height - 20 * mm, f"Country profile — {country} ({country_code})")
    c.setFont("Helvetica", 10)
    c.drawString(20 * mm, height - 28 * mm, "Qualitative profile based on color classes (Blue=Low, White=Moderate, Orange=High)")
    y = height - 36 * mm

    # For each scenario, list indicators and qualitative adjective
    for scen_name, df in scenarios_dict.items():
        c.setFont("Helvetica-Bold", 12)
        c.drawString(20 * mm, y, f"Scenario: {scen_name}")
        y -= 6 * mm
        c.setFont("Helvetica", 10)

        # determine categories for this scenario per indicator (indicator_classes param)
        # indicator_classes[scen_name] expected to be a dict: ind -> series of categories on ISO3 index
        scen_classes = indicator_classes.get(scen_name, {})
        if not scen_classes:
            c.drawString(22 * mm, y, "No indicators or no data.")
            y -= 8 * mm
            continue

        for ind_name, series in scen_classes.items():
            # try to get category for this country's ISO3
            cat = series.get(country_code, "NoData")
            label = category_to_label(cat)
            # draw line: IndicatorName : Label (Color)
            color_name = category_to_color(cat)
            text = f"- {ind_name}: {label} ({color_name})"
            c.drawString(22 * mm, y, text)
            y -= 6 * mm
            if y < 30 * mm:
                c.showPage()
                y = height - 20 * mm
        y -= 4 * mm

    # legend at bottom
    c.setFont("Helvetica-Bold", 11)
    c.drawString(20 * mm, 30 * mm, "Legend:")
    c.setFont("Helvetica", 10)
    c.drawString(25 * mm, 24 * mm, "Blue = Low")
    c.drawString(25 * mm, 18 * mm, "White = Moderate")
    c.drawString(25 * mm, 12 * mm, "Orange = High")
    c.save()
    buf.seek(0)
    return buf.getvalue()

# ----------------------
# Load data
# ----------------------
with st.spinner("Loading data..."):
    world = load_world(SHAPEFILE_PATH)
    world_json = json.loads(world.to_json())
    scenarios = load_scenarios(EXCEL_PATH, SCENARIO_SHEETS)

# ----------------------
# Sidebar: controls
# ----------------------
st.sidebar.title("Controls")
scenario_choice = st.sidebar.selectbox("Scenario", SCENARIO_SHEETS, index=0)
df = scenarios[scenario_choice]

# detect indicator columns (exclude ID/name columns)
possible_id_cols = [c for c in df.columns if c.lower() in ("is03","iso3","ucode","code","country","uname","uName","uName".lower())]
# more robust: prefer ISO3 or uCode; but we standardized earlier if possible
id_col = None
for candidate in ["ISO3", "uCode", "uCode".lower(), "uCode".upper(), "Code", "code"]:
    if candidate in df.columns:
        id_col = candidate
        break
# fallback to first column as ID
if id_col is None:
    # try common names
    if any(c.lower() == "ucode" for c in df.columns):
        id_col = [c for c in df.columns if c.lower() == "ucode"][0]
    elif any(c.lower() == "iso3" for c in df.columns):
        id_col = [c for c in df.columns if c.lower() == "iso3"][0]
    elif len(df.columns) >= 2:
        id_col = df.columns[0]
    else:
        id_col = df.columns[0]

# find name column
name_col = None
for cand in ["uName", "uName".lower(), "Country", "country", "uName"]:
    if cand in df.columns:
        name_col = cand
        break
if name_col is None:
    # fallback to second column if exists
    name_col = df.columns[1] if len(df.columns) > 1 else id_col

# build list of indicator columns (exclude id/name)
indicators = [c for c in df.columns if c not in [id_col, name_col]]
if not indicators:
    st.error("Aucun indicateur trouvé dans la feuille sélectionnée. Vérifie les colonnes du fichier Excel.")
    st.stop()

indicator_choice = st.sidebar.selectbox("Indicator", indicators)

# big title and legend top
st.title("Green Hydrogen Impact Atlas")
st.markdown("**Map view** (large) — Blue = low, White = medium, Orange = high")

# ----------------------
# Compute categories for chosen scenario & indicator
# ----------------------
# Ensure ISO codes are strings uppercase
for k, dff in scenarios.items():
    if "ISO3" in dff.columns:
        dff["ISO3"] = dff["ISO3"].astype(str).str.upper()
    else:
        # try to rename if uCode present
        low = {c.lower(): c for c in dff.columns}
        if "ucode" in low:
            dff.rename(columns={low["ucode"]: "ISO3"}, inplace=True)
            dff["ISO3"] = dff["ISO3"].astype(str).str.upper()
        else:
            # not ideal: try to build ISO3 from uName? we skip
            pass
    scenarios[k] = dff

# build merged GeoDataFrame for current scenario
# rename world ISO3 column already to "ISO3"
merged = world.merge(scenarios[scenario_choice], left_on="ISO3", right_on="ISO3", how="left")

# compute classification for current indicator (and also prepare for all scenarios for PDF)
indicator_classes_all = {}
for scen_name, dframe in scenarios.items():
    if indicator_choice in dframe.columns:
        cat_series = classify_three(dframe[indicator_choice].astype(float))
        # index by ISO3 for quick lookup
        s_by_iso = pd.Series(cat_series.values, index=dframe["ISO3"].astype(str).str.upper())
        indicator_classes_all[scen_name] = {indicator_choice: s_by_iso}
    else:
        indicator_classes_all[scen_name] = {indicator_choice: pd.Series(dtype=object)}

# Now for display we want category column on merged
if indicator_choice in merged.columns:
    merged["Category"] = classify_three(merged[indicator_choice].astype(float)).values
else:
    # if indicator absent, set NoData
    merged["Category"] = ["NoData"] * len(merged)

# build color mapping (plotly expects discrete colors)
color_map = {"Low": "blue", "Medium": "white", "High": "orange", "NoData": "lightgrey"}

# For Plotly discrete coloring we pass color=Category and color_discrete_map
fig = px.choropleth(
    merged,
    geojson=world_json,
    locations="ISO3",
    featureidkey="properties.ISO3",
    color="Category",
    category_orders={"Category": ["High", "Medium", "Low", "NoData"]},
    color_discrete_map=color_map,
    hover_name=name_col if name_col in merged.columns else "Country",
    projection="mercator",
    title=f"{indicator_choice} — {scenario_choice}"
)
fig.update_geos(fitbounds="locations", visible=False)
fig.update_layout(margin={"r":0,"t":40,"l":0,"b":0}, paper_bgcolor="white")

# Show big map (use container width)
st.plotly_chart(fig, use_container_width=True)

# Legend (custom)
st.markdown("**Legend:**")
st.markdown("- 🔵 **Blue** = Low")
st.markdown("- ⚪ **White** = Moderate")
st.markdown("- 🟠 **Orange** = High")
st.markdown("- ⚫ **Grey** = No data")

# ----------------------
# Country selection & PDF download
# ----------------------
st.sidebar.markdown("---")
st.sidebar.header("Country profile")
# Choose country by name present in the selected scenario data
country_names = list(scenarios[scenario_choice][name_col].dropna().unique())
country_names_sorted = sorted(country_names)
selected_country = st.sidebar.selectbox("Select a country", country_names_sorted)

# Find ISO for selected country (best-effort)
selected_iso = None
df_sel = scenarios[scenario_choice]
row = df_sel[df_sel[name_col] == selected_country]
if not row.empty and "ISO3" in row.columns:
    selected_iso = str(row["ISO3"].iloc[0]).upper()

if st.sidebar.button("Download PDF profile"):
    if not selected_country:
        st.sidebar.warning("Choose a country first.")
    else:
        # Create PDF bytes using indicator_classes_all
        pdf_bytes = make_pdf_for_country(selected_country, selected_iso or selected_country, scenarios, indicator_classes_all)
        st.sidebar.download_button("Download PDF", data=pdf_bytes, file_name=f"profile_{selected_country}.pdf", mime="application/pdf")
