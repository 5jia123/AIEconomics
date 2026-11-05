import streamlit as st
import yfinance as yf
from fredapi import Fred
import pandas as pd
import datetime

# --- Gemini SDK 相容性匯入 (相容 google-genai 與 google.generativeai) ---
GENAI_MODE = None
try:
    from google import genai
    GENAI_MODE = "google-genai"
except ImportError:
    try:
        import google.generativeai as legacy_genai
        GENAI_MODE = "google-generativeai"
    except ImportError:
        GENAI_MODE = None

# --- 1. 頁面基本配置 ---
st.set_page_config(
    page_title="全球總經與美債倒掛監控儀表板",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="collapsed",
)

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
    </style>
    """,
    unsafe_allow_html=True,
)

# --- 2. 監控代碼字典設定 ---
YFINANCE_CONFIG = {
    "S&P 500": "^GSPC",
    "黃金現貨 (GC=F)": "GC=F",
    "原油 (WTI)": "CL=F",
    "美元指數": "DX-Y.NYB",
}

FRED_CONFIG = {
    "美國 10 年期國債殖利率": "DGS10",
    "日本 10 年期國債殖利率": "IRLTLT01JPM156N",
    "美國 CPI (年增率)": "CPIAUCSL",
    "美國失業率": "UNRATE",
}

# --- 3. 金鑰讀取 (Secrets 優先，側邊欄備用) ---
FRED_API_KEY = st.secrets.get("FRED_API_KEY", "")
GEMINI_API_KEY = st.secrets.get("GEMINI_API_KEY", "")

with st.sidebar:
    st.header("API 金鑰設定")
    if not FRED_API_KEY:
        FRED_API_KEY = st.text_input("FRED API Key", type="password")
    if not GEMINI_API_KEY:
        GEMINI_API_KEY = st.text_input("Gemini API Key", type="password")

# --- 4. 資料快取與獲取函式 ---
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
        except Exception as e:
            results[label] = {"error": str(e)}
    return results

@st.cache_data(ttl=3600)
def get_macro_and_spread_data(series_dict, api_key):
    results = {"metrics": {}, "spread_df": None, "error": None}
    if not api_key:
        return results

    try:
        fred = Fred(api_key=api_key)
    except Exception as e:
        results["error"] = f"FRED 初始化失敗: {e}"
        return results

    # 4-1. 抓取一般總經指標
    for label, series_id in series_dict.items():
        try:
            raw_series = fred.get_series(series_id)
            series = pd.to_numeric(raw_series, errors="coerce").dropna()
            if series.empty or len(series) < 2:
                results["metrics"][label] = {"error": "無資料"}
                continue

            if series_id == "CPIAUCSL":
                if len(series) >= 13:
                    latest_cpi = series.iloc[-1]
                    prev_year_cpi = series.iloc[-13]
                    yoy = ((latest_cpi - prev_year_cpi) / prev_year_cpi) * 100

                    prev_month_cpi = series.iloc[-2]
                    prev_month_year_cpi = series.iloc[-14]
                    prev_yoy = ((prev_month_cpi - prev_month_year_cpi) / prev_month_year_cpi) * 100
                    diff_yoy = yoy - prev_yoy

                    results["metrics"][label] = {
                        "value": yoy,
                        "diff": diff_yoy,
                        "date": series.index[-1].strftime("%Y-%m"),
                        "unit": "%",
                        "history": ((series - series.shift(12)) / series.shift(12) * 100).dropna(),
                    }
            else:
                latest = series.iloc[-1]
                prev = series.iloc[-2]
                diff = latest - prev
                is_monthly = series_id in ["IRLTLT01JPM156N", "UNRATE"]
                date_fmt = "%Y-%m" if is_monthly else "%Y-%m-%d"

                results["metrics"][label] = {
                    "value": latest,
                    "diff": diff,
                    "date": series.index[-1].strftime(date_fmt),
                    "unit": "%",
                    "history": series,
                }
        except Exception as e:
            results["metrics"][label] = {"error": str(e)}

    # 4-2. 抓取並計算 10Y-2Y 美債利差
    try:
        dgs10 = pd.to_numeric(fred.get_series("DGS10"), errors="coerce")
        dgs2 = pd.to_numeric(fred.get_series("DGS2"), errors="coerce")
        df_spread = pd.DataFrame({"10Y": dgs10, "2Y": dgs2}).dropna()
        df_spread["Spread"] = df_spread["10Y"] - df_spread["2Y"]
        results["spread_df"] = df_spread
    except Exception as e:
        results["error"] = f"利差計算失敗: {e}"

    return results

# --- 5. 執行載入 ---
with st.spinner("載入即時市場、殖利率與總經數據中..."):
    yf_data = get_yfinance_metrics(YFINANCE_CONFIG)
    fred_data = get_macro_and_spread_data(FRED_CONFIG, FRED_API_KEY)

# --- 6. 頁面標頭 ---
st.title("全球金融市場與宏觀利率儀表板")
st.caption(f"最後更新時間: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
st.write("---")

# --- 7. 第一區塊：核心市場指標 ---
st.subheader("大宗商品與關鍵市場資產")
cols_yf = st.columns(4)
for idx, (label, sym) in enumerate(YFINANCE_CONFIG.items()):
    col = cols_yf[idx % 4]
    item = yf_data.get(label)
    with col:
        if item and "price" in item:
            st.metric(
                label=label,
                value=f"${item['price']:,.2f}" if "指數" not in label else f"{item['price']:,.2f}",
                delta=f"{item['change']:+.2f}%",
            )
        else:
            err = item.get("error", "無資料") if item else "載入失敗"
            st.metric(label=label, value="N/A", delta=err)

st.write("")

# --- 8. 第二區塊：全球國債殖利率、失業率與 CPI ---
st.subheader("全球國債殖利率與總經數據 (FRED)")
if not FRED_API_KEY:
    st.info("💡 請在左側側邊欄輸入 FRED API Key，或在 `.streamlit/secrets.toml` 配置 `FRED_API_KEY`。")
else:
    cols_fred = st.columns(4)
    for idx, (label, series_id) in enumerate(FRED_CONFIG.items()):
        col = cols_fred[idx % 4]
        item = fred_data["metrics"].get(label)
        with col:
            if item and "value" in item:
                st.metric(
                    label=f"{label} ({item['date']})",
                    value=f"{item['value']:.2f}{item['unit']}",
                    delta=f"{item['diff']:+.2f}{item['unit']}",
                )
            else:
                err = item.get("error", "無資料") if item else "無資料"
                st.metric(label=label, value="N/A", delta=err)

st.write("---")

# --- 9. 第三區塊：10Y-2Y 倒掛警示燈號 ---
st.subheader("美國 10Y - 2Y 公債殖利率利差與倒掛監控")
spread_df = fred_data.get("spread_df")

latest_spread = None
latest_10y = None
latest_2y = None

if spread_df is not None and not spread_df.empty:
    latest_spread = spread_df["Spread"].iloc[-1]
    prev_spread = spread_df["Spread"].iloc[-2]
    diff_spread = latest_spread - prev_spread
    latest_date = spread_df.index[-1].strftime("%Y-%m-%d")
    latest_10y = spread_df["10Y"].iloc[-1]
    latest_2y = spread_df["2Y"].iloc[-1]

    col_s1, col_s2, col_s3 = st.columns(3)
    with col_s1:
        st.metric(
            label=f"10Y-2Y 利差 ({latest_date})",
            value=f"{latest_spread:+.2f}%",
            delta=f"{diff_spread:+.2f}% (前一日)",
        )
    with col_s2:
        st.metric("10 年期美債 (DGS10)", f"{latest_10y:.2f}%")
    with col_s3:
        st.metric("2 年期美債 (DGS2)", f"{latest_2y:.2f}%")

    st.write("")

    if latest_spread < 0:
        st.error(
            f"🚨 **【經濟衰退警訊：殖利率曲線倒掛 (Yield Curve Inversion)】**\n\n"
            f"當前利差為 **{latest_spread:+.2f}% (< 0)**。短期利率高於長期利率，歷史上常作為美國經濟衰退的領先指標。"
        )
    else:
        st.success(
            f"✅ **【殖利率曲線正常 (Normal Curve)】**\n\n"
            f"當前利差為 **{latest_spread:+.2f}% (≥ 0)**，目前未出現倒掛現象。"
        )
else:
    st.info("尚無利差數據（請檢查 FRED API Key 設定）。")

st.write("---")

# --- 10. 第四區塊：趨勢歷史走勢圖 ---
st.subheader("趨勢歷史走勢圖")
tab1, tab2, tab3, tab4 = st.tabs([
    "黃金現貨 (GC=F)",
    "日本 10 年期國債殖利率",
    "10Y-2Y 利差歷史 (含倒掛基準線)",
    "美國 CPI 年增率"
])

with tab1:
    gold = yf_data.get("黃金現貨 (GC=F)")
    if gold and "history" in gold:
        st.line_chart(pd.DataFrame({"黃金收盤價 (USD)": gold["history"]}))

with tab2:
    jgb = fred_data["metrics"].get("日本 10 年期國債殖利率")
    if jgb and "history" in jgb:
        st.line_chart(pd.DataFrame({"日本 10Y 公債殖利率 (%)": jgb["history"].tail(60)}))

with tab3:
    if spread_df is not None and not spread_df.empty:
        recent_spread = spread_df.tail(252 * 5)
        chart_data = pd.DataFrame(
            {
                "10Y-2Y 利差 (%)": recent_spread["Spread"],
                "倒掛臨界線 (0%)": 0.0,
            },
            index=recent_spread.index,
        )
        st.line_chart(chart_data, color=["#1f77b4", "#ff4b4b"])
    else:
        st.info("無利差歷史資料。")

with tab4:
    cpi = fred_data["metrics"].get("美國 CPI (年增率)")
    if cpi and "history" in cpi:
        st.line_chart(pd.DataFrame({"美國 CPI YoY (%)": cpi["history"].tail(60)}))

st.write("---")

# --- 11. 第五區塊：AI 總體經濟摘要生成 ---
st.subheader("🤖 AI 總體經濟即時短評")

cpi_item = fred_data["metrics"].get("美國 CPI (年增率)")
unrate_item = fred_data["metrics"].get("美國失業率")

cpi_val = f"{cpi_item['value']:.2f}%" if cpi_item and "value" in cpi_item else "無資料"
unrate_val = f"{unrate_item['value']:.2f}%" if unrate_item and "value" in unrate_item else "無資料"
us10y_val = f"{latest_10y:.2f}%" if latest_10y is not None else "無資料"
spread_val = f"{latest_spread:+.2f}%" if latest_spread is not None else "無資料"

if st.button("產生 AI 總體經濟摘要", type="primary"):
    if GENAI_MODE is None:
        st.error("環境中尚未安裝 Gemini SDK，請在專案的 `requirements.txt` 中加入 `google-genai` 並重新部署。")
    elif not GEMINI_API_KEY:
        st.error("請先設定 `GEMINI_API_KEY`（可於左側側邊欄輸入或在 `.streamlit/secrets.toml` 配置）。")
    else:
        with st.spinner("AI 正在分析最新總經數據..."):
            prompt = f"""
