import datetime
import re
import time
from urllib.parse import quote

import feedparser
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
import yfinance as yf
from fredapi import Fred

# ==========================================
# 0. Gemini SDK 相容性匯入 (優先使用新版 google-genai)
# ==========================================
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

# ==========================================
# 1. 頁面配置與樣式優化
# ==========================================
st.set_page_config(
    page_title="全球總經、金融市場與 AI 鷹鴿情緒儀表板",
    page_icon="🌐",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
    <style>
    div[data-testid="stMetricValue"] {
        font-size: 1.65rem;
        font-weight: 700;
    }
    div[data-testid="stMetricDelta"] {
        font-size: 0.9rem;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

# ==========================================
# 2. 金鑰安全讀取與側邊欄控制
# ==========================================
def get_secret_safe(key_name: str) -> str:
    try:
        return st.secrets.get(key_name, "")
    except Exception:
        return ""

FRED_API_KEY = get_secret_safe("FRED_API_KEY")
GEMINI_API_KEY = get_secret_safe("GEMINI_API_KEY")

with st.sidebar:
    st.header("⚙️ 系統設定與金鑰")

    if FRED_API_KEY:
        st.success("✅ 已從 Secrets 讀取 FRED Key")
    else:
        FRED_API_KEY = st.text_input("請輸入 FRED API Key:", type="password")

    if GEMINI_API_KEY:
        st.success("✅ 已從 Secrets 讀取 Gemini Key")
    else:
        GEMINI_API_KEY = st.text_input("請輸入 Gemini API Key:", type="password")

    st.write("---")
    if st.button("🔄 強制刷新所有快取數據", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

    st.caption(f"SDK 狀態: `{GENAI_MODE or '未安裝'}`")

# ==========================================
# 3. 數據抓取函式 (FRED, yfinance, RSS 新聞)
# ==========================================
YFINANCE_CONFIG = {
    "S&P 500": "^GSPC",
    "台灣加權指數": "^TWII",
    "黃金現貨": "GC=F",
    "布蘭特原油": "BZ=F",
    "美元/台幣匯率": "TWD=X",
}

@st.cache_data(ttl=600)
def get_market_data(ticker_dict: dict):
    metrics = {}
    history_dict = {}
    for label, sym in ticker_dict.items():
        try:
            hist = yf.Ticker(sym).history(period="1y")["Close"].dropna()
            if len(hist) >= 2:
                latest = float(hist.iloc[-1])
                prev = float(hist.iloc[-2])
                pct_change = ((latest - prev) / prev) * 100
                metrics[label] = {"price": latest, "change": pct_change}
                history_dict[label] = hist
            elif len(hist) == 1:
                metrics[label] = {"price": float(hist.iloc[-1]), "change": 0.0}
                history_dict[label] = hist
        except Exception as e:
            metrics[label] = {"error": str(e)}

    history_df = pd.DataFrame(history_dict).ffill() if history_dict else pd.DataFrame()
    return metrics, history_df


@st.cache_data(ttl=3600)
def get_fred_all_data(api_key: str):
    """一次性抓取總經指標與美債 10Y-2Y 利差，減少重複請求"""
    res = {"metrics": {}, "history_df": pd.DataFrame(), "spread_df": pd.DataFrame(), "error": None}
    if not api_key:
        return res

    try:
        fred = Fred(api_key=api_key)
        series_map = {
            "CPIAUCSL": fred.get_series("CPIAUCSL"),
            "DGS10": fred.get_series("DGS10"),
            "DGS2": fred.get_series("DGS2"),
            "UNRATE": fred.get_series("UNRATE"),
            "FEDFUNDS": fred.get_series("FEDFUNDS"),
            "JGB10Y": fred.get_series("IRLTLT01JPM156N"),
        }
        clean = {k: pd.to_numeric(v, errors="coerce").dropna() for k, v in series_map.items()}

        # 1) 計算美國 CPI 年增率 (YoY %)
        cpi = clean["CPIAUCSL"]
        if len(cpi) >= 14:
            cpi_yoy_series = ((cpi - cpi.shift(12)) / cpi.shift(12) * 100).dropna()
            res["metrics"]["美國 CPI 年增率"] = {
                "value": float(cpi_yoy_series.iloc[-1]),
                "diff": float(cpi_yoy_series.iloc[-1] - cpi_yoy_series.iloc[-2]),
                "date": cpi_yoy_series.index[-1].strftime("%Y-%m"),
            }
        else:
            cpi_yoy_series = pd.Series(dtype=float)

        # 2) 其他單一指標處理
        single_specs = [
            ("10年期美債殖利率", "DGS10", "%Y-%m-%d"),
            ("聯邦基金利率", "FEDFUNDS", "%Y-%m"),
            ("美國失業率", "UNRATE", "%Y-%m"),
            ("日本 10Y 殖利率", "JGB10Y", "%Y-%m"),
        ]
        for label, code, date_fmt in single_specs:
            s = clean[code]
            if len(s) >= 2:
                res["metrics"][label] = {
                    "value": float(s.iloc[-1]),
                    "diff": float(s.iloc[-1] - s.iloc[-2]),
                    "date": s.index[-1].strftime(date_fmt),
                }

        # 3) 計算 10Y - 2Y 美債利差
        spread_df = pd.DataFrame({"10Y": clean["DGS10"], "2Y": clean["DGS2"]}).dropna()
        spread_df["Spread"] = spread_df["10Y"] - spread_df["2Y"]
        res["spread_df"] = spread_df

        # 4) 整理總經趨勢圖用 DataFrame (近 3 年)
        hist_df = pd.DataFrame({
            "美國 CPI 年增率 (%)": cpi_yoy_series,
            "10年期美債殖利率 (%)": clean["DGS10"],
            "聯邦基金利率 (%)": clean["FEDFUNDS"],
            "美國失業率 (%)": clean["UNRATE"],
            "日本 10Y 殖利率 (%)": clean["JGB10Y"],
        }).ffill().tail(365 * 3)
        res["history_df"] = hist_df

    except Exception as e:
        res["error"] = str(e)

    return res


@st.cache_data(ttl=1800)
def fetch_google_news(keyword: str = "Federal Reserve Inflation", max_items: int = 8):
    """使用 quote 安全轉碼，並依據中/英文關鍵字自動切換對應新聞頻道"""
    safe_keyword = quote(keyword.strip())
    has_chinese = bool(re.search(r"[\u4e00-\u9fff]", keyword))
    if has_chinese:
        url = f"https://news.google.com/rss/search?q={safe_keyword}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
    else:
        url = f"https://news.google.com/rss/search?q={safe_keyword}&hl=en-US&gl=US&ceid=US:en"

    feed = feedparser.parse(url)
    articles = []
    for entry in feed.entries[:max_items]:
        articles.append({
            "title": entry.get("title", "無標題"),
            "link": entry.get("link", ""),
            "published": entry.get("published", ""),
        })
    return articles


# ==========================================
# 4. Gemini AI 生成引擎 (支援多模型退避與備援)
# ==========================================
def call_gemini_with_fallback(prompt_text: str, api_key: str):
    candidate_models = ["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.0-flash"]
    last_err = None

    if GENAI_MODE == "google-genai":
        client = genai.Client(api_key=api_key)
        for model_name in candidate_models:
            for _ in range(2):
                try:
                    res = client.models.generate_content(
                        model=model_name,
                        contents=prompt_text,
                    )
                    return res.text, model_name
                except Exception as e:
                    last_err = e
                    if "503" in str(e) or "429" in str(e) or "high demand" in str(e).lower():
                        time.sleep(1.0)
                        continue
                    break

    elif GENAI_MODE == "google-generativeai":
        legacy_genai.configure(api_key=api_key)
        for model_name in candidate_models:
            try:
                model = legacy_genai.GenerativeModel(model_name)
                res = model.generate_content(prompt_text)
                return res.text, model_name
            except Exception as e:
                last_err = e
                time.sleep(1.0)

    raise last_err if last_err else RuntimeError("環境未安裝 Gemini SDK 或所有模型皆無回應")


# ==========================================
# 5. 主畫面渲染邏輯
# ==========================================
st.title("🌐 總體經濟、金融指標與 AI 鷹鴿情緒儀表板")
st.caption(f"系統更新時間: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

with st.spinner("同步全球金融市場與總經數據中..."):
    yf_metrics, yf_history = get_market_data(YFINANCE_CONFIG)
    fred_res = get_fred_all_data(FRED_API_KEY)

# --- 第一區塊：金融市場即時行情 ---
st.subheader("📈 關鍵金融市場與大宗商品 (yfinance)")
cols_m = st.columns(len(YFINANCE_CONFIG))
for idx, (label, _) in enumerate(YFINANCE_CONFIG.items()):
    with cols_m[idx]:
        item = yf_metrics.get(label, {})
        if "price" in item:
            prefix = "" if ("指數" in label or "匯率" in label) else "$"
            st.metric(
                label=label,
                value=f"{prefix}{item['price']:,.2f}",
                delta=f"{item['change']:+.2f}%",
            )
        else:
            st.metric(label=label, value="N/A", delta=item.get("error", "讀取失敗"))

st.write("---")

# --- 第二區塊：FRED 總體經濟與美債倒掛監控 ---
st.subheader("🏛️ 總體經濟指標與美債殖利率曲線 (FRED)")
if not FRED_API_KEY:
    st.info("👈 請在左側側邊欄輸入您的 **FRED API Key**（或設定於 `.streamlit/secrets.toml`）以解鎖總經數據與倒掛監控。")
elif fred_res["error"]:
    st.error(f"FRED 數據讀取失敗，請檢查 API Key 是否正確。錯誤訊息: {fred_res['error']}")
else:
    # 2-1. 核心總經 KPI
    macro_keys = ["美國 CPI 年增率", "10年期美債殖利率", "聯邦基金利率", "美國失業率", "日本 10Y 殖利率"]
    cols_f = st.columns(len(macro_keys))
    for idx, k in enumerate(macro_keys):
        with cols_f[idx]:
            m = fred_res["metrics"].get(k)
            if m:
                st.metric(
                    label=f"{k} ({m['date']})",
                    value=f"{m['value']:.2f}%",
                    delta=f"{m['diff']:+.2f}%",
                )
            else:
                st.metric(label=k, value="N/A")

    st.write("")

    # 2-2. 10Y-2Y 倒掛警示卡片
    spread_df = fred_res["spread_df"]
    if not spread_df.empty:
        latest_spread = float(spread_df["Spread"].iloc[-1])
        diff_spread = float(spread_df["Spread"].iloc[-1] - spread_df["Spread"].iloc[-2])
        latest_date = spread_df.index[-1].strftime("%Y-%m-%d")

        c_sp1, c_sp2 = st.columns([1, 2.5])
        with c_sp1:
            st.metric(
                label=f"美債 10Y - 2Y 利差 ({latest_date})",
                value=f"{latest_spread:+.2f}%",
                delta=f"{diff_spread:+.2f}% (前一日)",
            )
        with c_sp2:
            if latest_spread < 0:
                st.error(
                    f"🚨 **【殖利率曲線倒掛警訊 (Yield Curve Inversion)】**\n\n"
                    f"當前 10Y-2Y 利差為 **{latest_spread:+.2f}% (< 0)**，短率高於長率，歷史上常反映緊縮壓力與衰退風險。"
                )
            else:
                st.success(
                    f"✅ **【殖利率曲線維持正斜率 (Normal Curve)】**\n\n"
                    f"當前 10Y-2Y 利差為 **{latest_spread:+.2f}% (≥ 0)**，長端利率高於短端，曲線結構正常。"
                )

st.write("---")

# --- 第三區塊：互動分頁（金融走勢 / 總經與利差 / NLP 新聞鷹鴿情緒分析） ---
tab1, tab2, tab3 = st.tabs([
    "📊 金融市場走勢 (Plotly)",
    "📉 總經與美債倒掛歷史",
    "📰 NLP 財經新聞與 AI 鷹鴿情緒分析",
])

with tab1:
    if not yf_history.empty:
        market_choice = st.selectbox("選擇金融市場標的：", yf_history.columns)
        fig1 = px.line(
            yf_history,
            y=market_choice,
            title=f"{market_choice} 近一年收盤走勢",
            labels={"value": "價格 / 點數", "Date": "日期"},
        )
        fig1.update_layout(hovermode="x unified")
        st.plotly_chart(fig1, use_container_width=True)
    else:
        st.warning("尚無金融市場歷史數據。")

with tab2:
    if not fred_res["history_df"].empty:
        col_chart1, col_chart2 = st.columns(2)
        with col_chart1:
            macro_choice = st.selectbox("選擇總體經濟指標：", fred_res["history_df"].columns)
            fig2 = px.line(
                fred_res["history_df"],
                y=macro_choice,
                title=f"{macro_choice} 近三年趨勢",
                labels={"value": "數值 (%)", "index": "日期"},
            )
            fig2.update_layout(hovermode="x unified")
            st.plotly_chart(fig2, use_container_width=True)

        with col_chart2:
            recent_spread = fred_res["spread_df"].tail(252 * 5)
            fig_sp = go.Figure()
            fig_sp.add_trace(
                go.Scatter(
                    x=recent_spread.index,
                    y=recent_spread["Spread"],
                    mode="lines",
                    name="10Y-2Y 利差 (%)",
                    line=dict(color="#1f77b4", width=2),
                )
            )
            fig_sp.add_hline(y=0, line_dash="dash", line_color="red", annotation_text="倒掛臨界線 (0%)")
            fig_sp.update_layout(title="美國 10Y-2Y 公債利差 (近 5 年)", hovermode="x unified")
            st.plotly_chart(fig_sp, use_container_width=True)
    else:
        st.info("輸入 FRED API Key 後即可檢視總經與利差歷史圖表。")

with tab3:
    st.subheader("📰 即時新聞擷取與 Gemini 鷹鴿派情緒評分")
    search_kw = st.text_input(
        "🔍 輸入新聞搜尋關鍵字（支援中英文，例如：Federal Reserve Inflation、TSMC、聯準會 降息）：",
        value="Federal Reserve Inflation",
    )

    articles = fetch_google_news(search_kw)
    nlp_col1, nlp_col2 = st.columns([1, 1.3])

    with nlp_col1:
        st.markdown(f"#### 📌 最新相關新聞 ({len(articles)} 則)")
        if articles:
            for idx, art in enumerate(articles, 1):
                st.markdown(f"**{idx}.** [{art['title']}]({art['link']})")
                if art["published"]:
                    st.caption(f"發布時間: {art['published']}")
        else:
            st.warning("查無相關新聞，請嘗試更換關鍵字。")

    with nlp_col2:
        st.markdown("#### 🤖 Gemini AI 總經與鷹鴿情緒報告")

        if st.button("🚀 執行新聞鷹鴿派與市場情緒分析", type="primary"):
            if GENAI_MODE is None:
                st.error("請先在 `requirements.txt` 安裝 `google-genai`。")
            elif not GEMINI_API_KEY:
                st.warning("👈 請先在左側側邊欄輸入 Gemini API Key。")
            elif not articles:
                st.warning("目前無新聞標題可供分析。")
            else:
                with st.spinner("AI 正在結合總經數據與最新新聞進行鷹鴿立場剖析..."):
                    # 整理目前總經背景給 AI 參考（若有）
                    macro_context = "（尚未提供 FRED 總經數據）"
                    if fred_res["metrics"]:
                        m = fred_res["metrics"]
                        cpi_str = f"{m['美國 CPI 年增率']['value']:.2f}%" if "美國 CPI 年增率" in m else "N/A"
                        r10_str = f"{m['10年期美債殖利率']['value']:.2f}%" if "10年期美債殖利率" in m else "N/A"
                        un_str = f"{m['美國失業率']['value']:.2f}%" if "美國失業率" in m else "N/A"
                        macro_context = f"美國 CPI 年增率: {cpi_str} | 10Y美債: {r10_str} | 失業率: {un_str}"

                    news_list_str = "\n".join([f"{i}. {a['title']}" for i, a in enumerate(articles, 1)])

                    prompt = f"""
你是一位華爾街資深總體經濟與貨幣政策分析師。請結合【當前總經背景】與【最新新聞標題】，針對關鍵字「{search_kw}」進行深度市場情緒與政策立場評估。

【當前總經背景】
{macro_context}

【最新抓取新聞標題】
{news_list_str}

【輸出格式要求（請使用繁體中文與 Markdown）】
1. **綜合市場情緒儀表**：
   - 市場情緒分數：**XX / 100**（0 為極度恐慌/偏空，50 為中立，100 為極度樂觀/偏多）
   - 貨幣政策立場判定：**【偏鷹派 (Hawkish) / 偏鴿派 (Dovish) / 中立觀望 (Neutral)】**
2. **核心宏觀解讀（150 字內）**：綜合新聞風向與總經數據，點出目前市場最關注的定價邏輯與潛在風險。
3. **逐條新聞衝擊剖析表**：以 Markdown 表格呈現（欄位：`編號` | `新聞核心摘要` | `立場標籤 (鷹/鴿/利多/利空)` | `影響程度 (高/中/低)`）。
"""
                    try:
                        report_text, used_model = call_gemini_with_fallback(prompt, GEMINI_API_KEY)
                        st.session_state["nlp_report"] = (report_text, used_model, search_kw)
                    except Exception as e:
                        st.error(f"AI 分析失敗（請稍後重試）: {e}")

        # 顯示保存於 session_state 的分析結果
        if "nlp_report" in st.session_state:
            report_text, used_model, analyzed_kw = st.session_state["nlp_report"]
            st.caption(f"分析關鍵字：`{analyzed_kw}` ｜ 使用模型：`{used_model}`")
            st.markdown(report_text)
