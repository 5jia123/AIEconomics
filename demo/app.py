import datetime
import re
import time
import urllib.request
import xml.etree.ElementTree as ET
from urllib.parse import quote

import pandas as pd
import streamlit as st
import yfinance as yf
from fredapi import Fred

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

# --- 2. 監控代碼字典設定 ---
YFINANCE_CONFIG = {
    "S&P 500": "^GSPC",
    "台灣加權指數": "^TWII",
    "黃金現貨 (GC=F)": "GC=F",
    "原油 (WTI)": "CL=F",
    "美元/台幣匯率": "TWD=X",
}

FRED_CONFIG = {
    "美國 10 年期國債殖利率": "DGS10",
    "聯邦基金利率": "FEDFUNDS",
    "美國 CPI (年增率)": "CPIAUCSL",
    "美國失業率": "UNRATE",
    "日本 10 年期國債殖利率": "IRLTLT01JPM156N",
}

# --- 3. 金鑰安全讀取 (避免無 secrets.toml 時報錯) ---
def get_secret_safe(key_name: str) -> str:
    try:
        return st.secrets.get(key_name, "")
    except Exception:
        return ""

FRED_API_KEY = get_secret_safe("FRED_API_KEY")
GEMINI_API_KEY = get_secret_safe("GEMINI_API_KEY")

