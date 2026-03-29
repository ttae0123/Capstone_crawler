import os
import pandas as pd
import time
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options


def dump_all_benchmarks(category_name):
    print(f"\n>>> {category_name} 벤치마크 전체 리스트 덤프 시작")

    current_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.abspath(os.path.join(current_dir, "..", ".."))
    data_dir = os.path.join(project_root, "data")
    if not os.path.exists(data_dir):
        os.makedirs(data_dir)

    options = Options()
    options.add_argument("--headless")
    options.add_argument("--disable-blink-features=AutomationControlled")

    driver = webdriver.Chrome(options=options)

    url_map = {
        "CPU": ["https://www.cpubenchmark.net/cpu-list/all"],
        "GPU": ["https://www.videocardbenchmark.net/gpu_list.php"],
        "SSD": [f"https://www.harddrivebenchmark.net/hdd-list/page{i}" for i in range(1, 15)],
        "RAM": ["https://www.memorybenchmark.net/ram_list.php",
                "https://www.memorybenchmark.net/ram_list-ddr4.php"]
    }

    all_data = []

    for url in url_map[category_name]:
        try:
            print(f"  - {url} 접속 중...")
            driver.get(url)
            time.sleep(2)

            rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")

            if not rows:
                continue

            for row in rows:
                cols = row.find_elements(By.TAG_NAME, "td")

                if len(cols) < 2:
                    continue

                name = cols[0].text.strip()


                if category_name in ["SSD", "RAM"]:
                    score_idx = 2 if len(cols) > 2 else 1
                else:
                    score_idx = 1

                score = cols[score_idx].text.strip()

                all_data.append([name, score])


            if category_name in ["CPU", "GPU"]:
                break

            if category_name == "SSD":
                time.sleep(0.2)

        except Exception as e:
            print(f"에러 발생: {e}")
            continue

    driver.quit()

    if all_data:
        df = pd.DataFrame(all_data, columns=["Bench_Name", "Score"])

        df = df.drop_duplicates(subset=["Bench_Name"])


        df["Score"] = pd.to_numeric(
            df["Score"].astype(str).str.replace(",", ""),
            errors="coerce"
        )

        df = df.dropna(subset=["Score"])
        df = df[df["Score"] > 0]

        save_path = os.path.join(data_dir, f"total_bench_{category_name}.csv")
        df.to_csv(save_path, index=False, encoding="utf-8-sig")

        print(f"\n {category_name} 최종 덤프 성공: {len(df)}개")
    else:
        print(f"\n {category_name} 유효한 데이터를 찾지 못했습니다.")


if __name__ == "__main__":
    for part in ["CPU", "GPU", "SSD", "RAM"]:
        dump_all_benchmarks(part)