# PC 부품 데이터 수집·분석 파이프라인

## 구성

- `src/crawler/danawa_crawler.py`: 8종 부품 수집. 웹페이지 원문은 **메모리에서 파싱**, HTML 저장 없음.
- `src/crawler/benchmark_crawler.py`: CPU/GPU/RAM/SSD 벤치마크 수집.
- `src/crawler/ratio_crawler.py`: 컴퓨존 사무/게이밍 견적 수집(별도 수동 실행용). 영상편집 카테고리는 사이트에서 0건이어서 수집하지 않음.
- `src/logic/data_processor.py`: 다나와 제품과 벤치마크 매칭. 매칭 진단 CSV 생성 안 함.
- `src/logic/ratio_analyzer.py`: 실제 견적 통계와 영상편집 비율 파생.
- `src/logic/create_DB.py`: 기존 8개 부품 테이블 및 `recommendation_budget_ratios` 생성.
- `src/logic/csv_to_db.py`: 부품 CSV 기존 적재 방식 + 비율 테이블 UPSERT.
- `src/logic/update_budget_ratios.py`: **크롤링 없이** 비율 CSV 재계산 및 DB 갱신.
- `src/logic/run_all.py`: 전체 벤치마크/다나와 크롤링 → 데이터 매칭 → 비율 재분석 → DB 적재.

## 설치 (Windows / Python)

```powershell
pip install -r requirements.txt
python -m playwright install chromium
```

Chrome/Selenium은 로컬 크롬 브라우저가 필요합니다. 기존 `src/logic/connection.py`의 로컬 MariaDB 연결 정보를 확인하세요. 원격 데이터베이스로 반영되는 것은 아닙니다.

## 비율 확인만 하고 싶을 때

```powershell
python src/logic/ratio_analyzer.py
```

입력 `data/compuzone_estimates.csv`, 출력 `data/recommendation_budget_ratios.csv`(18행)입니다. 실측 OFFICE/GAMING과 GAMING 기반 파생 VIDEO_EDITING으로 구분됩니다.

영상편집 비율 = 같은 예산 GAMING의 GPU 비율 × `0.95`. 줄어든 GPU 비율을 CPU 45%, RAM 35%, SSD 20%로 이동합니다. 8개 부품 합은 1.000000입니다.

## 비율만 DB에 갱신할 때 (크롤링 불필요)

```powershell
python src/logic/update_budget_ratios.py
```

이미 만들어진 DB에 전체 비율 CSV만 반영합니다. `usage + budget` UNIQUE 키에 대한 UPSERT이며, 기존 부품 테이블 8개를 TRUNCATE하지 않습니다.

## 기존 전체 파이프라인을 실행할 때

```powershell
python src/logic/run_all.py
```

전체 작업은 크롤링과 부품 테이블 재적재가 포함되어 오래 걸립니다. 비율만 갱신할 때 실행하지 마세요.

## 출력 정책

- **유지**: 부품 수집 CSV, 벤치마크 CSV, 통합 부품 CSV, `compuzone_estimates.csv`, `recommendation_budget_ratios.csv`.
- **생성 안 함**: 임시 페이지 HTML, 디버그 HTML, 매칭 진단 CSV, `.partial.csv`, 부가 비율 리포트 CSV.
- 정상 CSV는 크롤링이 전체 페이지 수집에 실패했을 때 덮어쓰지 않습니다.
- 기존에 다른 폴더에 남아 있는 오래된 디버그/임시 파일은 이 프로젝트 ZIP에 포함되지 않습니다.

> 실사이트 접근이 필요한 다나와/컴퓨존 크롤링과 실제 MariaDB 연결은 실행 환경에서 별도 확인이 필요합니다.
