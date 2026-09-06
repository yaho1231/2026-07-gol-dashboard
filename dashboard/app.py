import streamlit as st
import pandas as pd
import sqlite3
import json
import plotly.express as px
import plotly.graph_objects as go
import os
import math
from datetime import datetime
from dotenv import load_dotenv

import importlib.util

PROJECT_ROOT = os.path.dirname(os.path.dirname(__file__))
BACKEND_ROOT = os.path.join(PROJECT_ROOT, "backend")

def _load_analyze_player():
    module_path = os.path.join(BACKEND_ROOT, "app", "services", "player_analysis.py")
    spec = importlib.util.spec_from_file_location("deeplol_player_analysis", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.analyze_player

def _load_champion_data():
    module_path = os.path.join(BACKEND_ROOT, "app", "services", "champion_data.py")
    spec = importlib.util.spec_from_file_location("deeplol_champion_data", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

def _load_image_assets():
    module_path = os.path.join(os.path.dirname(__file__), "image_assets.py")
    spec = importlib.util.spec_from_file_location("deeplol_image_assets", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

def _load_position_service():
    module_path = os.path.join(BACKEND_ROOT, "app", "services", "position_service.py")
    spec = importlib.util.spec_from_file_location("deeplol_position_service", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

def _load_team_builder():
    module_path = os.path.join(os.path.dirname(__file__), "team_builder.py")
    spec = importlib.util.spec_from_file_location("deeplol_team_builder", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

def _load_player_tags():
    module_path = os.path.join(BACKEND_ROOT, "app", "services", "player_tags.py")
    spec = importlib.util.spec_from_file_location("deeplol_player_tags", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

champion_data = _load_champion_data()
enrich_champion_names = champion_data.enrich_champion_names
analyze_player = _load_analyze_player()
position_service = _load_position_service()
image_assets = _load_image_assets()
team_builder = _load_team_builder()
player_tags = _load_player_tags()
POSITION_KR = position_service.POSITION_KR

# ── DeepLOL 이미지 헬퍼 — image_assets가 로컬 캐시 후 data URI로 반환한다.
# st.cache_data로 감싸 스크립트 재실행 때 디스크 재조회/base64 재인코딩을 피한다.
@st.cache_data(show_spinner=False)
def champ_icon_by_name(champion_name) -> str | None:
    if not champion_name:
        return None
    image_key = champion_data.get_champion_image_key(champion_name=str(champion_name))
    return image_assets.champion_icon_uri(image_key)

@st.cache_data(show_spinner=False)
def tier_emblem(tier) -> str | None:
    return image_assets.tier_emblem_uri(tier)

@st.cache_data(show_spinner=False)
def position_icon(position) -> str | None:
    return image_assets.position_icon_uri(position)

@st.cache_data(show_spinner=False)
def profile_icon(icon_id) -> str | None:
    return image_assets.profile_icon_uri(icon_id)

def inline_img(uri: str | None, size: int = 20, radius: int = 4) -> str:
    """마크다운/HTML 문자열 안에 넣는 인라인 이미지 태그. URI가 없으면 빈 문자열."""
    if not uri:
        return ""
    return (
        f"<img src='{uri}' style='width:{size}px;height:{size}px;"
        f"border-radius:{radius}px;vertical-align:-{max(size // 5, 3)}px'>"
    )
dotenv_path = os.path.join(PROJECT_ROOT, ".env")
load_dotenv(dotenv_path)

MIN_STATS_MATCHES = 1
DIVISION_ROMAN = {1: "I", 2: "II", 3: "III", 4: "IV"}

# 다크 테마 위에서 서로 구분되는 파스텔 차트 팔레트 (인접 색끼리 대비되도록 정렬)
PASTEL_PALETTE = ["#8BE9DD", "#F5D78E", "#C4B5F0", "#F2A6A0", "#A8DFA0", "#9CC7F2", "#F5B8D9", "#FFC49E"]
# 포지션별 고정 색 — 데이터 순서가 바뀌어도 색이 유지되도록 매핑으로 지정
POSITION_COLORS = {
    "탑": "#F2A6A0",    # 코랄
    "정글": "#A8DFA0",  # 연두
    "미드": "#8BE9DD",  # 민트
    "원딜": "#F5D78E",  # 골드
    "서폿": "#C4B5F0",  # 라벤더
    "미정": "#9AA7B4",  # 그레이
}
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./inhouse.db")
_rel_db_path = DATABASE_URL.replace("sqlite:///", "", 1)
db_path = _rel_db_path if os.path.isabs(_rel_db_path) else os.path.join(PROJECT_ROOT, os.path.normpath(_rel_db_path))

# Set page configs
st.set_page_config(
    page_title="GOL League Dashboard",
    page_icon="🎮",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Custom premium styling injection (Neon glassmorphism accents)
st.markdown("""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=Outfit:wght@400;500;600;700;800&display=swap');

    /* Dark Theme Core */
    .stApp {
        background:
            radial-gradient(1200px 600px at 12% -10%, rgba(102,252,241,0.08), transparent 60%),
            radial-gradient(900px 500px at 95% 0%, rgba(69,162,158,0.10), transparent 55%),
            #0B0C10;
        color: #C5C6C7;
        font-family: 'Outfit', 'Inter', sans-serif;
    }

    .block-container { padding-top: 2.2rem; max-width: 1320px; }

    /* Headers styling */
    h1, h2, h3 {
        color: #E9FEFC !important;
        font-weight: 800;
        letter-spacing: 0.2px;
    }
    h1 {
        background: linear-gradient(90deg, #66FCF1 0%, #45A29E 60%, #66FCF1 100%);
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
        background-clip: text;
    }
    h2, h3 { color: #66FCF1 !important; }

    /* Sidebar */
    [data-testid="stSidebar"] {
        background: linear-gradient(180deg, #131A22 0%, #0E141B 100%);
        border-right: 1px solid rgba(69,162,158,0.45);
    }
    [data-testid="stSidebar"] [role="radiogroup"] label {
        background: rgba(255,255,255,0.02);
        border: 1px solid transparent;
        border-radius: 10px;
        padding: 8px 12px;
        margin-bottom: 6px;
        transition: all 0.18s ease;
    }
    [data-testid="stSidebar"] [role="radiogroup"] label:hover {
        background: rgba(102,252,241,0.08);
        border-color: rgba(102,252,241,0.35);
    }

    /* Buttons */
    .stButton > button {
        border-radius: 10px;
        border: 1px solid rgba(69,162,158,0.55);
        background: rgba(31,40,51,0.8);
        color: #E9FEFC;
        font-weight: 600;
        transition: all 0.18s ease;
    }
    .stButton > button:hover {
        border-color: #66FCF1;
        box-shadow: 0 0 0 2px rgba(102,252,241,0.18);
        transform: translateY(-1px);
    }

    /* Dataframes */
    [data-testid="stDataFrame"] {
        border: 1px solid rgba(69,162,158,0.30);
        border-radius: 12px;
        overflow: hidden;
    }
    
    /* Metrics / Cards design */
    div[data-testid="metric-container"], div[data-testid="stMetric"] {
        background: linear-gradient(145deg, rgba(31,40,51,0.95) 0%, rgba(20,27,35,0.95) 100%);
        border: 1px solid rgba(69,162,158,0.45);
        border-radius: 14px;
        padding: 16px 18px;
        box-shadow: 0 8px 24px rgba(0,0,0,0.35);
        transition: all 0.18s ease;
    }
    div[data-testid="metric-container"]:hover, div[data-testid="stMetric"]:hover {
        border-color: rgba(102,252,241,0.6);
        transform: translateY(-2px);
    }
    div[data-testid="stMetricValue"] {
        color: #66FCF1;
        font-size: 1.9rem;
        font-weight: 800;
    }
    
    /* Expander card decoration */
    .streamlit-expanderHeader {
        background-color: #1F2833 !important;
        border: 1px solid #45A29E !important;
        border-radius: 5px;
        color: #FFFFFF !important;
    }
    
    /* Custom Badge elements */
    .badge-mvp {
        background: linear-gradient(135deg, #FFD700 0%, #FFA500 100%);
        color: #000000;
        padding: 2px 8px;
        border-radius: 4px;
        font-weight: bold;
        font-size: 0.8rem;
    }
    .badge-win {
        background-color: #1A365D;
        color: #63B3ED;
        padding: 2px 8px;
        border-radius: 4px;
        font-weight: bold;
        font-size: 0.8rem;
    }
    .badge-loss {
        background-color: #742A2A;
        color: #FEB2B2;
        padding: 2px 8px;
        border-radius: 4px;
        font-weight: bold;
        font-size: 0.8rem;
    }

    /* Match history cards */
    .match-meta-bar {
        display: flex;
        flex-wrap: wrap;
        gap: 8px;
        margin-bottom: 12px;
    }
    .match-chip {
        background-color: #1F2833;
        border: 1px solid #45A29E;
        color: #C5C6C7;
        padding: 6px 12px;
        border-radius: 999px;
        font-size: 0.85rem;
        font-weight: 600;
    }
    .match-chip.win-blue {
        border-color: #63B3ED;
        color: #90CDF4;
        background-color: #1A365D;
    }
    .match-chip.win-red {
        border-color: #FC8181;
        color: #FEB2B2;
        background-color: #742A2A;
    }
    .match-chip.mvp {
        border-color: #FFD700;
        color: #FFD700;
        background: linear-gradient(135deg, rgba(255, 215, 0, 0.15) 0%, rgba(255, 165, 0, 0.1) 100%);
    }
    .team-panel-title {
        font-size: 1rem;
        font-weight: 700;
        margin-bottom: 8px;
        padding: 8px 12px;
        border-radius: 8px;
    }
    .team-panel-title.blue {
        background: linear-gradient(90deg, rgba(26, 54, 93, 0.9) 0%, rgba(15, 76, 58, 0.2) 100%);
        color: #90CDF4;
        border-left: 4px solid #63B3ED;
    }
    .team-panel-title.red {
        background: linear-gradient(90deg, rgba(116, 42, 42, 0.9) 0%, rgba(125, 46, 46, 0.2) 100%);
        color: #FEB2B2;
        border-left: 4px solid #FC8181;
    }

    /* ===== Analysis report components ===== */
    .section-label {
        display: flex; align-items: center; gap: 8px;
        font-size: 1.05rem; font-weight: 700; color: #9EE9E4;
        margin: 18px 0 10px;
    }
    .section-label::after {
        content: ""; flex: 1; height: 1px;
        background: linear-gradient(90deg, rgba(102,252,241,0.4), transparent);
    }

    .hero-card {
        background: linear-gradient(135deg, rgba(102,252,241,0.12) 0%, rgba(31,40,51,0.92) 45%, rgba(20,27,35,0.95) 100%);
        border: 1px solid rgba(102,252,241,0.4);
        border-radius: 16px;
        padding: 20px 22px;
        box-shadow: 0 10px 30px rgba(0,0,0,0.4);
        margin-bottom: 8px;
    }
    .hero-tags { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 12px; }
    .hero-tag {
        font-size: 0.8rem; font-weight: 700; padding: 5px 12px; border-radius: 999px;
        background: rgba(102,252,241,0.12); border: 1px solid rgba(102,252,241,0.45); color: #9EE9E4;
    }
    .hero-tag.alt { background: rgba(255,215,0,0.10); border-color: rgba(255,215,0,0.45); color: #FFD86B; }
    .hero-quote { font-size: 1.02rem; line-height: 1.6; color: #EAFBFA; }

    .metric-grid {
        display: grid;
        grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
        gap: 12px; margin-bottom: 6px;
    }
    .metric-card {
        background: linear-gradient(150deg, rgba(31,40,51,0.9), rgba(18,24,31,0.92));
        border: 1px solid rgba(69,162,158,0.25);
        border-radius: 13px; padding: 13px 15px;
        transition: transform 0.16s ease, border-color 0.16s ease;
    }
    .metric-card:hover { transform: translateY(-2px); border-color: rgba(102,252,241,0.5); }
    .mc-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
    .mc-label { font-weight: 700; color: #E9FEFC; font-size: 0.95rem; }
    .mc-desc { font-size: 0.72rem; color: #7C8A92; margin-top: 1px; }
    .mc-value { font-size: 1.55rem; font-weight: 800; color: #66FCF1; margin: 6px 0 2px; }
    .mc-eval { font-size: 0.76rem; color: #9AA7AE; line-height: 1.45; }
    .rating-chip {
        font-size: 0.7rem; font-weight: 800; padding: 3px 9px; border-radius: 999px; white-space: nowrap;
    }

    .sw-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
    @media (max-width: 820px) { .sw-grid { grid-template-columns: 1fr; } }
    .sw-col-title { font-weight: 800; font-size: 0.95rem; margin-bottom: 8px; }
    .sw-card {
        display: flex; align-items: center; gap: 12px;
        background: rgba(31,40,51,0.7);
        border-radius: 11px; padding: 11px 14px; margin-bottom: 9px;
        border-left: 4px solid #45A29E;
    }
    .sw-card.good { border-left-color: #10B981; background: linear-gradient(90deg, rgba(16,185,129,0.10), rgba(31,40,51,0.7) 60%); }
    .sw-card.bad  { border-left-color: #EF4444; background: linear-gradient(90deg, rgba(239,68,68,0.10), rgba(31,40,51,0.7) 60%); }
    .sw-main { flex: 1; }
    .sw-name { font-weight: 700; color: #E9FEFC; font-size: 0.95rem; }
    .sw-name .sw-desc { font-weight: 500; color: #7C8A92; font-size: 0.74rem; }
    .sw-sub { font-size: 0.76rem; color: #9AA7AE; margin-top: 2px; }
    .sw-val { font-size: 1.25rem; font-weight: 800; }
    .sw-card.good .sw-val { color: #34D399; }
    .sw-card.bad .sw-val { color: #F87171; }

    .callout {
        display: flex; gap: 12px; align-items: flex-start;
        background: linear-gradient(135deg, rgba(102,252,241,0.10), rgba(31,40,51,0.9));
        border: 1px solid rgba(102,252,241,0.4); border-left: 4px solid #66FCF1;
        border-radius: 12px; padding: 14px 16px; margin: 4px 0 6px;
    }
    .callout .ic { font-size: 1.3rem; }
    .callout .tx { color: #EAFBFA; line-height: 1.55; font-size: 0.95rem; }

    .pattern-card {
        background: rgba(31,40,51,0.7); border-radius: 12px; padding: 14px 16px;
        border: 1px solid rgba(69,162,158,0.25); height: 100%;
    }
    .pattern-card.win { border-top: 3px solid #34D399; }
    .pattern-card.loss { border-top: 3px solid #F87171; }
    .pattern-card .pc-title { font-weight: 800; margin-bottom: 8px; font-size: 0.95rem; }
    .pattern-card ul { margin: 0; padding-left: 18px; }
    .pattern-card li { font-size: 0.84rem; color: #BFD0D2; margin-bottom: 6px; line-height: 1.45; }

    /* Hero stat capsules */
    .hero-stats { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 14px; }
    .hero-stat {
        min-width: 104px; padding: 9px 14px; border-radius: 12px;
        background: rgba(11,12,16,0.55); border: 1px solid rgba(102,252,241,0.22);
    }
    .hero-stat .hs-k { font-size: 0.66rem; color: #7C8A92; font-weight: 700; letter-spacing: 0.5px; text-transform: uppercase; }
    .hero-stat .hs-v { font-size: 1.3rem; font-weight: 800; color: #66FCF1; margin-top: 2px; line-height: 1.2; }
    .hero-stat .hs-s { font-size: 0.68rem; color: #9AA7AE; margin-top: 2px; }

    /* Metric card gauge — 눈금(틱)이 포지션 평균(100%) 위치 */
    .mc-bar { position: relative; height: 6px; border-radius: 999px; background: rgba(255,255,255,0.07); margin-top: 9px; }
    .mc-bar .fill { height: 100%; border-radius: 999px; transition: width 0.4s ease; }
    .mc-bar .avg-tick { position: absolute; top: -3px; bottom: -3px; width: 2px; border-radius: 2px; background: rgba(255,255,255,0.4); left: 62.5%; }
    .mc-vs { display: flex; justify-content: space-between; font-size: 0.7rem; color: #7C8A92; margin-top: 5px; }
    .mc-vs b { color: #C5C6C7; font-weight: 700; }

    /* Win/Loss quantitative rows */
    .wl-row {
        display: flex; align-items: center; gap: 10px;
        padding: 8px 10px; border-radius: 9px;
        background: rgba(255,255,255,0.025); margin-bottom: 6px;
    }
    .wl-lab { flex: 1; font-weight: 700; color: #E9FEFC; font-size: 0.85rem; }
    .wl-num { font-size: 0.8rem; color: #7C8A92; text-align: right; white-space: nowrap; }
    .wl-num b { color: #E9FEFC; font-weight: 800; }
    .wl-delta {
        font-size: 0.76rem; font-weight: 800; padding: 3px 10px; border-radius: 999px;
        min-width: 70px; text-align: center; white-space: nowrap;
    }
    .wl-delta.up { color: #34D399; background: rgba(16,185,129,0.12); border: 1px solid rgba(16,185,129,0.4); }
    .wl-delta.down { color: #F87171; background: rgba(239,68,68,0.12); border: 1px solid rgba(239,68,68,0.4); }

    /* 이미지 메트릭 카드 (최다 픽 챔피언 등 st.metric 대체) */
    .img-metric {
        display: flex; align-items: center; gap: 12px;
        background: linear-gradient(145deg, rgba(31,40,51,0.95) 0%, rgba(20,27,35,0.95) 100%);
        border: 1px solid rgba(69,162,158,0.45);
        border-radius: 14px; padding: 14px 18px;
        box-shadow: 0 8px 24px rgba(0,0,0,0.35);
    }
    .img-metric img { width: 46px; height: 46px; border-radius: 10px; border: 1px solid rgba(102,252,241,0.35); }
    .img-metric .im-label { font-size: 0.8rem; color: #9AA7AE; }
    .img-metric .im-value { font-size: 1.25rem; font-weight: 800; color: #66FCF1; line-height: 1.3; }
    .img-metric .im-sub { font-size: 0.85rem; color: #9AA7AE; font-weight: 600; }

    /* 상세분석 프로필 헤더 */
    .player-head { display: flex; align-items: center; gap: 14px; margin-bottom: 12px; }
    .player-head img.avatar {
        width: 64px; height: 64px; border-radius: 16px;
        border: 2px solid rgba(102,252,241,0.45);
        box-shadow: 0 6px 18px rgba(0,0,0,0.4);
    }
    .player-head .ph-fallback {
        width: 64px; height: 64px; border-radius: 16px; font-size: 1.7rem;
        background: rgba(31,40,51,0.9); border: 2px solid rgba(69,162,158,0.4);
        display: flex; align-items: center; justify-content: center;
    }
    .player-head .ph-name { font-size: 1.45rem; font-weight: 800; color: #E9FEFC; }
    .player-head .ph-tier { display: flex; align-items: center; gap: 7px; color: #9EE9E4; font-weight: 700; font-size: 0.9rem; margin-top: 3px; }
    .player-head .ph-tier img { width: 26px; height: 26px; }

    /* 모스트 챔피언 TOP 3 카드 */
    .champ3-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 14px; margin-bottom: 8px; }
    @media (max-width: 820px) { .champ3-grid { grid-template-columns: 1fr; } }
    .champ3-card {
        position: relative; text-align: center; padding: 20px 14px 16px;
        border-radius: 15px; border: 1px solid rgba(69,162,158,0.35);
        background: linear-gradient(160deg, rgba(31,40,51,0.92), rgba(18,24,31,0.95));
        transition: transform 0.16s ease;
    }
    .champ3-card:hover { transform: translateY(-2px); }
    .champ3-card.rank1 { border-color: rgba(255,215,0,0.55); background: linear-gradient(160deg, rgba(255,215,0,0.10), rgba(31,40,51,0.92) 55%); }
    .champ3-card.rank2 { border-color: rgba(203,213,225,0.45); }
    .champ3-card.rank3 { border-color: rgba(205,127,50,0.5); }
    .champ3-card .c3-medal { position: absolute; top: 10px; left: 12px; font-size: 1.25rem; }
    .champ3-card img.c3-img { width: 64px; height: 64px; border-radius: 14px; border: 2px solid rgba(102,252,241,0.4); }
    .champ3-card .c3-fallback {
        width: 64px; height: 64px; border-radius: 14px; margin: 0 auto; font-size: 1.6rem;
        background: rgba(11,12,16,0.55); border: 2px solid rgba(69,162,158,0.35);
        display: flex; align-items: center; justify-content: center;
    }
    .champ3-card .c3-name { font-weight: 800; color: #E9FEFC; margin-top: 8px; font-size: 1.02rem; }
    .champ3-card .c3-main { color: #66FCF1; font-weight: 800; font-size: 0.9rem; margin-top: 4px; }
    .champ3-card .c3-sub { color: #9AA7AE; font-size: 0.78rem; margin-top: 2px; }

    /* 리포트 챔피언 강조 카드 얼굴 */
    .sw-card img.champ-face { width: 42px; height: 42px; border-radius: 10px; border: 1px solid rgba(102,252,241,0.3); }

    /* Plotly 차트 — 데이터 요소는 기본 어둡게, 호버한 지표만 원래 밝기 (아래 JS와 연동) */
    [data-testid="stPlotlyChart"] .barlayer .trace,
    [data-testid="stPlotlyChart"] .scatterlayer .trace,
    [data-testid="stPlotlyChart"] .pielayer .slice { transition: opacity 0.15s ease; }
    /* 차트가 페이지 스크롤(터치 포함)을 가로채지 않도록 */
    [data-testid="stPlotlyChart"],
    [data-testid="stPlotlyChart"] .draglayer,
    [data-testid="stPlotlyChart"] .nsewdrag { touch-action: pan-y !important; }

    /* Plotly 호버 스크립트용 1px iframe 컨테이너 — 화면에서 완전히 숨긴다 */
    .st-key-plotly_hover_script { display: none; }

    /* 자동 태그 칩 — 지표 기반, 마우스를 올리면 근거 툴팁 */
    .ptag-row { display: flex; flex-wrap: wrap; gap: 7px; margin: 6px 0 10px; }
    .ptag {
        font-size: 0.78rem; font-weight: 700; padding: 4px 11px;
        border-radius: 999px; border: 1px solid; cursor: help; white-space: nowrap;
    }
    .ptag.good { color: #34D399; background: rgba(16,185,129,0.10); border-color: rgba(16,185,129,0.45); }
    .ptag.bad { color: #F87171; background: rgba(239,68,68,0.10); border-color: rgba(239,68,68,0.45); }
    .ptag.neutral { color: #94A3B8; background: rgba(148,163,184,0.10); border-color: rgba(148,163,184,0.4); }

    /* Play style score bars */
    .style-row { display: flex; align-items: center; gap: 12px; margin-bottom: 8px; }
    .style-name { width: 160px; font-size: 0.85rem; color: #E9FEFC; font-weight: 600; text-align: right; white-space: nowrap; }
    .style-track { flex: 1; height: 10px; border-radius: 999px; background: rgba(255,255,255,0.06); overflow: hidden; }
    .style-fill { height: 100%; border-radius: 999px; background: linear-gradient(90deg, #2E6E6A, #45A29E); }
    .style-fill.top { background: linear-gradient(90deg, #45A29E, #66FCF1); box-shadow: 0 0 10px rgba(102,252,241,0.5); }
    .style-score { width: 46px; font-size: 0.8rem; color: #9AA7AE; font-weight: 700; }
    </style>
""", unsafe_allow_html=True)

# ── Plotly 인터랙션 잠금 — 휠 줌/드래그 줌/이동이 페이지 스크롤을 가로채지 않게 전부 끈다.
PLOTLY_CONFIG = {
    "scrollZoom": False,
    "doubleClick": False,
    "displayModeBar": False,
}

def freeze_chart(fig):
    """차트의 줌/팬을 비활성화한다 (호버 툴팁은 유지)."""
    fig.update_layout(dragmode=False)
    fig.update_xaxes(fixedrange=True)
    fig.update_yaxes(fixedrange=True)
    return fig

# ── Plotly 호버 하이라이트 — 기본은 어둡게(0.65), 마우스를 올린 지표만 원래 밝기(1.0).
# Streamlit은 차트를 부모 문서에 직접 렌더링하므로, 숨김 iframe에서 부모 DOM의
# 그래프 div에 plotly_hover/unhover를 바인딩한다. 리렌더로 스타일이 초기화될 수 있어
# 주기 틱마다 다시 적용한다(바인딩은 1회만).
# st.iframe은 height=0을 허용하지 않아 1px로 만들고, 키 컨테이너(.st-key-…)를 CSS로 접는다.
_hover_script_box = st.container(key="plotly_hover_script")
_hover_script_box.iframe("""<script>
(function () {
    var P = window.parent;
    var doc = P.document;
    var DIM = '0.65';

    function groups(gd) {
        return gd.querySelectorAll(
            '.barlayer .trace, .scatterlayer .trace, .pielayer .slice'
        );
    }

    function applyDim(gd) {
        var hovered = gd.__dimHovered;
        groups(gd).forEach(function (n) {
            var bright = hovered === 'all' || n === hovered;
            n.style.setProperty('opacity', bright ? '1' : DIM);
        });
    }

    function nodeForPoint(gd, pt) {
        if (!pt) return 'all';
        var slices = gd.querySelectorAll('.pielayer .slice');
        if (slices.length) return slices[pt.pointNumber] || 'all';
        var bars = gd.querySelectorAll('.barlayer .trace');
        if (bars.length) return bars[pt.curveNumber] || 'all';
        var lines = gd.querySelectorAll('.scatterlayer .trace');
        if (lines.length) return lines[pt.curveNumber] || 'all';
        return 'all';
    }

    function tick() {
        doc.querySelectorAll('.js-plotly-plot').forEach(function (gd) {
            try {
                if (gd.on && !gd.__dimBound) {
                    gd.__dimBound = true;
                    gd.on('plotly_hover', function (e) {
                        gd.__dimHovered = nodeForPoint(gd, e.points && e.points[0]);
                        applyDim(gd);
                    });
                    gd.on('plotly_unhover', function () {
                        gd.__dimHovered = null;
                        applyDim(gd);
                    });
                }
                applyDim(gd);
            } catch (err) { /* 차트 준비 전이면 다음 틱에서 재시도 */ }
        });
    }

    if (P.__plotlyDimTimer) { P.clearInterval(P.__plotlyDimTimer); }
    P.__plotlyDimTimer = P.setInterval(tick, 600);
    tick();
})();
</script>""", height=1)

# Helper DB connection
def get_connection():
    return sqlite3.connect(db_path)

def table_exists(conn, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?;",
        (table_name,),
    ).fetchone()
    return row is not None

def ensure_dashboard_schema(conn):
    if table_exists(conn, "match_participants"):
        columns = {row[1] for row in conn.execute("PRAGMA table_info(match_participants);").fetchall()}
        migrations = {
            "cs": "INTEGER NOT NULL DEFAULT 0",
            "total_damage_dealt": "INTEGER NOT NULL DEFAULT 0",
            "total_damage_taken": "INTEGER NOT NULL DEFAULT 0",
            "damage_self_mitigated": "INTEGER NOT NULL DEFAULT 0",
            "total_heal": "INTEGER NOT NULL DEFAULT 0",
            "total_heals_on_teammates": "INTEGER NOT NULL DEFAULT 0",
            "total_damage_shielded_on_teammates": "INTEGER NOT NULL DEFAULT 0",
            "total_damage_dealt_to_objectives": "INTEGER NOT NULL DEFAULT 0",
            "total_damage_dealt_to_turrets": "INTEGER NOT NULL DEFAULT 0",
            "vision_score": "INTEGER NOT NULL DEFAULT 0",
            "wards_placed": "INTEGER NOT NULL DEFAULT 0",
            "wards_killed": "INTEGER NOT NULL DEFAULT 0",
            "control_wards_bought": "INTEGER NOT NULL DEFAULT 0",
            "turret_kills": "INTEGER NOT NULL DEFAULT 0",
            "inhibitor_kills": "INTEGER NOT NULL DEFAULT 0",
            "double_kills": "INTEGER NOT NULL DEFAULT 0",
            "triple_kills": "INTEGER NOT NULL DEFAULT 0",
            "quadra_kills": "INTEGER NOT NULL DEFAULT 0",
            "penta_kills": "INTEGER NOT NULL DEFAULT 0",
            "killing_sprees": "INTEGER NOT NULL DEFAULT 0",
            "largest_killing_spree": "INTEGER NOT NULL DEFAULT 0",
            "largest_multi_kill": "INTEGER NOT NULL DEFAULT 0",
            "champ_level": "INTEGER NOT NULL DEFAULT 0",
            "time_ccing_others": "INTEGER NOT NULL DEFAULT 0",
            "total_minions_killed": "INTEGER NOT NULL DEFAULT 0",
            "neutral_minions_killed": "INTEGER NOT NULL DEFAULT 0",
            "raw_data": "TEXT",
        }
        for column_name, column_type in migrations.items():
            if column_name not in columns:
                conn.execute(f"ALTER TABLE match_participants ADD COLUMN {column_name} {column_type};")

    if table_exists(conn, "matches"):
        match_columns = {row[1] for row in conn.execute("PRAGMA table_info(matches);").fetchall()}
        if "raw_data" not in match_columns:
            conn.execute("ALTER TABLE matches ADD COLUMN raw_data TEXT;")

    if table_exists(conn, "riot_accounts"):
        riot_columns = {row[1] for row in conn.execute("PRAGMA table_info(riot_accounts);").fetchall()}
        riot_migrations = {
            "solo_league_points": "INTEGER",
            "solo_high_tier": "VARCHAR",
        }
        for column_name, column_type in riot_migrations.items():
            if column_name not in riot_columns:
                conn.execute(f"ALTER TABLE riot_accounts ADD COLUMN {column_name} {column_type};")
    conn.commit()

def parse_queue_id(raw_data) -> int | None:
    if not raw_data:
        return None
    try:
        payload = json.loads(raw_data) if isinstance(raw_data, str) else raw_data
        return payload.get("match_basic_dict", {}).get("queue_id")
    except (json.JSONDecodeError, TypeError, AttributeError):
        return None

def format_game_duration(seconds) -> str:
    total_seconds = int(float(seconds))
    minutes = total_seconds // 60
    secs = total_seconds % 60
    return f"{minutes}분 {secs:02d}초"

def get_match_mvp(parts: pd.DataFrame) -> dict | None:
    if parts.empty or parts["ai_score"].isna().all():
        return None
    mvp_row = parts.loc[parts["ai_score"].idxmax()]
    name = mvp_row["display_name"] if pd.notna(mvp_row["display_name"]) and mvp_row["display_name"] else mvp_row["summoner_name"]
    return {
        "name": name,
        "champion": mvp_row["champion_name"],
        "ai_score": mvp_row["ai_score"],
        "kda": mvp_row["kda"],
    }

def build_match_expander_label(match: pd.Series, parts: pd.DataFrame) -> str:
    dt = datetime.fromtimestamp(match["game_creation"] / 1000.0)
    date_str = dt.strftime("%Y-%m-%d %H:%M")
    duration_str = format_game_duration(match["game_duration"])
    result = "🔵 BLUE 승" if bool(match["blue_won"]) else "🔴 RED 승"
    mvp = get_match_mvp(parts)
    mvp_part = f" · MVP {mvp['name']} ({mvp['champion']})" if mvp else ""
    return f"🎮 {date_str} · {duration_str} · {result}{mvp_part}"

def parse_total_damage_dealt(raw_data) -> int:
    if not raw_data:
        return 0
    try:
        payload = json.loads(raw_data) if isinstance(raw_data, str) else raw_data
        final_stats = payload.get("final_stat_dict", {})
        for source in (final_stats, payload):
            for key in ("total_damage_dealt", "totalDamageDealt"):
                if key in source and source[key] is not None:
                    return int(float(source[key]))
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    return 0

def enrich_participants_damage(df: pd.DataFrame) -> pd.DataFrame:
    enriched = df.copy()
    if "total_damage_dealt" not in enriched.columns:
        enriched["total_damage_dealt"] = 0
    enriched["total_damage_dealt"] = pd.to_numeric(
        enriched["total_damage_dealt"], errors="coerce"
    ).fillna(0).astype(int)
    if "raw_data" in enriched.columns:
        missing_mask = enriched["total_damage_dealt"] == 0
        enriched.loc[missing_mask, "total_damage_dealt"] = enriched.loc[
            missing_mask, "raw_data"
        ].apply(parse_total_damage_dealt)
    return enriched

def prepare_team_table(team_df: pd.DataFrame) -> pd.DataFrame:
    table = team_df.copy()
    table["player"] = table["display_name"].fillna(table["summoner_name"])
    table["champ_icon"] = table["champion_name"].map(champ_icon_by_name)
    return table[
        ["player", "champ_icon", "champion_name", "kills", "deaths", "assists", "kda", "ai_score", "total_damage_dealt"]
    ].rename(
        columns={
            "player": "플레이어",
            "champ_icon": "챔프",
            "champion_name": "챔피언",
            "kills": "K",
            "deaths": "D",
            "assists": "A",
            "kda": "KDA",
            "ai_score": "AI점수",
            "total_damage_dealt": "딜량",
        }
    )

TEAM_TABLE_COLUMN_CONFIG = {
    # 챔피언 아이콘 열을 픽셀로 좁혀 이미지 좌우 여백을 줄인다(상세분석 챔피언 표와 동일 폭).
    "챔프": st.column_config.ImageColumn("챔프", width=44),
}
# ImageColumn이 있으면 행 높이가 자동으로 커지므로 명시적으로 압축한다.
TEAM_TABLE_ROW_HEIGHT = 40

def style_team_table(table_df: pd.DataFrame, mvp_name: str | None, highlight_color: str):
    styled = table_df.style.highlight_max(subset=["AI점수"], color=highlight_color).format(
        {
            "KDA": lambda value: f"{float(value):.2f}" if pd.notna(value) else "-",
            "AI점수": lambda value: f"{float(value):.1f}" if pd.notna(value) else "-",
            "딜량": lambda value: f"{int(value):,}" if pd.notna(value) else "-",
        }
    )
    # K/D/A 는 옆 칸(챔피언·KDA)과 살짝 구분되도록 은은한 배경 틴트로 묶는다.
    # 엄청 튀지 않게 낮은 투명도로, 색은 킬=초록 / 데스=빨강 / 어시=회색 으로 구분.
    kda_bg = {
        "K": "rgba(52, 211, 153, 0.15)",
        "D": "rgba(248, 113, 113, 0.15)",
        "A": "rgba(148, 163, 184, 0.15)",
    }
    for col, bg in kda_bg.items():
        if col in table_df.columns:
            styled = styled.set_properties(subset=[col], **{"background-color": bg})
    if mvp_name:
        def highlight_mvp(row):
            if row["플레이어"] == mvp_name:
                return ["background-color: rgba(255, 215, 0, 0.22)"] * len(row)
            return [""] * len(row)

        styled = styled.apply(highlight_mvp, axis=1)
    return styled

def render_match_meta_bar(match: pd.Series, parts: pd.DataFrame):
    is_blue_win = bool(match["blue_won"])
    mvp = get_match_mvp(parts)
    queue_id = match.get("queue_id")
    queue_label = f"내전 #{int(queue_id)}" if pd.notna(queue_id) and int(queue_id) == 3130 else (
        f"queue {int(queue_id)}" if pd.notna(queue_id) else "queue ?"
    )
    linked_players = int(match.get("linked_players", 0))
    total_players = int(match.get("total_players", 0))
    win_class = "win-blue" if is_blue_win else "win-red"
    win_label = "BLUE 승리" if is_blue_win else "RED 승리"
    mvp_chip = ""
    if mvp:
        mvp_img = inline_img(champ_icon_by_name(mvp["champion"]), size=18)
        mvp_chip = (
            f"<span class='match-chip mvp'>⭐ MVP {mvp['name']} · {mvp_img} {mvp['champion']} "
            f"(AI {float(mvp['ai_score']):.1f})</span>"
        )
    st.markdown(
        f"""
        <div class="match-meta-bar">
            <span class="match-chip">{queue_label}</span>
            <span class="match-chip {win_class}">{win_label}</span>
            <span class="match-chip">연동 {linked_players}/{total_players}</span>
            <span class="match-chip">ID …{str(match['match_id'])[-8:]}</span>
            {mvp_chip}
        </div>
        """,
        unsafe_allow_html=True,
    )

def render_match_teams(parts: pd.DataFrame):
    mvp = get_match_mvp(parts)
    mvp_name = mvp["name"] if mvp else None
    col_b, col_r = st.columns(2)

    with col_b:
        st.markdown("<div class='team-panel-title blue'>🔵 Blue Team</div>", unsafe_allow_html=True)
        blue_team_players = parts[parts["team_id"] == 100]
        if blue_team_players.empty:
            st.caption("참가자 데이터 없음")
        else:
            blue_table = prepare_team_table(blue_team_players)
            styled_blue = style_team_table(blue_table, mvp_name, "#0f4c3a")
            st.dataframe(styled_blue, width="stretch", hide_index=True, row_height=TEAM_TABLE_ROW_HEIGHT, column_config=TEAM_TABLE_COLUMN_CONFIG)

    with col_r:
        st.markdown("<div class='team-panel-title red'>🔴 Red Team</div>", unsafe_allow_html=True)
        red_team_players = parts[parts["team_id"] == 200]
        if red_team_players.empty:
            st.caption("참가자 데이터 없음")
        else:
            red_table = prepare_team_table(red_team_players)
            styled_red = style_team_table(red_table, mvp_name, "#7d2e2e")
            st.dataframe(styled_red, width="stretch", hide_index=True, row_height=TEAM_TABLE_ROW_HEIGHT, column_config=TEAM_TABLE_COLUMN_CONFIG)

def render_match_block(match: pd.Series, parts: pd.DataFrame):
    match_id = match["match_id"]
    match_parts = parts[parts["match_id"] == match_id]
    label = build_match_expander_label(match, match_parts)

    with st.expander(label, expanded=False):
        render_match_meta_bar(match, match_parts)
        render_match_teams(match_parts)

def get_latest_profile_icon_id(p_parts: pd.DataFrame) -> int | None:
    """플레이어의 가장 최근 경기 raw_data에서 소환사 프로필 아이콘 ID를 찾는다."""
    if p_parts.empty or "raw_data" not in p_parts.columns:
        return None
    ordered = p_parts.sort_values("game_creation", ascending=False, na_position="last")
    for raw in ordered["raw_data"]:
        if not raw:
            continue
        try:
            icon_id = json.loads(raw).get("profile_icon")
        except (json.JSONDecodeError, TypeError):
            continue
        if icon_id:
            return int(icon_id)
    return None

def format_tier_label(tier, division=None, league_points=None) -> str:
    if not tier or str(tier).upper() == "UNRANKED":
        return "언랭"
    div_label = DIVISION_ROMAN.get(division, str(division or "")).strip()
    label = f"{tier} {div_label}".strip()
    if league_points is not None:
        label = f"{label} ({int(league_points)}LP)"
    return label

def resolve_account_tier(account_row: pd.Series) -> dict:
    tier = account_row.get("solo_tier")
    if pd.isna(tier) or not str(tier).strip():
        tier = None
    division = account_row.get("solo_division")
    if pd.notna(division):
        division = int(division)
    else:
        division = None
    league_points = account_row.get("solo_league_points")
    if pd.notna(league_points):
        league_points = int(league_points)
    else:
        league_points = None
    high_tier = account_row.get("solo_high_tier")
    # DB에서 NaN(float)으로 올 수 있어 문자열/None으로 정규화한다.
    if pd.isna(high_tier) or not str(high_tier).strip():
        high_tier = None
    else:
        high_tier = str(high_tier).strip()
    return {
        "solo_tier": tier,
        "solo_division": division,
        "solo_league_points": league_points,
        "solo_high_tier": high_tier,
        "current_tier_label": format_tier_label(tier, division, league_points),
        "high_tier_label": high_tier or "-",
    }

def build_player_account_summary(player_id: int, df_accounts: pd.DataFrame) -> dict:
    accounts = df_accounts[df_accounts["player_id"] == player_id]
    if accounts.empty:
        return {
            "riot_accounts": "-",
            "current_tier": None,
            "current_tier_label": "-",
            "high_tier_label": "-",
            "account_details": [],
        }

    account_details = []
    high_tiers = set()
    for _, account in accounts.iterrows():
        tier_info = resolve_account_tier(account)
        if tier_info["high_tier_label"] and tier_info["high_tier_label"] != "-":
            high_tiers.add(tier_info["high_tier_label"])
        account_details.append({
            "riot_id": f"{account['riot_game_name']}#{account['riot_tag']}",
            "main_account": bool(account.get("main_account")),
            **tier_info,
        })

    main_accounts = [item for item in account_details if item["main_account"]]
    primary = main_accounts[0] if main_accounts else account_details[0]
    riot_ids = [item["riot_id"] for item in account_details]

    return {
        "riot_accounts": ", ".join(riot_ids),
        "current_tier": primary.get("solo_tier"),
        "current_tier_label": primary["current_tier_label"],
        "high_tier_label": ", ".join(sorted(high_tiers)) if high_tiers else primary["high_tier_label"],
        "account_details": account_details,
    }

# Archetype Style Analyzer
def generate_player_archetype(row):
    """
    Categorizes players based on their average statistics to give a fun summary.
    """
    total_games = row['matches_played']
    if total_games < MIN_STATS_MATCHES:
        return "표본 수집 중"
        
    avg_kda = row['avg_kda']
    avg_kills = row['avg_kills']
    avg_deaths = row['avg_deaths']
    avg_assists = row.get('avg_assists', 0) or 0
    avg_damage = row['avg_damage']
    avg_damage_taken = row.get('avg_damage_taken', 0) or 0
    avg_cs = row.get('avg_cs', 0) or 0
    # 시야점수(vision_score)는 DeepLOL 데이터가 전부 0이라 쓰지 않고, 실측 와드 수로 시야를 판단한다.
    avg_wards = row.get('avg_wards_placed', 0) or 0
    avg_control_wards = row.get('avg_control_wards', 0) or 0
    avg_ai = row['avg_ai_score']
    main_pos = row['main_pos']

    # Subcategory check (포지션별 세분화)
    if main_pos == "TOP":
        if avg_kills > 6.0 and avg_damage > 18000:
            return "라인전부터 찍어누르는 공격형 탑"
        elif avg_deaths > 6.0 and avg_damage > 18000:
            return "끊임없이 싸움을 거는 전투형 탑"
        elif avg_damage_taken > 28000:
            return "한타를 여는 돌격형 탑"
        elif avg_cs > 200 and avg_kills < 5.0:
            return "사이드를 굴리는 스플릿 운영형 탑"
        elif avg_kda > 3.0 and avg_deaths < 4.0:
            return "좀처럼 안 죽는 철벽 탑라이너"
        elif avg_deaths > 6.5:
            return "몸을 아끼지 않는 다이브형 탑"
        return "묵묵히 버텨내는 탑라이너"

    elif main_pos == "JUNGLE":
        if avg_kills > 6.0:
            return "갱킹으로 굴리는 육식형 정글"
        elif avg_assists > 9.0:
            return "한타를 설계하는 정글"
        elif avg_damage > 18000 and avg_kda > 3.0:
            return "딜까지 챙기는 캐리형 정글"
        elif avg_wards >= 12.0 or avg_control_wards >= 6.0:
            return "맵을 장악하는 운영형 정글"
        elif avg_kda > 3.5:
            return "이득만 챙기는 효율형 정글"
        elif avg_deaths > 6.0:
            return "공격적으로 동선을 짜는 다이브형 정글"
        return "팀을 받쳐주는 든든한 정글러"

    elif main_pos == "MIDDLE":
        if avg_damage > 22000 or avg_kills > 7.0:
            return "폭딜로 게임을 끝내는 캐리형 미드"
        elif avg_assists > 8.0 and avg_wards >= 10.0:
            return "로밍으로 판을 짜는 플레이메이커 미드"
        elif avg_cs > 200:
            return "라인 주도권을 쥐는 파밍형 미드"
        elif avg_kda > 3.5 and avg_deaths < 4.0:
            return "깔끔하게 굴리는 안정형 미드"
        elif avg_ai > 7.5:
            return "라인전이 탄탄한 미드라이너"
        elif avg_deaths > 6.0:
            return "변수를 만드는 공격형 미드"
        return "팀에 녹아드는 조율형 미드라이너"

    elif main_pos == "BOTTOM":
        if avg_damage > 24000:
            return "폭딜을 쏟아내는 하이퍼캐리 원딜"
        elif avg_kda > 4.0 and avg_deaths < 4.0:
            return "포지셔닝이 빛나는 안정형 원딜"
        elif avg_kills > 7.0:
            return "교전을 즐기는 공격형 원딜"
        elif avg_cs > 200:
            return "차근차근 성장하는 파밍형 원딜"
        elif avg_deaths > 5.5:
            return "교전에 자주 휘말리는 원딜"
        return "기본기가 탄탄한 원거리 딜러"

    elif main_pos == "UTILITY":
        # 서포터는 와드가 기본적으로 많아(경기당 30~45개) "와드 많음" 하나로는 전부 운영형이 돼버린다.
        # 그래서 딜/탱/생존/희생을 먼저 가려내고, 남는 경우만 시야·유틸형으로 분류한다.
        if avg_damage_taken > 26000:
            return "몸으로 받아내는 탱커형 서포터"
        elif avg_damage > 13000 and avg_kills > 3.5:
            return "딜을 욱여넣는 공격형 서포터"
        elif avg_deaths >= 5.3:
            return "팀 대신 몸을 던지는 희생형 서포터"
        elif avg_kda >= 4.5 and avg_deaths <= 4.8:
            return "좀처럼 죽지 않는 생존형 서포터"
        elif avg_control_wards >= 9.0 or avg_wards >= 34.0:
            return "시야로 판을 읽는 운영형 서포터"
        elif avg_assists >= 11.0:
            return "팀을 살려내는 유틸형 서포터"
        return "묵묵히 지켜주는 든든한 서포터"

    # Fallback by stats (포지션 미상/혼합)
    if avg_ai >= 7.8:
        return "경기를 읽는 플레이메이커"
    elif avg_kda >= 4.0:
        return "꾸준히 살아남는 안정형 플레이어"
    elif avg_damage >= 22000:
        return "딜을 책임지는 공격형 플레이어"
    elif avg_assists >= 12.0:
        return "보이지 않게 돕는 조력자"
    elif avg_deaths >= 6.5:
        return "앞장서 싸우는 투지형 플레이어"

    return "어디서든 1인분 하는 올라운더"


def render_player_analysis_report(report: dict):
    """구조화된 플레이어 분석 리포트를 카드 UI로 렌더링한다."""
    rating_palette = {"높음": "#34D399", "보통": "#94A3B8", "낮음": "#F87171"}

    def _rating_chip(text: str, quality: str | None = None) -> str:
        # 색상은 품질(좋음/나쁨) 기준, 텍스트는 실제 수치 크기 기준으로 분리한다.
        # 예) 데스 많음 → 텍스트 '높음' + 빨강(나쁨).
        color = rating_palette.get(quality or text, "#94A3B8")
        return (
            f"<span class='rating-chip' style='color:{color};"
            f"background:{color}1f;border:1px solid {color}66'>{text}</span>"
        )

    st.markdown("<div class='section-label'>🧠 AI 종합 리포트</div>", unsafe_allow_html=True)

    # 한줄평 히어로
    main_pos_label = report.get("main_position_label") or POSITION_KR.get(
        report.get("main_position", "UNKNOWN"), "미정"
    )
    sub_pos = report.get("sub_position", "UNKNOWN")
    sub_pos_label = report.get("sub_position_label") or POSITION_KR.get(sub_pos, "미정")
    pos_text = main_pos_label
    if sub_pos and sub_pos != "UNKNOWN":
        pos_text += f" / {sub_pos_label}"
    hero_tags = (
        f"<span class='hero-tag'>포지션 · {pos_text}</span>"
        f"<span class='hero-tag'>유형 · {report.get('player_type', '-')}</span>"
        f"<span class='hero-tag alt'>추천 · {report.get('recommended_style', '-')}</span>"
    )

    # 핵심 수치 캡슐 — 판수/승률/KDA/최근폼을 히어로에서 바로 보여준다.
    summary_rows = report.get("summary_table", [])

    def _summary_row(metric: str) -> dict | None:
        for r in summary_rows:
            if r.get("metric") == metric:
                return r
        return None

    hero_stats: list[tuple[str, str, str]] = [("분석 경기", f"{report.get('matches', 0)}판", pos_text)]
    for metric, label in (("win_rate", "승률"), ("kda", "KDA"), ("dpm", "DPM")):
        row = _summary_row(metric)
        if row:
            base = row.get("formatted_position_avg")
            sub = f"포지션 평균 {base}" if base else ""
            hero_stats.append((label, row["formatted_value"], sub))
    form_for_hero = report.get("recent_form")
    if form_for_hero:
        hero_stats.append((
            "최근 5판",
            f"{form_for_hero.get('recent_win_rate', 0)}%",
            f"{' '.join(form_for_hero.get('last5', []))} {form_for_hero.get('emoji', '')}",
        ))
    hero_stats_html = "".join(
        f"<div class='hero-stat'><div class='hs-k'>{k}</div><div class='hs-v'>{v}</div>"
        f"<div class='hs-s'>{s}</div></div>"
        for k, v, s in hero_stats
    )

    st.markdown(
        f"<div class='hero-card'><div class='hero-tags'>{hero_tags}</div>"
        f"<div class='hero-quote'>“{report.get('one_liner', '')}”</div>"
        f"<div class='hero-stats'>{hero_stats_html}</div></div>",
        unsafe_allow_html=True,
    )

    # 핵심 승리 조건 콜아웃
    if report.get("win_condition"):
        st.markdown(
            f"<div class='callout'><div class='ic'>🎯</div>"
            f"<div class='tx'><b>핵심 승리 조건</b><br>{report['win_condition']}</div></div>",
            unsafe_allow_html=True,
        )

    # 종합 지표 / 포지션별 지표가 공유하는 카드 HTML 생성기.
    def _metric_card_html(row: dict) -> str:
        # 색상은 품질(좋음/나쁨), 텍스트(칩)는 실제 수치 크기 기준으로 분리한다.
        quality = row.get("relative_rating", "보통")
        magnitude = row.get("magnitude_rating", quality)
        color = rating_palette.get(quality, "#94A3B8")
        desc = f"<div class='mc-desc'>{row['desc']}</div>" if row.get("desc") else ""

        # 정량 게이지 — 눈금(틱) 위치가 포지션 평균(100%), 표시 상한 160%.
        pct = row.get("pct_vs_avg")
        base = row.get("formatted_position_avg")
        gauge = ""
        eval_text = row.get("relative_evaluation", "")
        if pct is not None and base is not None:
            fill = min(max(pct, 4), 160) / 160 * 100
            gauge = (
                f"<div class='mc-bar'><div class='fill' style='width:{fill:.0f}%;background:{color}'></div>"
                f"<div class='avg-tick'></div></div>"
                f"<div class='mc-vs'><span>평균 <b>{base}</b></span><span><b>{pct}%</b></span></div>"
            )
            # 게이지가 평균 대비를 수치로 보여주므로 문장 평가는 챔피언 비교만 남긴다.
            champ_parts = [p for p in eval_text.split(" · ") if "챔피언" in p]
            eval_text = " · ".join(champ_parts)
        eval_html = f"<div class='mc-eval'>{eval_text}</div>" if eval_text else ""
        return (
            f"<div class='metric-card' style='border-left:4px solid {color}'>"
            f"<div class='mc-head'><span class='mc-label'>{row['label']}</span>{_rating_chip(magnitude, quality)}</div>"
            f"{desc}<div class='mc-value'>{row['formatted_value']}</div>"
            f"{gauge}{eval_html}</div>"
        )

    # 종합 지표 카드 그리드 (모든 포지션 통합)
    if summary_rows:
        st.markdown("<div class='section-label'>📊 종합 지표 — 게이지 눈금 = 포지션 평균</div>", unsafe_allow_html=True)
        st.caption(
            "여러 포지션을 소화한 플레이어는 비교 기준도 포지션 구성비로 가중합니다 "
            "(예: 원딜 8판 + 서폿 8판 → 기준 = 원딜 평균 50% + 서폿 평균 50%). "
            "포지션별 순수 비교는 아래 '포지션별 지표' 탭에서 확인하세요."
        )
        cards = [_metric_card_html(row) for row in summary_rows]
        st.markdown(f"<div class='metric-grid'>{''.join(cards)}</div>", unsafe_allow_html=True)

    # ⚔️ 라인전 → 후반 정량 분석 (lane_stat_dict vs final_stat_dict)
    lane_final = report.get("lane_final_analysis")
    if lane_final and lane_final.get("items"):
        st.markdown("<div class='section-label'>⚔️ 라인전 → 후반 정량 분석</div>", unsafe_allow_html=True)
        st.caption(
            f"라인전 종료 시점 스탯과 경기 최종 스탯을 비교합니다 "
            f"({lane_final.get('games', 0)}판 · 포지션 구성비 가중 평균 대비)"
        )
        lf_cards = []
        for it in lane_final["items"]:
            color = rating_palette.get(it.get("rating", "보통"), "#94A3B8")
            lf_cards.append(
                f"<div class='metric-card' style='border-left:4px solid {color}'>"
                f"<div class='mc-head'><span class='mc-label'>{it.get('emoji','')} {it['label']}</span>"
                f"{_rating_chip(it.get('rating', '보통'))}</div>"
                f"<div class='mc-value'>{it['value']}</div>"
                f"<div class='mc-eval'>{it['detail']}</div></div>"
            )
        st.markdown(f"<div class='metric-grid'>{''.join(lf_cards)}</div>", unsafe_allow_html=True)

    # 포지션별 지표 — 통합 지표는 원딜/서폿을 오가는 플레이어에서 왜곡되므로,
    # 플레이어가 실제로 플레이한 포지션마다 같은 포지션 평균과 비교해 따로 보여준다.
    position_breakdown = report.get("position_breakdown", [])
    if position_breakdown:
        st.markdown("<div class='section-label'>🎯 포지션별 지표</div>", unsafe_allow_html=True)
        st.caption("각 포지션의 경기만 모아, 같은 포지션 전체 평균과 비교합니다. 🔒는 표본 부족.")
        tab_labels = [
            f"{pb['position_label']} ({pb['games']}판)" + (" 🔒" if pb.get("locked") else "")
            for pb in position_breakdown
        ]
        for tab, pb in zip(st.tabs(tab_labels), position_breakdown):
            with tab:
                if pb.get("locked"):
                    st.info(
                        f"표본 부족 ({pb['games']}/{pb['min_games']}판) — "
                        f"{pb['min_games']}판 이상 쌓이면 **{pb['position_label']}** 지표가 공개됩니다. 🔒"
                    )
                    continue
                pos_cards = [_metric_card_html(row) for row in pb.get("summary_table", [])]
                st.markdown(f"<div class='metric-grid'>{''.join(pos_cards)}</div>", unsafe_allow_html=True)

    # 강점 / 약점
    def _sw_cards(items: list, kind: str) -> str:
        cls = "good" if kind == "strength" else "bad"
        out = []
        for it in items:
            desc = f" <span class='sw-desc'>({it['desc']})</span>" if it.get("desc") else ""
            out.append(
                f"<div class='sw-card {cls}'><div class='sw-main'>"
                f"<div class='sw-name'>{it['label']}{desc}</div>"
                f"<div class='sw-sub'>{it['description']}</div></div>"
                f"<div class='sw-val'>{it['value']}</div></div>"
            )
        return "".join(out) or "<div class='sw-sub'>데이터 부족</div>"

    strengths = report.get("strengths_top3", [])
    weaknesses = report.get("weaknesses_top3", [])
    if strengths or weaknesses:
        st.markdown("<div class='section-label'>⚖️ 강점 &amp; 약점</div>", unsafe_allow_html=True)
        st.markdown(
            "<div class='sw-grid'>"
            f"<div><div class='sw-col-title' style='color:#34D399'>💪 강점 TOP 3</div>{_sw_cards(strengths, 'strength')}</div>"
            f"<div><div class='sw-col-title' style='color:#F87171'>⚠️ 약점 TOP 3</div>{_sw_cards(weaknesses, 'weakness')}</div>"
            "</div>",
            unsafe_allow_html=True,
        )

    # 승패 요인 — 승리/패배 경기의 지표 평균을 수치로 비교한다.
    st.markdown("<div class='section-label'>📈 승패 요인</div>", unsafe_allow_html=True)
    wl_detail = report.get("win_loss_detail", [])
    if wl_detail:
        st.caption("승리 경기와 패배 경기의 지표 평균 비교 (Δ = 승리 − 패배)")

        def _wl_rows(items: list, positive: bool) -> str:
            rows_html = []
            for d in items:
                win_t = d.get("win_text") or f"{d.get('win_value', 0):.2f}"
                loss_t = d.get("loss_text") or f"{d.get('loss_value', 0):.2f}"
                delta = d.get("diff_text") or f"{d.get('diff', 0):+.2f}"
                cls = "up" if positive else "down"
                rows_html.append(
                    f"<div class='wl-row'><div class='wl-lab'>{d['label']}</div>"
                    f"<div class='wl-num'>승 <b>{win_t}</b></div>"
                    f"<div class='wl-num'>패 <b>{loss_t}</b></div>"
                    f"<div class='wl-delta {cls}'>Δ {delta}</div></div>"
                )
            return "".join(rows_html) or "<div class='sw-sub'>표본이 부족합니다.</div>"

        positives = [d for d in wl_detail if d.get("impact", 0) > 0][:3]
        negatives = [d for d in sorted(wl_detail, key=lambda x: x.get("impact", 0)) if d.get("impact", 0) < 0][:3]
        st.markdown(
            "<div class='sw-grid'>"
            f"<div class='pattern-card win'><div class='pc-title' style='color:#34D399'>✅ 승리 경기에서 앞선 지표</div>{_wl_rows(positives, True)}</div>"
            f"<div class='pattern-card loss'><div class='pc-title' style='color:#F87171'>❌ 승리로 연결되지 않은 지표</div>{_wl_rows(negatives, False)}</div>"
            "</div>",
            unsafe_allow_html=True,
        )
    else:
        # 상세 비교가 없으면(승/패 어느 한쪽 표본 부족 등) 문장형 패턴으로 대체.
        def _pattern_list(lines: list) -> str:
            if not lines:
                return "<li style='color:#7C8A92'>표본이 부족합니다.</li>"
            return "".join(f"<li>{line}</li>" for line in lines)

        st.markdown(
            "<div class='sw-grid'>"
            f"<div class='pattern-card win'><div class='pc-title' style='color:#34D399'>✅ 승리 패턴</div>"
            f"<ul>{_pattern_list(report.get('win_patterns', []))}</ul></div>"
            f"<div class='pattern-card loss'><div class='pc-title' style='color:#F87171'>❌ 패배 패턴</div>"
            f"<ul>{_pattern_list(report.get('loss_patterns', []))}</ul></div>"
            "</div>",
            unsafe_allow_html=True,
        )

    # 플레이 스타일 적합도 — 상위 스타일이 강조되는 수평 바.
    style_scores = report.get("player_type_scores", {})
    if style_scores:
        st.markdown("<div class='section-label'>🎭 플레이 스타일 적합도</div>", unsafe_allow_html=True)
        ranked = sorted(style_scores.items(), key=lambda kv: kv[1], reverse=True)
        max_score = max(ranked[0][1], 0.01)
        style_rows = []
        for i, (name, score) in enumerate(ranked):
            width = max(score / max_score * 100, 3)
            top_cls = " top" if i == 0 else ""
            style_rows.append(
                f"<div class='style-row'><div class='style-name'>{'👑 ' if i == 0 else ''}{name}</div>"
                f"<div class='style-track'><div class='style-fill{top_cls}' style='width:{width:.0f}%'></div></div>"
                f"<div class='style-score'>{score:.2f}</div></div>"
            )
        st.markdown(f"<div class='pattern-card'>{''.join(style_rows)}</div>", unsafe_allow_html=True)

    champ = report.get("champion_analysis", {})
    if champ.get("best") or champ.get("worst"):
        st.markdown("<div class='section-label'>⭐ 챔피언 분석</div>", unsafe_allow_html=True)

        def _champ_highlight_card(item: dict, kind: str, title: str) -> str:
            champ_uri = champ_icon_by_name(item["champion_name"])
            img = f"<img class='champ-face' src='{champ_uri}'>" if champ_uri else ""
            return (
                f"<div class='sw-card {kind}'>{img}<div class='sw-main'>"
                f"<div class='sw-name'>{title} · {item['champion_name']}</div>"
                f"<div class='sw-sub'>{item['games']}판 · 승률 {item['win_rate']:.1f}% · KDA {item['kda']:.2f}</div>"
                f"</div></div>"
            )

        highlight_cards = []
        if champ.get("best"):
            highlight_cards.append(_champ_highlight_card(champ["best"], "good", "가장 잘하는 챔피언"))
        if champ.get("worst"):
            highlight_cards.append(_champ_highlight_card(champ["worst"], "bad", "가장 부진한 챔피언"))
        st.markdown(f"<div class='sw-grid'>{''.join(highlight_cards)}</div>", unsafe_allow_html=True)

    if champ.get("table"):
        champ_df = pd.DataFrame(champ["table"])
        champ_df["icon"] = champ_df["champion_name"].map(champ_icon_by_name)
        champ_display = champ_df[["icon", "champion_name", "games", "win_rate", "kda", "cspm", "dpm", "gpm"]].rename(
            columns={
                "icon": "아이콘",
                "champion_name": "챔피언",
                "games": "판수",
                "win_rate": "승률(%)",
                "kda": "KDA",
                "cspm": "CSPM",
                "dpm": "DPM",
                "gpm": "GPM",
            }
        )
        st.dataframe(
            champ_display.style.format({
                "승률(%)": "{:.1f}",
                "KDA": "{:.2f}",
                "CSPM": "{:.1f}",
                "DPM": "{:.0f}",
                "GPM": "{:.0f}",
            }),
            # width="content"로 표를 내용 폭에 맞춘다. 기본값 "stretch"는 열을 컨테이너 전체 폭에
            # 비례 확대해 아이콘 열(44px)까지 늘어나 좌우 여백이 생겼다.
            width="content",
            hide_index=True,
            # ImageColumn이 있으면 행 높이가 자동으로 커져 이미지 대비 칸이 넓어진다. 압축한다.
            row_height=40,
            column_config={"아이콘": st.column_config.ImageColumn("", width=44)},
        )

    # 🧬 플레이 성향 · 폼 (핑 성향 / 라인전·한타 / 최근 폼 / 승부 기질)
    ping = report.get("ping_profile")
    lane = report.get("lane_performance")
    form = report.get("recent_form")
    momentum = report.get("momentum")

    def _stars(n) -> str:
        try:
            n = int(n)
        except (TypeError, ValueError):
            n = 0
        n = max(0, min(5, n))
        return "★" * n + "☆" * (5 - n)

    def _trait_card(emoji: str, label: str, value: str, sub: str, color: str = "#66FCF1") -> str:
        return (
            f"<div class='metric-card' style='border-left:4px solid {color}'>"
            f"<div class='mc-head'><span class='mc-label'>{emoji} {label}</span></div>"
            f"<div class='mc-value' style='font-size:1.05rem'>{value}</div>"
            f"<div class='mc-eval'>{sub}</div></div>"
        )

    if any([ping, lane, form, momentum]):
        st.markdown("<div class='section-label'>🧬 플레이 성향 · 폼</div>", unsafe_allow_html=True)
        trait_cards = []
        if form:
            trait_cards.append(_trait_card(
                form.get("emoji", ""), "최근 폼", form.get("tag", ""),
                f"{' '.join(form.get('last5', []))} · 최근 승률 {form.get('recent_win_rate', 0)}%",
            ))
        if ping:
            trait_cards.append(_trait_card(
                ping.get("emoji", ""), "핑 성향", ping.get("tag", ""),
                f"{ping.get('description', '')} · 경기당 {ping.get('avg_total', 0)}핑",
            ))
        if lane:
            trait_cards.append(_trait_card(
                lane.get("growth_emoji", ""), "라인전 → 한타", lane.get("growth_tag", ""),
                f"라인전 {_stars(lane.get('lane_stars'))} · 한타 {_stars(lane.get('teamfight_stars'))}"
                f" · AI {lane.get('lane_ai', 0)} → {lane.get('final_ai', 0)}"
                f" · 팀 내 {lane.get('lane_rank', 0)}위 → {lane.get('final_rank', 0)}위",
            ))
        st.markdown(f"<div class='metric-grid'>{''.join(trait_cards)}</div>", unsafe_allow_html=True)
        if momentum and momentum.get("tags"):
            chips = " ".join(
                f"<span class='rating-chip' style='color:#66FCF1;background:#66FCF11f;border:1px solid #66FCF166'>"
                f"{t['emoji']} {t['label']} · {t['desc']}</span>"
                for t in momentum["tags"]
            )
            st.markdown(f"<div style='margin-top:8px'>{chips}</div>", unsafe_allow_html=True)

    # 🤝 관계 분석 (시너지 / 천적)
    syn = report.get("synergy")
    if syn:
        st.markdown("<div class='section-label'>🤝 관계 분석</div>", unsafe_allow_html=True)
        rel_specs = [
            ("best_duo", "good", "🤝 최고의 듀오", "같은 팀일 때 승률"),
            ("favorite_prey", "good", "😎 밥줄", "상대로 만났을 때 내 승률"),
            ("worst_duo", "bad", "🧊 안 맞는 듀오", "같은 팀일 때 승률"),
            ("nemesis", "bad", "⚔️ 천적", "상대로 만났을 때 내 승률"),
        ]
        rel_cards = []
        for key, kind, title, phrase in rel_specs:
            item = syn.get(key)
            if not item:
                continue
            rel_cards.append(
                f"<div class='sw-card {kind}'><div class='sw-main'>"
                f"<div class='sw-name'>{title} · {item['name']}</div>"
                f"<div class='sw-sub'>{phrase} {item['win_rate']}% ({item['wins']}승 {item['losses']}패)</div>"
                f"</div><div class='sw-val'>{item['win_rate']}%</div></div>"
            )
        if rel_cards:
            st.caption(f"함께/상대로 {syn.get('min_games', 2)}경기 이상 만난 플레이어 기준")
            st.markdown(f"<div class='sw-grid'>{''.join(rel_cards)}</div>", unsafe_allow_html=True)

def load_data():
    conn = get_connection()
    ensure_dashboard_schema(conn)

    if not table_exists(conn, "players"):
        conn.close()
        return None
    
    # 1. Fetch player records with stats
    player_query = """
    SELECT 
        p.id, 
        p.display_name, 
        p.mmr, 
        p.created_at,
        COUNT(mp.id) as matches_played,
        SUM(CASE WHEN mp.win = 1 THEN 1 ELSE 0 END) as wins,
        SUM(CASE WHEN mp.win = 0 THEN 1 ELSE 0 END) as losses,
        AVG(mp.kills) as avg_kills,
        AVG(mp.deaths) as avg_deaths,
        AVG(mp.assists) as avg_assists,
        AVG(mp.kda) as avg_kda,
        AVG(mp.total_damage_taken) as avg_damage_taken,
        AVG(mp.gold_earned) as avg_gold,
        AVG(mp.cs) as avg_cs,
        AVG(mp.vision_score) as avg_vision_score,
        AVG(mp.wards_placed) as avg_wards_placed,
        AVG(mp.wards_killed) as avg_wards_killed,
        AVG(mp.control_wards_bought) as avg_control_wards,
        SUM(mp.penta_kills) as penta_kills,
        SUM(mp.quadra_kills) as quadra_kills,
        SUM(mp.triple_kills) as triple_kills,
        AVG(mp.ai_score) as avg_ai_score
    FROM players p
    LEFT JOIN match_participants mp ON p.id = mp.player_id
    GROUP BY p.id
    """
    df_players = pd.read_sql(player_query, conn)
    numeric_columns = [
        'matches_played', 'wins', 'losses', 'avg_kills', 'avg_deaths', 'avg_assists',
        'avg_kda', 'avg_damage_taken', 'avg_gold', 'avg_cs', 'avg_vision_score',
        'avg_wards_placed', 'avg_wards_killed', 'avg_control_wards',
        'penta_kills', 'quadra_kills', 'triple_kills', 'avg_ai_score'
    ]
    df_players[numeric_columns] = df_players[numeric_columns].fillna(0)
    
    # Resolve main/sub position for each player — 최신 경기 가중(recency)으로 산정해
    # 판수가 쌓이며 주 라인이 바뀌면 실시간으로 반영된다.
    pos_query = """
    SELECT mp.player_id, mp.position
    FROM match_participants mp
    JOIN matches m ON mp.match_id = m.match_id
    WHERE mp.player_id IS NOT NULL AND mp.position != 'UNKNOWN'
    ORDER BY m.game_creation DESC
    """
    df_pos = pd.read_sql(pos_query, conn)

    main_positions = {}
    sub_positions = {}
    for pid in df_players['id'].unique():
        ordered = df_pos[df_pos['player_id'] == pid]['position'].tolist()  # 최신순
        main_pos, sub_pos = position_service.main_and_sub_position(ordered)
        main_positions[pid] = main_pos
        sub_positions[pid] = sub_pos

    df_players['main_pos'] = df_players['id'].map(main_positions)
    df_players['sub_pos'] = df_players['id'].map(sub_positions)
    df_players['main_pos_kr'] = df_players['main_pos'].map(lambda p: POSITION_KR.get(p, "미정"))
    df_players['sub_pos_kr'] = df_players['sub_pos'].map(lambda p: POSITION_KR.get(p, "미정"))
    df_players['win_rate'] = (df_players['wins'] / df_players['matches_played'].replace(0, 1)) * 100
    df_players.loc[df_players['matches_played'] == 0, 'win_rate'] = 0.0
    df_players['qualified_stats'] = df_players['matches_played'] >= MIN_STATS_MATCHES

    # 2. Fetch matches log
    match_query = """
    SELECT m.match_id, m.game_creation, m.game_duration, m.game_mode, m.raw_data,
           SUM(CASE WHEN mp.team_id = 100 AND mp.win = 1 THEN 1 ELSE 0 END) as blue_won,
           SUM(CASE WHEN mp.player_id IS NOT NULL THEN 1 ELSE 0 END) as linked_players,
           COUNT(mp.id) as total_players
    FROM matches m
    JOIN match_participants mp ON m.match_id = mp.match_id
    GROUP BY m.match_id
    ORDER BY m.game_creation DESC
    """
    df_matches = pd.read_sql(match_query, conn)
    df_matches["queue_id"] = df_matches["raw_data"].apply(parse_queue_id)
    
    # 3. Fetch detailed participants
    part_query = """
    SELECT mp.*, p.display_name, m.game_creation, m.game_duration
    FROM match_participants mp
    LEFT JOIN players p ON mp.player_id = p.id
    LEFT JOIN matches m ON mp.match_id = m.match_id
    """
    df_participants = pd.read_sql(part_query, conn)
    df_participants = enrich_champion_names(df_participants)
    df_participants = enrich_participants_damage(df_participants)

    avg_damage_by_player = (
        df_participants[df_participants["player_id"].notna()]
        .groupby("player_id")["total_damage_dealt"]
        .mean()
    )
    df_players["avg_damage"] = df_players["id"].map(avg_damage_by_player).fillna(0)
    df_players["archetype"] = df_players.apply(generate_player_archetype, axis=1)

    accounts_query = """
    SELECT
        ra.player_id,
        ra.riot_game_name,
        ra.riot_tag,
        ra.main_account,
        ra.puuid,
        ra.summoner_id,
        ra.solo_tier,
        ra.solo_division,
        ra.solo_league_points,
        ra.solo_high_tier
    FROM riot_accounts ra
    ORDER BY ra.main_account DESC, ra.id ASC
    """
    df_accounts = pd.read_sql(accounts_query, conn)
    
    conn.close()
    return df_players, df_matches, df_participants, df_accounts

# Load data initially
if not os.path.exists(db_path):
    st.error(f"데이터베이스 파일 ({db_path})이 존재하지 않습니다. 먼저 봇을 실행하거나 플레이어를 추가해주세요.")
    st.stop()

loaded = load_data()
if loaded is None:
    st.warning("데이터베이스 스키마가 아직 생성되지 않았습니다. 백엔드 API를 먼저 실행해주세요.")
    st.stop()
df_players, df_matches, df_participants, df_accounts = loaded

account_summaries = {
    player_id: build_player_account_summary(player_id, df_accounts)
    for player_id in df_players["id"].unique()
}
df_players["riot_accounts"] = df_players["id"].map(lambda pid: account_summaries[pid]["riot_accounts"])
df_players["current_tier"] = df_players["id"].map(lambda pid: account_summaries[pid].get("current_tier"))
df_players["current_tier_label"] = df_players["id"].map(lambda pid: account_summaries[pid]["current_tier_label"])
df_players["high_tier_label"] = df_players["id"].map(lambda pid: account_summaries[pid]["high_tier_label"])

# 자동 태그 — 저장 없이 매 로드마다 전적에서 재계산하므로 새 경기가 동기화되면 즉시 반영된다.
tag_baselines = player_tags.build_baselines(df_participants)

def get_player_tags(player_id) -> list[dict]:
    return player_tags.compute_tags(int(player_id), tag_baselines)

def render_tag_chips(tags: list[dict], css_class: str = "ptag") -> str:
    return "".join(
        f"<span class='{css_class} {t['kind']}' title=\"{t['reason']}\">{t['emoji']} {t['label']}</span>"
        for t in tags
    )

# Navigation
st.sidebar.markdown("<h1 style='text-align: center;'>🏆 GOL League</h1>", unsafe_allow_html=True)
menu = st.sidebar.radio(
    "메뉴 선택",
    ["대시보드 홈", "플레이어 랭킹", "플레이어 상세 분석", "팀 밸런스 시뮬레이터", "경기 전적 기록"]
)

# Render chosen tab
if menu == "대시보드 홈":
    st.title("📊 내전 관리 시스템 대시보드")
    st.caption("GOL League · 내전 전적과 플레이어 성향을 한눈에")

    # High-level Metrics Row
    m_col1, m_col2, m_col3, m_col4 = st.columns(4)
    total_players = len(df_players)
    total_games = len(df_matches)
    
    # Calculate league average KDA
    league_kda = (df_participants['kills'].sum() + df_participants['assists'].sum()) / max(1, df_participants['deaths'].sum())
    
    m_col1.metric("총 플레이어 수", f"{total_players} 명")
    m_col2.metric("총 경기 수", f"{total_games} 판")
    m_col3.metric("리그 평균 KDA", f"{league_kda:.2f}")
    
    # Fetch most pick champion
    if not df_participants.empty:
        champ_counts = df_participants['champion_name'].value_counts()
        most_champ = champ_counts.index[0]
        most_champ_count = champ_counts.values[0]
        most_champ_uri = champ_icon_by_name(most_champ)
        if most_champ_uri:
            m_col4.markdown(
                f"<div class='img-metric'><img src='{most_champ_uri}'>"
                f"<div><div class='im-label'>최다 픽 챔피언</div>"
                f"<div class='im-value'>{most_champ} <span class='im-sub'>({most_champ_count}회)</span></div>"
                f"</div></div>",
                unsafe_allow_html=True,
            )
        else:
            m_col4.metric("최다 픽 챔피언", f"{most_champ} ({most_champ_count}회)")
    else:
        m_col4.metric("최다 픽 챔피언", "전적 없음")

    # Main Visual Layout
    col1, col2 = st.columns([2, 1])
    
    with col1:
        st.subheader("🔥 실시간 MMR 탑 5")
        top_5_mmr = df_players.sort_values(by="mmr", ascending=False).head(5)
        top_5_display = top_5_mmr[["display_name", "riot_accounts", "current_tier_label", "high_tier_label", "mmr"]].copy()
        top_5_display.insert(2, "tier_icon", top_5_mmr["current_tier"].map(tier_emblem))
        top_5_display.columns = ["디스코드 닉네임", "라이엇 계정", "티어", "현재 솔로 티어", "최고 티어", "MMR"]
        # 순위가 한눈에 들어오도록 상위 3명은 메달, 나머지는 등수를 닉네임 앞에 붙인다.
        top_5_display = top_5_display.reset_index(drop=True)
        rank_medals = {0: "🥇", 1: "🥈", 2: "🥉"}
        top_5_display["순위"] = [rank_medals.get(i, f"{i + 1}위") for i in range(len(top_5_display))]
        top_5_display = top_5_display[["순위", "디스코드 닉네임", "라이엇 계정", "티어", "현재 솔로 티어", "최고 티어", "MMR"]]

        # 1·2·3등 행에 금/은/동 배경 틴트를 깔아 상위권을 강조한다.
        rank_row_bg = {
            0: "rgba(255, 215, 0, 0.22)",    # 🥇 골드
            1: "rgba(203, 213, 225, 0.18)",  # 🥈 실버
            2: "rgba(205, 127, 50, 0.20)",   # 🥉 브론즈
        }

        def _highlight_top3(row):
            color = rank_row_bg.get(row.name)
            return [f"background-color: {color}" if color else ""] * len(row)

        styled_top5 = top_5_display.style.apply(_highlight_top3, axis=1).format({"MMR": "{:.0f}"})
        st.dataframe(
            styled_top5,
            width="stretch",
            hide_index=True,
            column_config={
                "순위": st.column_config.TextColumn("순위", width="small"),
                "티어": st.column_config.ImageColumn("티어", width="small"),
            },
        )
        
        # Plot MMR leaderboard chart
        # 막대 색을 각 플레이어의 주 라인 색(POSITION_COLORS)에 맞춘다.
        # 트레이스 구조(플레이어당 막대 1개·호버 하이라이트)를 유지하려고
        # color는 display_name 그대로 두고 이름→라인색 매핑만 주입한다.
        line_color_map = {
            r["display_name"]: POSITION_COLORS.get(r["main_pos_kr"], POSITION_COLORS["미정"])
            for _, r in top_5_mmr.iterrows()
        }
        fig_mmr = px.bar(
            top_5_mmr,
            x="display_name",
            y="mmr",
            color="display_name",
            color_discrete_map=line_color_map,
            labels={"display_name": "플레이어", "mmr": "MMR"},
            text_auto=True
        )
        fig_mmr.update_layout(
            paper_bgcolor='rgba(0,0,0,0)',
            plot_bgcolor='rgba(0,0,0,0)',
            font_color="#FFFFFF",
            showlegend=False
        )
        freeze_chart(fig_mmr)
        st.plotly_chart(fig_mmr, width="stretch", config=PLOTLY_CONFIG)

    with col2:
        st.subheader("📍 포지션 분포도")
        pos_counts = df_players[df_players['main_pos'] != 'UNKNOWN']['main_pos_kr'].value_counts().reset_index()
        pos_counts.columns = ['Position', 'Count']
        
        fig_pie = px.pie(
            pos_counts,
            values="Count",
            names="Position",
            color="Position",
            color_discrete_map=POSITION_COLORS,
        )
        fig_pie.update_traces(
            textinfo="label+percent",
            marker=dict(line=dict(color="#0B0C10", width=2)),
            # 호버 하이라이트 JS가 pointNumber로 DOM 조각을 찾으므로
            # 그리는 순서와 데이터 순서를 일치시킨다 (데이터가 이미 내림차순 정렬).
            sort=False,
        )
        fig_pie.update_layout(
            paper_bgcolor='rgba(0,0,0,0)',
            plot_bgcolor='rgba(0,0,0,0)',
            font_color="#FFFFFF"
        )
        freeze_chart(fig_pie)
        st.plotly_chart(fig_pie, width="stretch", config=PLOTLY_CONFIG)

    # Recent Matches Grid
    st.subheader("🕒 최근 경기 요약")
    if not df_matches.empty:
        recent_5 = df_matches.head(5)
        for _, match in recent_5.iterrows():
            render_match_block(match, df_participants)
    else:
        st.info("동기화된 내전 경기가 없습니다. 디스코드에서 내전을 진행한 뒤 전적을 동기화시켜주세요.")

elif menu == "플레이어 랭킹":
    st.title("🏆 내전 리그 플레이어 랭킹")
    
    # Sort options
    sort_by = st.selectbox("정렬 기준", ["MMR 순", "승률 순", "평균 AI점수 순", "판수 순"])
    
    if sort_by == "MMR 순":
        ranked_df = df_players.sort_values(by="mmr", ascending=False)
    elif sort_by == "승률 순":
        ranked_df = df_players.sort_values(by=["qualified_stats", "win_rate", "matches_played"], ascending=[False, False, False])
    elif sort_by == "평균 AI점수 순":
        ranked_df = df_players.sort_values(by=["qualified_stats", "avg_ai_score"], ascending=[False, False])
    else:
        ranked_df = df_players.sort_values(by="matches_played", ascending=False)
        
    # Formatting table
    ranked_display = ranked_df.copy()
    ranked_display = ranked_display.reset_index(drop=True)
    ranked_display.index += 1
    ranked_display['sample_status'] = ranked_display['matches_played'].apply(
        lambda games: "공개" if games >= MIN_STATS_MATCHES else f"표본 부족 ({int(games)}/{MIN_STATS_MATCHES})"
    )
    protected_columns = ['wins', 'losses', 'win_rate', 'avg_kda', 'avg_ai_score', 'archetype']
    for column in protected_columns:
        ranked_display.loc[~ranked_display['qualified_stats'], column] = None
    ranked_display.loc[~ranked_display['qualified_stats'], 'archetype'] = "표본 수집 중"
    ranked_display['tier_icon'] = ranked_display['current_tier'].map(tier_emblem)
    ranked_display['main_pos_icon'] = ranked_display['main_pos'].map(position_icon)
    ranked_display['sub_pos_icon'] = ranked_display['sub_pos'].map(position_icon)
    ranked_display = ranked_display[['display_name', 'riot_accounts', 'tier_icon', 'current_tier_label', 'high_tier_label', 'mmr', 'matches_played', 'sample_status', 'wins', 'losses', 'win_rate', 'avg_kda', 'avg_ai_score', 'main_pos_icon', 'main_pos_kr', 'sub_pos_icon', 'sub_pos_kr', 'archetype']]

    ranked_display.columns = ['디스코드 닉네임', '라이엇 계정', '티어', '현재 솔로 티어', '최고 티어', 'MMR', '경기 수', '평가 상태', '승리', '패배', '승률 (%)', '평균 KDA', '평균 AI점수', '주', '주 포지션', '부', '부 포지션', '플레이 스타일']

    # Style mapping
    st.dataframe(
        ranked_display.style.format({
            'MMR': '{:.0f}',
            '경기 수': '{:.0f}',
            '승리': '{:.0f}',
            '패배': '{:.0f}',
            '승률 (%)': '{:.1f}%',
            '평균 KDA': '{:.2f}',
            '평균 AI점수': '{:.2f}'
        }, na_rep='-').highlight_max(subset=['MMR'], color='#123c3d'),
        width="stretch",
        column_config={
            "티어": st.column_config.ImageColumn("티어", width="small"),
            "주": st.column_config.ImageColumn("주", width="small"),
            "부": st.column_config.ImageColumn("부", width="small"),
        },
    )

elif menu == "플레이어 상세 분석":
    st.title("🔍 플레이어 상세 성적 및 성향 분석")
    
    # 디스코드 닉네임 ㄱㄴㄷ순으로 정렬한다. 한글 음절(가-힣)은 유니코드 코드포인트가
    # 이미 ㄱㄴㄷ 순서라 기본 정렬로 가나다순이 되고, casefold로 영문 대소문자도 함께 묶인다.
    player_list = sorted(
        df_players['display_name'].dropna().tolist(),
        key=lambda name: str(name).casefold(),
    )
    if not player_list:
        st.warning("등록된 플레이어가 없습니다.")
        st.stop()
        
    selected_name = st.selectbox("플레이어 선택", player_list)
    p_info = df_players[df_players['display_name'] == selected_name].iloc[0]
    p_id = p_info['id']
    
    # Filter stats
    p_parts = df_participants[df_participants['player_id'] == p_id]
    has_enough_sample = bool(p_info['qualified_stats'])
    
    col1, col2 = st.columns([1, 2])
    
    with col1:
        summary = account_summaries.get(p_id, {})

        # 프로필 아이콘(최근 경기 raw_data) + 닉네임 + 티어 엠블럼 헤더
        prof_uri = profile_icon(get_latest_profile_icon_id(p_parts))
        avatar_html = (
            f"<img class='avatar' src='{prof_uri}'>" if prof_uri
            else "<div class='ph-fallback'>👤</div>"
        )
        tier_uri = tier_emblem(summary.get("current_tier"))
        tier_img_html = f"<img src='{tier_uri}'>" if tier_uri else ""
        st.markdown(
            f"<div class='player-head'>{avatar_html}<div>"
            f"<div class='ph-name'>{selected_name}</div>"
            f"<div class='ph-tier'>{tier_img_html}<span>{summary.get('current_tier_label', '-')}</span></div>"
            f"</div></div>",
            unsafe_allow_html=True,
        )

        st.markdown(f"**라이엇 계정:** {summary.get('riot_accounts', '-')}")
        high_tier_img = inline_img(tier_emblem(summary.get("high_tier_label")), size=22)
        st.markdown(
            f"**최고 티어:** {high_tier_img} {summary.get('high_tier_label', '-')}",
            unsafe_allow_html=True,
        )
        main_pos_img = inline_img(position_icon(p_info["main_pos"]), size=18)
        sub_pos_img = inline_img(position_icon(p_info["sub_pos"]), size=18)
        st.markdown(
            f"**주/부 포지션:** {main_pos_img} {p_info['main_pos_kr']} · {sub_pos_img} {p_info['sub_pos_kr']}",
            unsafe_allow_html=True,
        )
        st.markdown(f"**플레이어 성향:** `{p_info['archetype']}`")

        # 자동 태그 — 지표 기반 top3. 마우스를 올리면 산정 근거가 보인다.
        p_tags = get_player_tags(p_id)
        st.markdown(
            f"<div class='ptag-row'>{render_tag_chips(p_tags)}</div>",
            unsafe_allow_html=True,
        )
        st.caption(" · ".join(f"{t['emoji']} {t['label']}: {t['reason']}" for t in p_tags))

        account_details = summary.get("account_details", [])
        if account_details:
            st.markdown("**연동된 라이엇 계정**")
            account_rows = []
            for account in account_details:
                account_rows.append({
                    "계정": account["riot_id"],
                    "메인": "✅" if account["main_account"] else "",
                    "엠블럼": tier_emblem(account.get("solo_tier")),
                    "현재 티어": account["current_tier_label"],
                    "최고 티어": account["high_tier_label"],
                })
            st.dataframe(
                pd.DataFrame(account_rows),
                width="stretch",
                hide_index=True,
                column_config={"엠블럼": st.column_config.ImageColumn("티어", width="small")},
            )
        
        # Mini cards
        st.metric("현재 MMR", f"{p_info['mmr']}")
        st.metric("수집된 경기", f"{int(p_info['matches_played'])}/{MIN_STATS_MATCHES}판")
        if has_enough_sample:
            st.metric("전적", f"{int(p_info['wins'])}승 {int(p_info['losses'])}패 (승률 {p_info['win_rate']:.1f}%)")
            st.metric("평균 KDA", f"{p_info['avg_kda']:.2f} (평균 {p_info['avg_kills']:.1f}/{p_info['avg_deaths']:.1f}/{p_info['avg_assists']:.1f})")
            st.metric("평균 AI점수", f"{p_info['avg_ai_score']:.2f}")
        else:
            st.info(f"개인 평가 스탯은 최소 {MIN_STATS_MATCHES}판 이상 수집된 뒤 공개됩니다.")

    with col2:
        st.markdown("### 📈 최근 AI점수 / 딜량 변화 추이")
        if has_enough_sample and not p_parts.empty:
            p_parts_sorted = p_parts.sort_values(by="id") # Cronological
            p_parts_sorted['game_index'] = range(1, len(p_parts_sorted) + 1)
            
            # Double line chart
            fig_trends = go.Figure()
            fig_trends.add_trace(go.Scatter(
                x=p_parts_sorted['game_index'],
                y=p_parts_sorted['ai_score'],
                name='AI Score (Left)',
                line=dict(color='#66FCF1', width=3),
                yaxis='y1'
            ))
            fig_trends.add_trace(go.Scatter(
                x=p_parts_sorted['game_index'],
                y=p_parts_sorted['total_damage_dealt'],
                name='딜량 (Right)',
                line=dict(color='#FFC49E', width=2, dash='dot'),
                yaxis='y2'
            ))
            
            fig_trends.update_layout(
                paper_bgcolor='rgba(0,0,0,0)',
                plot_bgcolor='rgba(0,0,0,0)',
                font_color="#FFFFFF",
                yaxis=dict(title=dict(text='AI Score', font=dict(color='#66FCF1')), tickfont=dict(color='#66FCF1')),
                yaxis2=dict(title=dict(text='딜량', font=dict(color='#FFC49E')), tickfont=dict(color='#FFC49E'), overlaying='y', side='right')
            )
            freeze_chart(fig_trends)
            st.plotly_chart(fig_trends, width="stretch", config=PLOTLY_CONFIG)
        else:
            st.info("플레이어의 공개 가능한 게임 데이터가 아직 부족합니다.")

    if not has_enough_sample:
        st.stop()

    # 모스트 챔피언 TOP 3 — 판수 기준(동률이면 승률)으로 뽑아 챔피언 이미지와 함께 보여준다.
    st.subheader("🏆 모스트 챔피언 TOP 3")
    if not p_parts.empty:
        most_champs = p_parts.groupby("champion_name").agg(
            games=("id", "count"),
            wins=("win", "sum"),
            kda=("kda", "mean"),
            ai_score=("ai_score", "mean"),
        ).reset_index()
        most_champs["win_rate"] = most_champs["wins"] / most_champs["games"] * 100
        most_champs = most_champs.sort_values(
            by=["games", "win_rate"], ascending=[False, False]
        ).head(3)

        medals = ["🥇", "🥈", "🥉"]
        cards = []
        for rank, (_, champ_row) in enumerate(most_champs.iterrows()):
            champ_uri = champ_icon_by_name(champ_row["champion_name"])
            img_html = (
                f"<img class='c3-img' src='{champ_uri}'>" if champ_uri
                else "<div class='c3-fallback'>🎮</div>"
            )
            kda_text = f"{champ_row['kda']:.2f}" if pd.notna(champ_row["kda"]) else "-"
            ai_text = f"{champ_row['ai_score']:.1f}" if pd.notna(champ_row["ai_score"]) else "-"
            cards.append(
                f"<div class='champ3-card rank{rank + 1}'>"
                f"<div class='c3-medal'>{medals[rank]}</div>{img_html}"
                f"<div class='c3-name'>{champ_row['champion_name']}</div>"
                f"<div class='c3-main'>{int(champ_row['games'])}판 · 승률 {champ_row['win_rate']:.1f}%</div>"
                f"<div class='c3-sub'>KDA {kda_text} · AI {ai_text}</div>"
                f"</div>"
            )
        st.markdown(f"<div class='champ3-grid'>{''.join(cards)}</div>", unsafe_allow_html=True)
    else:
        st.info("챔피언 기록이 아직 없습니다.")

    st.subheader("📌 개인 평균 지표")
    # 시야점수(vision_score)는 DeepLOL 데이터가 전부 0이라 제외한다. 시야는 아래 '와드' 지표로 확인.
    stat_col1, stat_col2, stat_col3 = st.columns(3)
    avg_minutes = (p_parts['game_duration'].replace(0, pd.NA) / 60).mean()
    avg_minutes = 1 if pd.isna(avg_minutes) or avg_minutes <= 0 else avg_minutes
    stat_col1.metric("평균 딜량", f"{p_info['avg_damage']:.0f}", f"DPM {p_info['avg_damage'] / avg_minutes:.0f}")
    stat_col2.metric("평균 골드", f"{p_info['avg_gold']:.0f}", f"GPM {p_info['avg_gold'] / avg_minutes:.0f}")
    stat_col3.metric("평균 CS", f"{p_info['avg_cs']:.1f}", f"CS/min {p_info['avg_cs'] / avg_minutes:.1f}")

    # DeepLOL 데이터에 없는 지표(감소시킨 피해·CC 시간·오브젝트/포탑 피해·회복/보호막)는
    # 항상 0으로만 나와 표시하지 않는다.
    detail_metrics = pd.DataFrame([
        {"구분": "전투", "지표": "받은 피해", "평균": p_info['avg_damage_taken']},
        {"구분": "시야", "지표": "와드 설치", "평균": p_info['avg_wards_placed']},
        {"구분": "시야", "지표": "와드 제거", "평균": p_info['avg_wards_killed']},
        {"구분": "시야", "지표": "제어 와드 구매", "평균": p_info['avg_control_wards']},
    ])
    detail_metrics['평균'] = detail_metrics['평균'].round(1)
    st.dataframe(detail_metrics, width="stretch", hide_index=True)

    st.subheader("💥 멀티킬 기록")
    multi_cols = st.columns(3)
    multi_cols[0].metric("트리플킬", f"{int(p_info['triple_kills'])}회")
    multi_cols[1].metric("쿼드라킬", f"{int(p_info['quadra_kills'])}회")
    multi_cols[2].metric("펜타킬", f"{int(p_info['penta_kills'])}회")

    # 해당 플레이어 경기의 time_analysis(분당 승률 타임라인)를 모아 모멘텀 분석에 주입.
    player_match_ids = set(df_participants[df_participants["player_id"] == p_id]["match_id"])
    match_timelines = {}
    for _, mrow in df_matches[df_matches["match_id"].isin(player_match_ids)].iterrows():
        try:
            ta = json.loads(mrow["raw_data"]).get("time_analysis") if mrow["raw_data"] else None
        except (json.JSONDecodeError, TypeError):
            ta = None
        if ta:
            match_timelines[mrow["match_id"]] = ta

    analysis_report = analyze_player(
        p_id, df_participants, min_games=MIN_STATS_MATCHES, match_timelines=match_timelines
    )
    if "error" not in analysis_report:
        render_player_analysis_report(analysis_report)
    else:
        st.info(analysis_report["error"])

elif menu == "팀 밸런스 시뮬레이터":
    team_builder.render_team_builder({
        "df_players": df_players,
        "df_participants": df_participants,
        "champ_icon_by_name": champ_icon_by_name,
        "position_icon": position_icon,
        "tier_emblem": tier_emblem,
        "freeze_chart": freeze_chart,
        "plotly_config": PLOTLY_CONFIG,
        "position_service": position_service,
        "position_colors": POSITION_COLORS,
        "get_player_tags": get_player_tags,
    })

elif menu == "경기 전적 기록":
    st.title("📁 전체 매치 히스토리 로그")
    st.caption("내전 커스텀 게임(queue_id=3130)만 저장됩니다. 등록 플레이어 연동은 `/동기화` 또는 계정 등록 시 자동으로 갱신됩니다.")
    
    if df_matches.empty:
        st.info("로그에 저장된 경기가 없습니다.")
    else:
        inhouse_count = int((df_matches["queue_id"] == 3130).sum())
        st.metric("저장된 내전 경기", f"{len(df_matches)}판", f"queue_id=3130: {inhouse_count}판")

        games_per_page = 10
        total_pages = math.ceil(len(df_matches) / games_per_page)
        page = st.number_input("페이지", min_value=1, max_value=total_pages, value=1)
        
        start_idx = (page - 1) * games_per_page
        end_idx = start_idx + games_per_page
        
        page_matches = df_matches.iloc[start_idx:end_idx]

        for _, match in page_matches.iterrows():
            render_match_block(match, df_participants)
