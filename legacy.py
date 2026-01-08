"""
레거시 챗봇 - 조회 기능 (통합 버전)
"""

import pandas as pd
import re
import json
import requests


# =============================================================================
# 0) 파싱 / 유틸
# =============================================================================

LEGACY_DEFAULT_YEAR = "2025"


def normalize_line_name(line_val):
    s = str(line_val).strip()
    if s == "1":
        return "조립1"
    if s == "2":
        return "조립2"
    if s == "3":
        return "조립3"
    if "조립" in s:
        return s
    return s


def normalize_date(date_val):
    if not date_val:
        return ""
    s = str(date_val).strip()
    if len(s) >= 10:
        return s[:10]
    return s


def extract_version(text: str) -> str:
    t = (text or "")
    if ("0차" in t) or ("초기" in t) or ("계획" in t):
        return "0차"
    return "최종"


def extract_date_info(text: str, default_year: str = LEGACY_DEFAULT_YEAR):
    """
    지원:
    - '9월 5일'
    - '9/5'
    - '2025-09-05'
    - '10월' (month만)
    """
    info = {"date": None, "month": None, "year": default_year}
    t = (text or "").strip()

    # YYYY-MM-DD
    m = re.search(r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b", t)
    if m:
        y, mm, dd = m.groups()
        info["year"] = y
        info["month"] = int(mm)
        info["date"] = f"{int(y):04d}-{int(mm):02d}-{int(dd):02d}"
        return info

    # M월 D일
    m = re.search(r"(\d{1,2})\s*월\s*(\d{1,2})\s*일", t)
    if m:
        mm, dd = m.groups()
        info["month"] = int(mm)
        info["date"] = f"{int(info['year']):04d}-{int(mm):02d}-{int(dd):02d}"
        return info

    # M/D
    m = re.search(r"\b(\d{1,2})\s*/\s*(\d{1,2})\b", t)
    if m:
        mm, dd = m.groups()
        info["month"] = int(mm)
        info["date"] = f"{int(info['year']):04d}-{int(mm):02d}-{int(dd):02d}"
        return info

    # 월만
    m = re.search(r"(\d{1,2})\s*월", t)
    if m:
        info["month"] = int(m.group(1))

    return info


def extract_product_keyword(text: str):
    """제품 키워드 추출"""
    ignore_words = {
        "생산량", "알려줘", "비교해줘", "비교", "제품", "최종", "0차", "월", "일", 
        "capa", "카파", "초과", "어떻게", "돼", "있어", "사례", "총", "와", "과"
    }
    words = (text or "").split()
    for w in words:
        clean_w = re.sub(r"[^a-zA-Z0-9가-힣]", "", w)
        if not clean_w:
            continue
        if clean_w.lower() in ignore_words:
            continue
        if re.match(r"\d+(월|일)", clean_w):
            continue
        return clean_w
    return None


def extract_category(text: str):
    """구분 키워드 추출 (fan, motor, flange)"""
    text_lower = (text or "").lower()
    if "fan" in text_lower or "팬" in text:
        return "fan"
    if "motor" in text_lower or "모터" in text:
        return "motor"
    if "flange" in text_lower or "플랜지" in text:
        return "flange"
    return None


# =============================================================================
# 1) ⭐ final_issue: 증산/간섭 유사사례 (수정됨)
# =============================================================================

def detect_increase_case_intent(text: str) -> bool:
    """final_issue 기반 증산/간섭 유사사례 질문 의도 감지"""
    keywords = [
        "우선순위", "선순위", "후순위", "변경", "늘려", "늘려야", "증산", "증량", 
        "더", "추가", "확대", "긴급", "급하게", "땡겨", "당겨",
        "독점", "간섭", "유사", "사례", "예전", "과거", "비슷한"
    ]
    t = (text or "").strip()
    return any(k in t for k in keywords)


def _final_issue_query(user_input: str, supabase, target_date: str | None):
    """
    조건:
    1. final_issue에서 같은 날 다음 조건 모두 만족:
       - final_remark='긴급 물량 증량' & field_role='선순위'
       - final_remark='품목간 간섭' & field_role='후순위'
    2. production_data에서:
       - 납기일 = final_issue.date → 변경 전 생산량 (0차)
       - 생산일 = final_issue.date → 변경 후 생산량 (최종)
    """
    try:
        # Step 1: final_issue에서 조건에 맞는 데이터 조회
        res = supabase.table("final_issue").select("date, item_name, line, final_remark, field_role").execute()
        
        if not res.data:
            return None
        
        df = pd.DataFrame(res.data)
        
        # 같은 날짜별로 그룹화하여 조건 확인
        valid_dates = []
        for date, group in df.groupby('date'):
            has_increase = any(
                (row['final_remark'] == '긴급 물량 증량' or '긴급 물량 증량' in str(row['final_remark'])) and 
                row['field_role'] == '선순위' 
                for _, row in group.iterrows()
            )
            has_interference = any(
                (row['final_remark'] == '품목간 간섭' or '품목간 간섭' in str(row['final_remark']) or '품목 간 간섭' in str(row['final_remark'])) and 
                row['field_role'] == '후순위'
                for _, row in group.iterrows()
            )
            
            if has_increase and has_interference:
                valid_dates.append(date)
        
        if not valid_dates:
            return "우선순위 변경 사례는 있으나, 긴급 증량(선순위)과 품목간 간섭(후순위)이 같은 날 발생한 케이스가 없습니다."
        
        # target_date가 있으면 필터링
        if target_date:
            valid_dates = [d for d in valid_dates if d == target_date]
            if not valid_dates:
                return f"{target_date}에는 우선순위 변경 사례가 없습니다."
        
        # Step 2: 유효한 날짜의 final_issue 데이터 필터링
        df_valid = df[df['date'].isin(valid_dates)].copy()
        
        # Step 3: production_data에서 변경 전/후 생산량 조회
        result_list = []
        
        for _, row in df_valid.iterrows():
            date = row['date']
            item_name = row['item_name']
            line = row['line']
            remark = row['final_remark']
            field_role = row['field_role']  # 우선순위 추가
            
            # 변경 전: 0차 버전의 납기일 기준
            res_before = supabase.table("production_data").select("생산량, 라인").eq("납기일", date).eq("버전", "0차").ilike("품명", f"%{item_name}%").execute()
            
            # 변경 후: 최종 버전의 생산일 기준
            res_after = supabase.table("production_data").select("생산량, 라인").eq("생산일", date).eq("버전", "최종").ilike("품명", f"%{item_name}%").execute()
            
            qty_before = 0
            qty_after = 0
            prod_line = line
            
            if res_before.data:
                qty_before = int(res_before.data[0].get('생산량', 0))
                prod_line = res_before.data[0].get('라인', line)
            
            if res_after.data:
                qty_after = int(res_after.data[0].get('생산량', 0))
                if not res_before.data:
                    prod_line = res_after.data[0].get('라인', line)
            
            result_list.append({
                '날짜': date,
                '품명': item_name,
                '라인': prod_line,
                '변경_전': qty_before,
                '변경_후': qty_after,
                '사유': remark,
                '우선순위': field_role  # 우선순위 추가
            })
        
        if not result_list:
            return "final_issue에서 유효한 데이터를 찾았으나 production_data와 매칭되는 데이터가 없습니다."
        
        # Step 4: 결과를 Context 형식으로 반환
        context_log = "[PRIORITY_CHANGE_CASE]\n"
        context_log += "Title: 우선순위 변경으로 인한 생산량 변경 사례\n"
        context_log += "Data:\n"
        for item in result_list:
            context_log += f"{item['날짜']}|{item['품명']}|{item['라인']}|{item['변경_전']}|{item['변경_후']}|{item['사유']}|{item['우선순위']}\n"
        
        return context_log
        
    except Exception as e:
        return f"증산/간섭 사례 조회 중 오류: {str(e)}"



# =============================================================================
# 2) Legacy DB 조회
# =============================================================================

def fetch_db_data_legacy(user_input: str, supabase):
    info = extract_date_info(user_input, LEGACY_DEFAULT_YEAR)
    target_date = info["date"]
    target_month = info["month"]
    target_version = extract_version(user_input)
    product_key = extract_product_keyword(user_input)
    category = extract_category(user_input)
    context_log = ""

    try:
        # =====================================================================
        # 0) ⭐ final_issue 증산/간섭 유사사례 (최우선)
        # =====================================================================
        if detect_increase_case_intent(user_input):
            fi = _final_issue_query(user_input, supabase, target_date)
            if fi is not None:
                return fi

        # =====================================================================
        # 1) 제품 생산량 0차 최종 비교
        # =====================================================================
        if target_date and product_key and ("0차" in user_input and "최종" in user_input) and "비교" in user_input:
            res_v0 = supabase.table("production_data").select("납기일, 품명, 생산량").eq("납기일", target_date).eq("버전", "0차").ilike("품명", f"%{product_key}%").execute()
            res_final = supabase.table("production_data").select("생산일, 품명, 생산량").eq("생산일", target_date).eq("버전", "최종").ilike("품명", f"%{product_key}%").execute()

            v0_qty = 0
            product_name = product_key
            if res_v0.data:
                df_v0 = pd.DataFrame(res_v0.data)
                v0_qty = int(df_v0.iloc[0]['생산량']) if '생산량' in df_v0.columns else 0
                product_name = df_v0.iloc[0]['품명']

            final_qty = 0
            if res_final.data:
                df_final = pd.DataFrame(res_final.data)
                final_qty = int(df_final.iloc[0]['생산량']) if '생산량' in df_final.columns else 0
                if not res_v0.data and '품명' in df_final.columns:
                    product_name = df_final.iloc[0]['품명']

            if not res_v0.data and not res_final.data:
                return f"{target_date}에 {product_key} 제품의 0차 또는 최종 데이터가 없습니다."

            context_log += f"\n[PRODUCT_COMPARISON]\n"
            context_log += f"Date: {target_date}\n"
            context_log += f"Product: {product_name}\n"
            context_log += f"V0: {v0_qty}\n"
            context_log += f"Final: {final_qty}\n"
            return context_log

        # =====================================================================
        # 2) 과거 이슈 사례 (production_issue_analysis_8_11)
        # =====================================================================
        if "사례" in user_input and not detect_increase_case_intent(user_input):
            issue_mapping = {
                "MDL1": {"keywords": ["먼저", "줄여", "순위", "교체"], "db_text": "생산순위 조정",
                         "title": "MDL1: 미달(생산순위 조정/모델 교체)"},
                "MDL2": {"keywords": ["감사", "정지", "설비", "라인전체"], "db_text": "라인전체이슈",
                         "title": "MDL2: 미달(라인전체이슈/설비)"},
                "MDL3": {"keywords": ["부품", "자재", "결품", "수급", "안되는"], "db_text": "자재결품",
                         "title": "MDL3: 미달(부품수급/자재결품)"},
                "PRP": {"keywords": ["선행", "미리", "당겨", "땡겨"], "db_text": "선행 생산",
                        "title": "PRP: 선행 생산(숙제 미리하기)"},
                "SMP": {"keywords": ["샘플", "긴급"], "db_text": "계획외 긴급 생산",
                        "title": "SMP: 계획외 긴급 생산"},
                "CCL": {"keywords": ["취소"], "db_text": "계획 취소",
                        "title": "CCL: 계획 취소/라인 가동중단"},
            }

            detected_code = None
            for code, meta in issue_mapping.items():
                if any(k in user_input for k in meta["keywords"]):
                    detected_code = code
                    break

            if detected_code:
                meta = issue_mapping[detected_code]
                query = supabase.table("production_issue_analysis_8_11") \
                    .select("품목명, 날짜, 계획_v0, 실적_v2, 누적차이_Gap, 최종_이슈분류")

                if detected_code == "MDL2":
                    query = query.or_("최종_이슈분류.ilike.%라인전체이슈%,최종_이슈분류.ilike.%설비%")
                elif detected_code == "MDL3":
                    query = query.or_("최종_이슈분류.ilike.%부품수급%,최종_이슈분류.ilike.%자재결품%")
                else:
                    query = query.ilike("최종_이슈분류", f"%{meta['db_text']}%")

                response = query.limit(3).execute()
                if response.data:
                    context_log += f"[{detected_code} CASE FOUND]\n"
                    context_log += f"Title: {meta['title']}\n"
                    context_log += f"Data: {json.dumps(response.data, ensure_ascii=False)}"
                    return context_log
                return "관련된 과거 유사 사례를 찾을 수 없습니다."

        # =====================================================================
        # 3) 구분별 월 생산량 조회
        # =====================================================================
        if target_month and category and ("생산량" in user_input or "알려" in user_input):
            res = supabase.table("production_data").select("월, 구분, 생산량").eq("월", target_month).eq("버전", target_version).ilike("구분", f"%{category}%").execute()

            if res.data:
                df = pd.DataFrame(res.data)
                total_qty = df['생산량'].sum()

                context_log += f"\n[CATEGORY_PRODUCTION]\n"
                context_log += f"Month: {target_month}\n"
                context_log += f"Category: {category}\n"
                context_log += f"Total: {int(total_qty)}\n"
                return context_log
            else:
                return f"{target_month}월 {category} 구분 데이터가 production_data 테이블에 없습니다."

        # =====================================================================
        # 4) 월간 생산량 브리핑 (2개월 이상 비교)
        # =====================================================================
        found_months = re.findall(r"(\d{1,2})월", user_input)
        found_months = sorted(list(set([int(m) for m in found_months])))

        if len(found_months) >= 2 and product_key is None and category is None:
            res = supabase.table("monthly_production").select("월, 총_생산량").in_("월", found_months).eq("버전", target_version).execute()

            if res.data:
                df = pd.DataFrame(res.data)
                df = df.sort_values(by='월')

                month_data = {}
                for _, row in df.iterrows():
                    month_data[int(row['월'])] = int(row['총_생산량'])

                context_log += f"\n[MONTHLY_COMPARISON]\n"
                context_log += f"Version: {target_version}\n"
                context_log += f"Months: {','.join(map(str, found_months))}\n"
                for month in found_months:
                    qty = month_data.get(month, 0)
                    context_log += f"{month}월: {qty}\n"

                return context_log
            else:
                return "요청하신 월의 데이터가 monthly_production 테이블에 없습니다."

        # =====================================================================
        # 5) 단일 월 생산량 조회
        # =====================================================================
        if target_month and ("생산량" in user_input or "알려" in user_input) and not target_date and not ("capa" in user_input.lower() or "카파" in user_input or "초과" in user_input) and not category:
            res = supabase.table("daily_total_production").select("월, 라인, 총_생산량").eq("월", target_month).eq("버전", target_version).execute()

            if res.data:
                df = pd.DataFrame(res.data)
                df['라인'] = df['라인'].apply(normalize_line_name)

                line_summary = df.groupby('라인')['총_생산량'].sum().to_dict()

                line_data = {
                    '조립1': int(line_summary.get('조립1', 0)),
                    '조립2': int(line_summary.get('조립2', 0)),
                    '조립3': int(line_summary.get('조립3', 0))
                }

                total_sum = sum(line_data.values())

                context_log += f"\n[MONTHLY_PRODUCTION]\n"
                context_log += f"Month: {target_month}\n"
                context_log += f"Version: {target_version}\n"
                context_log += f"조립1: {line_data['조립1']}\n"
                context_log += f"조립2: {line_data['조립2']}\n"
                context_log += f"조립3: {line_data['조립3']}\n"
                context_log += f"Total: {total_sum}\n"

                return context_log
            else:
                return f"{target_month}월 {target_version} 데이터가 daily_total_production 테이블에 없습니다."

        # =====================================================================
        # 6) CAPA 조회
        # =====================================================================
        if target_month and ("capa" in user_input.lower() or "카파" in user_input) and "초과" not in user_input:
            res = supabase.table("daily_capa").select("*").eq("월", target_month).execute()

            if res.data:
                df = pd.DataFrame(res.data)
                df['라인'] = df['라인'].apply(normalize_line_name)

                line_data = {
                    '조립1': 0,
                    '조립2': 0,
                    '조립3': 0
                }

                for line in ['조립1', '조립2', '조립3']:
                    line_capa = df[df['라인'] == line]
                    if not line_capa.empty:
                        capa_val = line_capa.iloc[0].get('capa') or line_capa.iloc[0].get('CAPA', 0)
                        line_data[line] = int(capa_val)

                context_log += f"\n[CAPA_INFO]\n"
                context_log += f"Month: {target_month}\n"
                context_log += f"조립1: {line_data['조립1']}\n"
                context_log += f"조립2: {line_data['조립2']}\n"
                context_log += f"조립3: {line_data['조립3']}\n"

                return context_log
            else:
                return f"{target_month}월 CAPA 데이터가 없습니다."

        # =====================================================================
        # 7) CAPA 초과 조회
        # =====================================================================
        if "초과" in user_input and target_month:
            res = supabase.table("daily_total_production").select("날짜, 라인, 총_생산량, 월").eq("월", target_month).eq("버전", target_version).execute()

            if not res.data:
                return f"{target_month}월 {target_version} 데이터가 없습니다."

            df = pd.DataFrame(res.data)
            df['라인'] = df['라인'].apply(normalize_line_name)
            df['날짜'] = df['날짜'].apply(normalize_date)

            capa_res = supabase.table("daily_capa").select("*").eq("월", target_month).execute()

            if not capa_res.data:
                return f"{target_month}월 CAPA 데이터가 없습니다."

            capa_df = pd.DataFrame(capa_res.data)
            capa_df['라인'] = capa_df['라인'].apply(normalize_line_name)

            capa_map = {}
            for _, row in capa_df.iterrows():
                line = row['라인']
                capa_value = row.get('capa') or row.get('CAPA', 0)
                capa_map[line] = int(capa_value)

            over_list = []
            for _, row in df.iterrows():
                line = row['라인']
                total_qty = int(row['총_생산량'])
                date = row['날짜']

                if line in capa_map:
                    capa = capa_map[line]
                    if total_qty > capa:
                        over_list.append({
                            '날짜': date,
                            '라인': line,
                            'CAPA': capa,
                            '총_생산량': total_qty
                        })

            if over_list:
                context_log += f"\n[CAPA_OVER]\n"
                context_log += f"Month: {target_month}\n"
                context_log += f"Count: {len(over_list)}\n"
                context_log += "Data:\n"
                for item in over_list:
                    context_log += f"{item['날짜']}|{item['라인']}|{item['CAPA']}|{item['총_생산량']}\n"
                return context_log
            else:
                return f"{target_month}월 {target_version} 버전에서 CAPA를 초과한 날이 없습니다."

        # =====================================================================
        # 8) 일별 생산량 조회
        # =====================================================================
        if target_date:
            res = supabase.table("daily_total_production").select("날짜, 라인, 총_생산량").eq("날짜", target_date).eq("버전", target_version).execute()

            if res.data:
                df = pd.DataFrame(res.data)
                df['라인'] = df['라인'].apply(normalize_line_name)

                date_obj = pd.to_datetime(target_date)
                month = date_obj.month
                day = date_obj.day

                line_data = {
                    '조립1': 0,
                    '조립2': 0,
                    '조립3': 0
                }

                for _, row in df.iterrows():
                    line = row['라인']
                    qty = int(row['총_생산량'])
                    if line in line_data:
                        line_data[line] = qty

                total_sum = sum(line_data.values())

                context_log += f"\n[DAILY_PRODUCTION]\n"
                context_log += f"Month: {month}\n"
                context_log += f"Day: {day}\n"
                context_log += f"Version: {target_version}\n"
                context_log += f"조립1: {line_data['조립1']}\n"
                context_log += f"조립2: {line_data['조립2']}\n"
                context_log += f"조립3: {line_data['조립3']}\n"
                context_log += f"Total: {total_sum}\n"

                return context_log
            else:
                return f"{target_date} {target_version} 데이터가 없습니다."

        return "질문을 이해하지 못했습니다. 예시를 참고해주세요."

    except Exception as e:
        return f"오류 발생: {str(e)}"


# =============================================================================
# 3) Gemini 응답 생성
# =============================================================================

def query_gemini_ai_legacy(user_input: str, context: str, gemini_key: str) -> str:
    system_prompt = f"""
당신은 숙련된 생산계획 담당자입니다. 제공된 데이터(Context)를 기반으로 사용자의 질문에 답하세요.

[중요: 우선순위 변경 사례 답변 규칙]
Context에 '[PRIORITY_CHANGE_CASE]'가 포함되어 있다면, 반드시 아래 형식으로 출력하세요:

# 우선순위 변경으로 인한 생산량 변경 사례

| 날짜 | 품명 | 라인 | 변경 전 | 변경 후 | 사유 | 우선순위 |
|------|------|------|---------|---------|------|----------|
(Data에서 | 구분자로 파싱하여 표 생성)

[중요: 제품 버전 비교 답변 규칙]
Context에 '[PRODUCT_COMPARISON]'이 포함되어 있다면, 반드시 아래 형식으로 출력하세요:

# {{Product}} 버전 비교

| 날짜 | 품명 | 0차 | 최종 |
|------|------|------|------|
| {{Date}} | {{Product}} | {{V0}} | {{Final}} |

[중요: 구분별 생산량 답변 규칙]
Context에 '[CATEGORY_PRODUCTION]'이 포함되어 있다면, 반드시 아래 형식으로 출력하세요:

# {{Month}}월 {{Category}} 생산량

| 구분 | 생산량 |
|------|--------|
| {{Category}} | {{Total}} |

[중요: 월간 생산량 비교 답변 규칙]
Context에 '[MONTHLY_COMPARISON]'이 포함되어 있다면, 반드시 아래 형식으로 출력하세요:

# {{Month1}}월 vs {{Month2}}월 (vs {{Month3}}월...)

| 월 | 생산량 |
|----|--------|
| {{Month1}}월 | {{총_생산량1}} |
| {{Month2}}월 | {{총_생산량2}} |
...

**{{더 많은 월}}월이 {{더 적은 월}}월보다 {{차이}}개 더 생산했습니다.**

[중요: CAPA 정보 답변 규칙]
Context에 '[CAPA_INFO]'가 포함되어 있다면, 반드시 아래 형식으로 출력하세요:

# {{Month}}월 CAPA

| 라인 | CAPA |
|------|------|
| 조립1 | {{조립1}} |
| 조립2 | {{조립2}} |
| 조립3 | {{조립3}} |

[중요: 월간 생산량 답변 규칙]
Context에 '[MONTHLY_PRODUCTION]'이 포함되어 있다면, 반드시 아래 형식으로 출력하세요:

# {{Month}}월 {{Version}} 생산량

| 라인 | 생산량 |
|------|--------|
| 조립1 | {{조립1}} |
| 조립2 | {{조립2}} |
| 조립3 | {{조립3}} |

**합계: {{Total}}개**

[중요: 일별 생산량 답변 규칙]
Context에 '[DAILY_PRODUCTION]'이 포함되어 있다면, 반드시 아래 형식으로 출력하세요:

# {{Month}}월 {{Day}}일 {{Version}} 생산량

| 라인 | 생산량 |
|------|--------|
| 조립1 | {{조립1}} |
| 조립2 | {{조립2}} |
| 조립3 | {{조립3}} |

**합계: {{Total}}개**

[중요: CAPA 초과 답변 규칙]
Context에 '[CAPA_OVER]'가 포함되어 있다면, 반드시 아래 형식으로 출력하세요:

# {{Month}}월 CAPA 초과 총 {{Count}}건

| 날짜 | 라인 | CAPA | 총 생산량 |
|------|------|------|-----------|
(Data에서 | 구분자로 파싱하여 표 생성)

[중요: 이슈 코드 답변 규칙]
Context에 [CODE CASE FOUND]가 있다면:
1. 답변 최상단에 코드명과 제목을 # Heading 1로 적으세요.
2. 데이터를 바탕으로 표를 작성하세요.

[일반 답변 규칙]
1. 숫자는 제공된 그대로 전달하세요.
2. 데이터가 없으면 없다고 하세요.
3. 간결하고 명확하게 답변하세요.

[Context Data]:
{context}

[User Question]:
{user_input}
"""

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash-exp:generateContent?key={gemini_key}"
    headers = {"Content-Type": "application/json"}
    data = {"contents": [{"parts": [{"text": system_prompt}]}]}

    try:
        response = requests.post(url, headers=headers, json=data, timeout=60)
        if response.status_code == 200:
            result = response.json()
            try:
                return result['candidates'][0]['content']['parts'][0]['text']
            except Exception:
                return "응답 파싱 실패"
        else:
            return f"API 오류: {response.status_code}"
    except Exception as e:
        return f"통신 오류: {e}"

