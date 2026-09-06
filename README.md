# GOL League Inhouse Manager

디스코드 리그 오브 레전드 **내전(사설 게임) 운영 자동화 플랫폼**입니다.
팀 밸런싱 → 커스텀 게임 코드 발급 → 전적 자동 수집 → 지표 분석 → 리포트/공유 카드 발행까지,
내전 한 판의 전 과정을 봇 명령어와 웹 대시보드 하나로 처리합니다.

```
Discord 봇  ──HTTP──▶  FastAPI 백엔드  ──▶  SQLite (players / matches / participants)
 (슬래시 명령)              │  │
                            │  └─▶ 백그라운드 Worker ──▶ DeepLOL API (전적 자동 수집)
                            │  └─▶ Ollama (로컬 LLM, 한국어 해설·리포트)
                            └─▶ Pillow 카드 렌더러 (PNG 공유 이미지)

Streamlit 대시보드 ──HTTP──▶ 같은 FastAPI 백엔드 / DB
```

---

## 한눈에 보는 기능

| 영역 | 내용 |
| --- | --- |
| **플레이어 관리** | 디스코드 멤버를 키로 플레이어 등록. 한 사람이 부캐 포함 **여러 Riot 계정**을 가질 수 있고, 모든 전적은 디스코드 플레이어 단위로 합산됩니다. `홍길동(컴퓨터)` 같은 접미사 닉네임은 자동 정규화되어 별칭으로 묶입니다. |
| **전적 자동 수집** | 백그라운드 Worker가 DeepLOL API를 폴링해 커스텀 게임을 찾아 저장. 변화 감지 + 휴면 계정 스킵으로 전수 폴링을 피합니다. KDA·딜량·탱킹·시야·골드·오브젝트·타임라인(raw_data)까지 적재. |
| **Elo MMR** | 티어 기반 초기 MMR(`IRON 620` ~ `CHALLENGER 2100`)에서 시작해, 내전 결과로 팀 레이팅 대비 Elo를 갱신. 경기 수에 따라 K값이 감쇠합니다. 전체 재계산도 지원. |
| **팀 밸런싱** | 5v5 조합 전수 탐색. `mmr` / `tier` / `lane` 모드, 포지션 중복 패널티, 최근 10경기 폼 반영, 특정 듀오 분리 옵션. |
| **플레이 분석** | 포지션 평균 대비 지표 편차로 강점/약점 TOP 3, 승리 패턴 vs 패배 패턴, 챔피언별 성적, 시너지·라이벌 관계, 성향 태그(`킬 캐처`, `시야 장인`, `한타 본능`, `마이웨이` 등)를 산출. |
| **AI 해설** | 로컬 **Ollama(`qwen3:8b`)** 로 매칭 결과 전력 분석·승부 예측 코멘터리를 한국어로 생성. 외부 LLM API 비용 없음. |
| **공유 카드(PNG)** | Pillow로 직접 렌더링: MVP 카드, 파워랭킹 TOP 10, 명예의 전당, 듀오 시너지, 경기 흐름(승률 그래프), 스타일 카드, 팀 로스터 카드. |
| **정기 리포트** | 경기 종료 리포트·신기록 알림을 공지 채널에 자동 게시. 주간 결산은 매주 월요일 저녁 자동 발행. |
| **웹 대시보드** | Streamlit 5개 화면 — 대시보드 홈 / 플레이어 랭킹 / 플레이어 상세 분석 / 팀 밸런스 시뮬레이터 / 경기 전적 기록. |

---

## 기술 스택

- **Backend**: FastAPI, SQLAlchemy, APScheduler, httpx, Pillow, SQLite
- **Bot**: discord.py (슬래시 명령 + 셀렉트 메뉴 View)
- **Dashboard**: Streamlit + pandas
- **AI**: Ollama 로컬 추론 (`qwen3:8b`)
- **데이터 소스**: DeepLOL API (전적·AI-Score), Riot Data Dragon (챔피언 메타·이미지 캐시)
- **배포**: Cloudflare Tunnel (대시보드만 공개, API는 `127.0.0.1` 로컬 전용)

## 디렉터리 구조

```
backend/app/
  main.py            FastAPI 엔드포인트 (28종)
  models/            Player · PlayerDiscordAlias · RiotAccount · Match · MatchParticipant
  services/          matchmaking · mmr · player_analysis · player_tags · records
                     match_report · weekly_report · share_cards · style_card
                     deeplol_api · champion_data · position_service · ai_service
  workers/match_sync.py   전적 폴링 · Elo 계산 · 참가자 재연결
  scripts/           과거 매치 백필
bot/
  main.py            슬래시 명령 16종
  views/             참가자 선택 UI
  services/          클래시(커스텀 게임) 코드 · 이미지
dashboard/
  app.py             Streamlit 5개 화면
  team_builder.py    밸런스 시뮬레이터
run.sh               백엔드 + 대시보드 + 봇 + 터널 통합 실행/정리
```

