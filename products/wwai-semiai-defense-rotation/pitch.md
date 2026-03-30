# WWAI 반도체↔방산 레짐전환 Active ETF
**Product Code**: wwai-semiai-defense-rotation
**Status**: Paper Portfolio (Phase 1)
**Demo**: pension.wwai.app
**Date**: 2026-03-30

---

## Elevator Pitch

**한국어 (30초)**
강세장엔 반도체·AI ETF, 약세장엔 방산·원자력으로 자동전환하는 IRP/DC 연금 전략입니다. 채권 30% 슬리브로 FSC 안전자산 의무를 지키면서도 CAGR 25.7%, 최대낙폭 -15.9%, Calmar 1.62를 검증 달성했습니다. KRX 상장 ETF만으로 구성, 직접투자 가능합니다.

**English (30 seconds)**
A regime-switching pension strategy: semiconductor/AI ETFs in bull markets, defense/nuclear in bear markets — automatically. With 30% bond sleeve for FSC compliance: validated CAGR 25.7%, MaxDD -15.9%, Calmar 1.62. Built entirely from KRX-listed ETFs.

---

## Strategy Logic

```
KODEX200 vs MA60
     │
     ├─ BULL ──→ 반도체/AI ETF top-12 (by 21d momentum)
     │            ACE AI반도체TOP3+, TIGER 반도체TOP10, ...
     │
     └─ BEAR ──→ 방산/원자력 ETF top-7 (by 21d momentum)
                  KODEX 방위산업, KODEX 원자력, TIGER 200 중공업, ...
                  + Bond Sleeve 30% (국고채/MMF ETF)
```

**핵심 인사이트**: 단순 채권 헤지(Mode 2, Calmar 0.77)가 아닌, 반도체와 역상관 섹터(방산·원자력)로의 **완전전환**이 DD 제어의 핵심.

---

## Pipeline: 3단계 증류 결과

| 단계 | 모델 | CAGR | MaxDD | Calmar | Sharpe | TO |
|------|------|------|-------|--------|--------|----|
| Mode 1 (Discovery) | 반도체 EW | 18.6% | -35.6% | 0.52 | 0.670 | — |
| Mode 2 (Enhancement) | 레짐게이트+채권 | 21.9% | -28.4% | 0.77 | 0.979 | — |
| Mode 3 S4 (Distillation) | MomRot tight k=8/5 | 41.0% | -22.3% | 1.83 | 1.477 | 72.6% |
| **S3.5+IB (FSC 적합)** | **Bond30%+Inertia** | **25.7%** | **-15.9%** | **1.62** | **1.428** | **42.1%** |
| 비교: Teacher EW | 방산/원자력 8개 | 35.5% | -17.1% | 2.08 | 1.989 | 2.5% |

*Backtest: 2022-01-01 ~ 2026-03-30 (4.2년). 검증된 시뮬레이션 결과이며 실제 운용 수익률이 아닙니다.*

---

## 최근 성과 (2026-03-30 기준)

| 기간 | S4 전략 | KOSPI |
|------|---------|-------|
| 1개월 | -12.4% | -10.6% |
| 3개월 | +48.3% | — |
| 6개월 | +98.1% | — |

**현재 레짐: 🟢 BULL** (KODEX200 MA60 대비 +4.85%)
→ S3.5+IB: 반도체/AI 12개 top-by-momentum 70% + 단기채 30% 보유 중.

---

## FSC IRP/DC 적합성

| 규정 | 기준 | 적합 여부 |
|------|------|-----------|
| 위험자산 한도 | 주식형 ≤ 70% | ✅ Bond sleeve 30% |
| 안전자산 의무 | 채권/MMF ≥ 30% | ✅ Bond sleeve 30% |
| 레버리지 금지 | 2x/인버스 불가 | ✅ 해당 없음 |
| 백테스트 기간 | 최소 3년 | ✅ 4.2년 |
| 단일 ETF 한도 | ≤ 40% | ✅ EW ~6–8% |
| 최소 보유 종목 | ≥ 3개 | ✅ bull 12개 / bear 7개 |
| 코스콤 테스트베드 | 일임 출시 전 필수 | ⚠️ Phase 2 예정 |

---

## 상품화 로드맵

```
Phase 0 ✅  백테스트 4.2년 완료 (2026-03)
            pension.wwai.app 시뮬 공개

Phase 1     pension.wwai.app 페이퍼 포트폴리오 (2026-04~06)
            S3.5 + Bond 30% 일별 NAV 추적 시작
            회전율 38% 목표 실증

Phase 2     코스콤 테스트베드 등록 (2026-07~12)
            투자일임업자 파트너 확보
            예상 비용: 1,000만원

Phase 3     FSC 신고 → 공모 ETF or 일임 SMA (2027-01~06)
            목표 AUM: 50B KRW (ETF) or 제한 없음 (SMA)
```

---

## 리스크 고지

1. **백테스트 한계**: 4.2년 실데이터, SR 신뢰구간 ±0.6 — 과거 성과가 미래를 보장하지 않음
2. **룩어헤드 바이어스**: 유니버스 구성 시 현재 상장 ETF 사용 (Option B — 인정·미수정)
3. **레짐 신호 래그**: MA60은 후행 지표, 전환 시점 2-6주 오차
4. **섹터 집중**: Bull 구간 반도체 집중 위험 (6M +98%이나 1M -12% 모두 발생)
5. **코스콤 미심사**: 일임 서비스 출시 전 코스콤 테스트베드 통과 필수

---

*이 문서는 내부 제품 개발 논의용입니다. 투자 권유 또는 유가증권 청약 권유가 아닙니다.*