with st.sidebar:
    st.header("⚙️ API 金鑰與設定")
    if FRED_API_KEY:
        st.success("✅ 已自動讀取 FRED Key")
    else:
        FRED_API_KEY = st.text_input("FRED API Key", type="password")

    if GEMINI_API_KEY:
        st.success("✅ 已自動讀取 Gemini Key")
    else:
        GEMINI_API_KEY = st.text_input("Gemini API Key", type="password")

    st.write("---")
    if st.button("🔄 強制刷新所有快取數據", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

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

    raw_cache = {}

    # 4-1. 抓取一般總經指標
    for label, series_id in series_dict.items():
        try:
            raw_series = fred.get_series(series_id)
            series = pd.to_numeric(raw_series, errors="coerce").dropna()
            raw_cache[series_id] = series

            if series.empty or len(series) < 2:
                results["metrics"][label] = {"error": "無資料"}
                continue

            if series_id == "CPIAUCSL":
                if len(series) >= 14:
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
                is_monthly = series_id in ["IRLTLT01JPM156N", "UNRATE", "FEDFUNDS"]
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
        dgs10 = raw_cache.get("DGS10")
        if dgs10 is None:
            dgs10 = pd.to_numeric(fred.get_series("DGS10"), errors="coerce")
        dgs2 = pd.to_numeric(fred.get_series("DGS2"), errors="coerce")
        df_spread = pd.DataFrame({"10Y": dgs10, "2Y": dgs2}).dropna()
        df_spread["Spread"] = df_spread["10Y"] - df_spread["2Y"]
        results["spread_df"] = df_spread
    except Exception as e:
        results["error"] = f"利差計算失敗: {e}"

    return results


@st.cache_data(ttl=1800)
def fetch_google_news(keyword: str = "Federal Reserve Inflation", max_items: int = 8):
    """使用內建 urllib + ElementTree 解析 Google News RSS（免安裝 feedparser）"""
    safe_keyword = quote(keyword.strip())
    has_chinese = bool(re.search(r"[\u4e00-\u9fff]", keyword))
    if has_chinese:
        url = f"https://news.google.com/rss/search?q={safe_keyword}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
    else:
        url = f"https://news.google.com/rss/search?q={safe_keyword}&hl=en-US&gl=US&ceid=US:en"

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            xml_data = resp.read()
        root = ET.fromstring(xml_data)

        articles = []
        for item in root.findall("./channel/item")[:max_items]:
            title = item.findtext("title", default="無標題")
            link = item.findtext("link", default="")
            pub_date = item.findtext("pubDate", default="")
            articles.append({"title": title, "link": link, "published": pub_date})
        return articles
    except Exception:
        return []


# --- 5. 執行載入 ---
with st.spinner("載入即時市場、殖利率與總經數據中..."):
    yf_data = get_yfinance_metrics(YFINANCE_CONFIG)
    fred_data = get_macro_and_spread_data(FRED_CONFIG, FRED_API_KEY)

# --- 6. 頁面標頭 ---
st.title("🌐 全球金融市場、宏觀利率與 AI 輿論儀表板")
st.caption(f"最後更新時間: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
st.write("---")

# --- 7. 第一區塊：核心市場指標 ---
st.subheader("📈 大宗商品與關鍵市場資產")
cols_yf = st.columns(len(YFINANCE_CONFIG))
for idx, (label, sym) in enumerate(YFINANCE_CONFIG.items()):
    col = cols_yf[idx]
    item = yf_data.get(label)
    with col:
        if item and "price" in item:
            no_dollar = "指數" in label or "匯率" in label
            st.metric(
                label=label,
                value=f"{item['price']:,.2f}" if no_dollar else f"${item['price']:,.2f}",
                delta=f"{item['change']:+.2f}%",
            )
        else:
            err = item.get("error", "無資料") if item else "載入失敗"
            st.metric(label=label, value="N/A", delta=err)

st.write("")

# --- 8. 第二區塊：全球國債殖利率、失業率與 CPI ---
st.subheader("🏛️ 全球國債殖利率與總經數據 (FRED)")
if not FRED_API_KEY:
    st.info("💡 請在左側側邊欄輸入 FRED API Key，或在 `.streamlit/secrets.toml` 配置 `FRED_API_KEY`。")
else:
    cols_fred = st.columns(len(FRED_CONFIG))
    for idx, (label, series_id) in enumerate(FRED_CONFIG.items()):
        col = cols_fred[idx]
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
st.subheader("🚨 美國 10Y - 2Y 公債殖利率利差與倒掛監控")
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
st.subheader("📊 趨勢歷史走勢圖")
tab1, tab2, tab3, tab4 = st.tabs([
    "黃金現貨 (GC=F)",
    "日本 10 年期國債殖利率",
    "10Y-2Y 利差歷史 (含倒掛基準線)",
    "美國 CPI 年增率",
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

# --- 11. Gemini AI 呼叫函式 (具備多模型備援與重試機制) ---
cpi_item = fred_data["metrics"].get("美國 CPI (年增率)")
unrate_item = fred_data["metrics"].get("美國失業率")
fed_item = fred_data["metrics"].get("聯邦基金利率")

cpi_val = f"{cpi_item['value']:.2f}%" if cpi_item and "value" in cpi_item else "無資料"
unrate_val = f"{unrate_item['value']:.2f}%" if unrate_item and "value" in unrate_item else "無資料"
fed_val = f"{fed_item['value']:.2f}%" if fed_item and "value" in fed_item else "無資料"
us10y_val = f"{latest_10y:.2f}%" if latest_10y is not None else "無資料"
spread_val = f"{latest_spread:+.2f}%" if latest_spread is not None else "無資料"


def generate_macro_summary(prompt_text, api_key):
    """嘗試依序呼叫不同模型，遇 503/429 自動退避重試並降級備援"""
    candidate_models = ["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.0-flash"]
    last_err = None

    if GENAI_MODE == "google-genai":
        client = genai.Client(api_key=api_key)
        for model_name in candidate_models:
            for retry in range(2):
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
                    else:
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

    raise last_err if last_err else Exception("所有備援模型皆無法回應")


# --- 12. 第五區塊：AI 總體經濟即時短評 ---
st.subheader("🤖 AI 總體經濟即時短評")

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
- 聯邦基金利率: {fed_val}
- 美國 10 年期國債殖利率: {us10y_val}
- 10Y-2Y 國債利差: {spread_val}

【撰寫要求】
1. 評估目前通膨與就業市場的平衡狀況。
2. 解讀殖利率與利差反映的市場降息/衰退預期。
3. 繁體中文撰寫，字數控制在 200 字以內，語氣專業洗鍊。
"""
            try:
                ai_text, used_model = generate_macro_summary(prompt, GEMINI_API_KEY)
                st.session_state["macro_summary"] = (ai_text, used_model)
            except Exception as e:
                st.error(f"生成摘要失敗（伺服器暫時壅塞，請稍候重試）: {e}")

if "macro_summary" in st.session_state:
    ai_text, used_model = st.session_state["macro_summary"]
    st.markdown(
        f"""
        <div style="background-color: #f1f3f5; color: #1f2937; padding: 16px; border-radius: 8px; border-left: 4px solid #1f77b4; line-height: 1.6;">
        {ai_text}
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.caption(f"⚡ 由 {used_model} 自動分析生成")

st.write("---")

# --- 13. 第六區塊：NLP 即時新聞與 AI 鷹鴿情緒評分 ---
st.subheader("📰 即時財經新聞與 AI 鷹鴿情緒評分")

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
    st.markdown("#### 🤖 Gemini AI 鷹鴿派情緒分析報告")
    if st.button("🚀 執行新聞鷹鴿派與市場情緒分析", type="primary"):
        if GENAI_MODE is None:
            st.error("請先在 `requirements.txt` 中安裝 `google-genai`。")
        elif not GEMINI_API_KEY:
            st.error("請先設定 `GEMINI_API_KEY`。")
        elif not articles:
            st.warning("目前無新聞標題可供分析。")
        else:
            with st.spinner("AI 正在剖析新聞立場與鷹鴿情緒中..."):
                news_list_str = "\n".join([f"{i}. {a['title']}" for i, a in enumerate(articles, 1)])
                nlp_prompt = f"""
你是一位專業總體經濟分析師。請結合目前總經數據（CPI 年增率: {cpi_val}、失業率: {unrate_val}、聯邦基金利率: {fed_val}、10Y美債: {us10y_val}、10Y-2Y利差: {spread_val}），分析以下關於「{search_kw}」的最新新聞標題：

{news_list_str}

請以繁體中文輸出結構化報告：
1. **整體市場情緒分數**：0~100 分（0 極度恐慌，50 中立，100 極度樂觀）
2. **貨幣政策立場判定**：鷹派 (Hawkish) / 鴿派 (Dovish) / 中立 (Neutral)
3. **重點宏觀解讀**（150 字內）
4. **逐條新聞衝擊剖析表**（Markdown 表格：編號 | 新聞摘要 | 鷹鴿/多空標籤 | 影響程度）
"""
                try:
                    report_text, used_model = generate_macro_summary(nlp_prompt, GEMINI_API_KEY)
                    st.session_state["nlp_report"] = (report_text, used_model, search_kw)
                except Exception as e:
                    st.error(f"分析失敗: {e}")

    if "nlp_report" in st.session_state:
        report_text, used_model, analyzed_kw = st.session_state["nlp_report"]
        st.caption(f"分析關鍵字：`{analyzed_kw}` ｜ 使用模型：`{used_model}`")
        st.markdown(report_text)