---

## 설치 및 실행

### 1. 사전 요구사항
- Python 3.10+ (권장 3.13)
- Ollama + `qwen3:8b`
  ```bash
  ollama pull qwen3:8b
  ```

### 2. 환경변수
```bash
cp .env.example .env
```
`.env`에서 최소한 아래 값을 채웁니다. (`API_KEY`는 `run.sh`가 없으면 자동 생성)
```env
DISCORD_BOT_TOKEN=your_discord_bot_token_here
ADMIN_DISCORD_IDS=          # 서버 관리자 권한 없이 관리자 명령을 쓸 유저 ID (콤마 구분)
CLASH_PASSWORD=             # 커스텀 게임 코드 생성용
PUBLIC_MODE=quick           # quick | named | port | none
```

### 3. 실행
```bash
./run.sh
```
`run.sh`가 의존성 설치 확인 → FastAPI → Streamlit → Cloudflare Tunnel → 디스코드 봇 순으로 띄우고,
`Ctrl+C` 한 번으로 전부 정리합니다. macOS 유휴 절전으로 봇 게이트웨이가 끊기는 것을 막기 위해
`caffeinate`도 함께 잡아둡니다(`KEEP_AWAKE=0`으로 해제).

- FastAPI: `http://127.0.0.1:8000/docs` — **로컬 전용**, 봇/대시보드만 사용
- 대시보드: `http://127.0.0.1:8501` — 공개는 Cloudflare Tunnel이 담당
- 외부에서의 쓰기 요청(POST/PUT/DELETE)은 `X-API-Key` 헤더 필요

패키지를 수동 설치하려면:
```bash
pip install -r backend/requirements.txt -r bot/requirements.txt -r dashboard/requirements.txt
```

---

## 디스코드 명령어

### 일반
| 명령 | 설명 |
| --- | --- |
| `/팀생성` | 참가자 10명을 선택하면 밸런스 조합 + AI 해설 코멘터리 출력 |
| `/코드 [매치이름]` | 커스텀 게임 참가 코드 생성 |
| `/분석 [디스코드명]` | 성향·지표·챔피언·시너지 종합 분석 리포트 |
| `/최근경기` | 가장 최근 내전의 경기 리포트 |
| `/주간리포트` | 지난 7일 결산 (매주 월요일 저녁 자동 발행) |
| `/명예의전당` | 통산 기록 보유자 · 누적 MVP · 다승 랭킹 |
| `/파워랭킹` | Elo MMR 기준 TOP 10 카드 |
| `/스타일카드 [디스코드명]` | 개인 플레이 스타일 공유 카드 |
| `/듀오카드 [듀오상대]` | 두 플레이어가 같은 팀일 때의 성적 카드 |
| `/경기흐름` | 최근 경기 승률 흐름 그래프 카드 |
| `/동기화` | 전적 즉시 재수집 트리거 |
| `/청소 [검색범위]` | 봇이 이 채널에 남긴 메시지 일괄 삭제 |

### 관리자 (`/관리자 …`)
| 명령 | 설명 |
| --- | --- |
| `/관리자 등록 [디스코드명] [관리_닉네임] [롤계정명]` | 플레이어 등록 + 대표 계정 연동 (DeepLOL로 유효성 검사 및 `puu_id` 조회) |
| `/관리자 계정추가 [디스코드명] [롤계정명]` | 같은 플레이어에 부캐 추가 |
| `/관리자 등록취소 [디스코드명 \| 관리_닉네임]` | 플레이어와 연결된 계정 전부 삭제 |
| `/관리자 공지채널` | 경기·주간 리포트가 올라올 채널을 현재 채널로 설정 |

예시: `/관리자 등록 디스코드명:@홍길동 관리_닉네임:홍길동 롤계정명:소환사이름#KR1`

---

## 주요 API

| 분류 | 엔드포인트 |
| --- | --- |
| 플레이어 | `GET/POST /api/players`, `GET /api/players/{id}`, `GET /api/players/discord/{discord_id}`, `PUT/DELETE /api/players/{id}`, `POST /api/players/{id}/accounts`, `DELETE /api/accounts/{id}` |
| 분석 | `GET /api/players/{id}/analysis`, `GET /api/reports/weekly`, `GET /api/matches/reports`, `GET /api/hall-of-fame` |
| 매칭 | `POST /api/matchmake`, `POST /api/matchmake/commentary`, `POST /api/matchmake/roster-card` |
| 카드 | `/api/players/{id}/style-card`, `/api/matches/{id}/mvp-card`, `/api/matches/latest/momentum-card`, `/api/power-ranking/card`, `/api/hall-of-fame/card`, `/api/duo-card` |
| 동기화 | `POST /api/sync`, `GET /api/sync/status`, `POST /api/relink` |

전체 스펙은 실행 후 `http://127.0.0.1:8000/docs`에서 확인할 수 있습니다.
