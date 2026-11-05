import streamlit as st
import yfinance as yf
from fredapi import Fred
import pandas as pd
import datetime

# --- 頁面基本設定 ---
st.set_page_config(
    page_title="全球總經與市場監控儀表板",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# 自訂 CSS 提升視覺質感與 KPI 卡片間距
st.markdown(
    """
    <style>
    div[data-testid="stMetricValue"] {
        font-size: 1.8rem;
        font-weight: 700;
    }
    div[data-testid="stMetricDelta"] {
        font-size: 0.95rem;
    }
    .metric-card {
        background-color: #f8f9fa;
        border-radius: 8px;
        padding: 12px;
        border: 1px solid #e9ecef;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

# --- 字典設定 ---
YFINANCE_CONFIG = {
    "S&P 500": "^GSPC",
    "黃金現貨 (GC=F)": "GC=F",
    "原油 (WTI)": "CL=F",
    "美元指數": "DX-Y.NYB",
}

FRED_CONFIG = {
    "美國 10 年期國債殖利率": "DGS10",
    "日本 10 年期國債殖利率": "IRLTLT01JPM156N",
    "美國 CPI (年增率基準)": "CPIAUCSL",
}

# --- 初始化 FRED 客戶端 ---
FRED_API_KEY = st.secrets.get("FRED_API_KEY", None)

# --- 資料讀取與快取函式 ---
@st.cache_data(ttl=3600)
def get_yfinance_metrics(ticker_dict):
    results = {}
    for label, sym in ticker_dict.items():
        try:
            ticker = yf.Ticker(sym)
            hist = ticker.history(period="6mo")
            if not hist.empty and len(hist) >= 2:
                latest = hist["Close"].iloc[-1]
                prev = hist["Close"].iloc[-2]
                pct_change = ((latest - prev) / prev) * 100
                results[label] = {
                    "price": latest,
                    "change": pct_change,
                    "history": hist["Close"],
                }
            elif not hist.empty:
                results[label] = {
                    "price": hist["Close"].iloc[-1],
                    "change": 0.0,
                    "history": hist["Close"],
                }
        except Exception:
            results[label] = None
    return results

@st.cache_data(ttl=86400)
def get_fred_metrics(series_dict, api_key):
    results = {}
    if not api_key:
        return results

    fred = Fred(api_key=api_key)
    for label, series_id in series_dict.items():
        try:
            series = fred.get_series(series_id).dropna()
            if not series.empty and len(series) >= 2:
                latest = series.iloc[-1]
                prev = series.iloc[-2]
                diff = latest - prev
                results[label] = {
                    "value": latest,
                    "diff": diff,
                    "date": series.index[-1].strftime("%Y-%m-%d"),
                    "history": series,
                }
        except Exception:
            results[label] = None
    return results

# --- 執行資料載入 ---
with st.spinner("載入即時市場與總經指標中..."):
    yf_data = get_yfinance_metrics(YFINANCE_CONFIG)
    fred_data = get_fred_metrics(FRED_CONFIG, FRED_API_KEY)

# --- 頁面標頭 ---
st.title("全球金融市場與宏觀利率儀表板")
st.caption(f"最後更新時間: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

st.write("---")

# --- 第一區塊：核心市場指標 (含黃金現貨 GC=F) ---
st.subheader("大宗商品與關鍵市場資產")
cols_yf = st.columns(4)

for idx, (label, sym) in enumerate(YFINANCE_CONFIG.items()):
    col = cols_yf[idx % 4]
    item = yf_data.get(label)
    with col:
        if item:
            st.metric(
                label=label,
                value=f"${item['price']:,.2f}" if "指數" not in label else f"{item['price']:,.2f}",
                delta=f"{item['change']:+.2f}%",
            )
        else:
            st.metric(label=label, value="N/A", delta="無資料")

st.write("")

# --- 第二區塊：全球主要國債殖利率 (含日本 10 年期國債) ---
st.subheader("全球國債殖利率與總經數據")
if not FRED_API_KEY:
    st.warning("尚未在 `.streamlit/secrets.toml` 中配置 `FRED_API_KEY`，無法抓取 FRED 殖利率指標。")
else:
    cols_fred = st.columns(3)
    for idx, (label, series_id) in enumerate(FRED_CONFIG.items()):
        col = cols_fred[idx % 3]
        item = fred_data.get(label)
        with col:
            if item:
                unit = "%" if "殖利率" in label else ""
                st.metric(
                    label=f"{label} ({item['date']})",
                    value=f"{item['value']:.3f}{unit}",
                    delta=f"{item['diff']:+.3f}{unit}",
                )
            else:
                st.metric(label=label, value="N/A", delta="無資料")

st.write("---")

# --- 第三區塊：走勢折線圖 (兩欄佈局) ---
st.subheader("趨勢走勢圖")
tab1, tab2 = st.tabs(["黃金現貨 (GC=F)", "日本 10 年期國債殖利率"])

with tab1:
    gold_data = yf_data.get("黃金現貨 (GC=F)")
    if gold_data and not gold_data["history"].empty:
        chart_df = pd.DataFrame({"黃金結算價 (USD)": gold_data["history"]})
        st.line_chart(chart_df)
    else:
        st.info("黃金歷史數據加載中或無法讀取。")

with tab2:
    jgb_data = fred_data.get("日本 10 年期國債殖利率")
    if jgb_data and not jgb_data["history"].empty:
        # 取近 5 年月資料，避免圖表過度壓縮
        recent_history = jgb_data["history"].tail(60)
        chart_df = pd.DataFrame({"日本 10Y 公債殖利率 (%)": recent_history})
        st.line_chart(chart_df)
    else:
        st.info("日本 10 年期國債殖利率數據加載中或 FRED 金鑰未配置。")