你是一位專業的宏觀經濟分析師。請根據以下最新抓取的總經指標數據，為投資人撰寫一份約 200 字的客觀精準短評。

【最新宏觀指標】
- 美國 CPI 年增率: {cpi_val}
- 美國失業率: {unrate_val}
- 美國 10 年期國債殖利率: {us10y_val}
- 10Y-2Y 國債利差: {spread_val}

【撰寫要求】
1. 評估目前通膨與就業市場的平衡狀況。
2. 解讀殖利率與利差反映的市場降息/衰退預期。
3. 繁體中文撰寫，字數控制在 200 字以內，語氣專業洗鍊。
"""
            try:
                ai_text = ""
                if GENAI_MODE == "google-genai":
                    client = genai.Client(api_key=GEMINI_API_KEY)
                    response = client.models.generate_content(
                        model="gemini-2.5-flash",
                        contents=prompt,
                    )
                    ai_text = response.text
                elif GENAI_MODE == "google-generativeai":
                    legacy_genai.configure(api_key=GEMINI_API_KEY)
                    model = legacy_genai.GenerativeModel("gemini-1.5-flash")
                    response = model.generate_content(prompt)
                    ai_text = response.text

                st.markdown(
                    f"""
                    <div style="background-color: #f1f3f5; padding: 16px; border-radius: 8px; border-left: 4px solid #1f77b4; line-height: 1.6;">
                    {ai_text}
                    </div>
                    """,
                    unsafe_allow_html=True,
                )
            except Exception as e:
                st.error(f"生成摘要失敗: {e}")
