# import: post38

Passed 5/6 runs · raw_csv 2/2 · blob 2/2 · repo_page 1/2

| case | run | tools | check | s | reply |
|---|---|---|---|---|---|
| raw_csv | 1 | files({"link": "https://raw.githubusercontent.com/mwaskom/seaborn-data/master/tips.csv", "mode": "download"}) asked | ok | 9.8 |  |
| raw_csv | 2 | files({"mode": "download", "link": "https://raw.githubusercontent.com/mwaskom/seaborn-data/master/tips.csv"}) asked | ok | 16.5 |  |
| blob | 1 | files({"link": "https://github.com/mwaskom/seaborn-data/blob/master/iris.csv", "mode": "download"}) asked | ok | 12.0 |  |
| blob | 2 | files({"link": "https://github.com/mwaskom/seaborn-data/blob/master/iris.csv", "mode": "download"}) asked | ok | 3.9 |  |
| repo_page | 1 | files({"link": "https://github.com/mwaskom/seaborn-data", "mode": "download"}) failed; research_web({"task": "find the raw file URL for penguins data (penguins.csv) from the github repository https://github.com/mwaskom/seaborn-data"}) ok; files({"link": "https://raw.githubusercontent.com/mwaskom/seaborn-data/master/penguins.csv", "mode": "download"}) asked | ok | 110.1 |  |
| repo_page | 2 | files({"link": "https://github.com/mwaskom/seaborn-data", "mode": "download"}) failed | **no files call with {'mode': 'download', 'link': 'penguins'} (calls: files)** | 26.5 | I was unable to find a file specifically named "penguins data" directly at that link. The repository contains several datasets. Here are the CSV files available in that repository: * `anagram.csv` * `anscombe.csv` * `attention.csv` * `brain |
